"""Stateful fusion engine: one fused track per ``correlation_key`` (PRD §11.4).

The backend owns authoritative fused state (PRD §13.1/§13.3); the MQTT bus only
carries raw per-source records. This engine is where same-identity observations
from a local radio and an Internet feed collapse into a single track — fused on a
reliable ``correlation_key`` and *never* on proximity or ambiguous identity
(FUSION-FR-006). Records whose ``correlation_key`` is ``None`` never reach here;
``LiveState`` keeps those keyed by their own id.

Design split (FUSION-FR-007): the *only* mutable object is :class:`FusionGroup`,
which holds the latest record per contributing source. Everything that produces a
fused track — :meth:`FusionGroup.views`, :meth:`FusionGroup.fuse`,
:meth:`FusionGroup.all_expired` — is a pure function of (contributors, now, cfg),
so fusion is fully deterministic and unit-testable with an explicit ``now``.

Fusion metadata (active source, per-field source, last-local-RF time, …) rides
inside ``TrackRecord.attributes["fusion"]`` rather than in new schema fields, so
no ``schema_version`` bump is needed (PRD §37 "protect the schema").
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from aether.fusion.freshness import (
    DEFAULT_FRESHNESS,
    DEFAULT_FRESHNESS_FALLBACK,
    FreshnessWindow,
    age_seconds,
    classify,
    window_for,
)
from aether.fusion.precedence import (
    DYNAMIC_FIELDS,
    METADATA_FIELDS,
    ContributorView,
    FieldPick,
    pick_dynamic_field,
    pick_metadata_field,
)
from aether.schema.provenance import Provenance
from aether.schema.records import TrackRecord

log = logging.getLogger(__name__)

#: Single attributes key under which all fusion metadata is stored.
FUSION_ATTR_KEY = "fusion"

#: Conflict-detection epsilons: two non-expired contributors "disagree" on a field
#: only beyond these, keeping the (purely diagnostic) conflict list cheap. Geometry
#: uses a squared lon/lat-degree threshold (~0.0005² ≈ 50 m mid-latitude) to avoid
#: a real great-circle distance in the hot path.
_GEOMETRY_EPS_SQ = 0.0005**2
_FIELD_EPS: dict[str, float] = {
    "altitude_m": 30.0,
    "speed_mps": 5.0,
    "heading_deg": 5.0,
    "vertical_rate_mps": 1.0,
}


@dataclass(frozen=True)
class FusionConfig:
    """Freshness table + fallback driving precedence. Defaulted in code (no env knobs in M3.1)."""

    freshness: dict[str, FreshnessWindow] = field(default_factory=lambda: dict(DEFAULT_FRESHNESS))
    fallback: FreshnessWindow = DEFAULT_FRESHNESS_FALLBACK


@dataclass
class Contributor:
    """The latest record from one source contributing to a fused track."""

    source: str
    record: TrackRecord
    local_rf: bool
    observed_at: datetime
    received_at: datetime


def _is_local_rf(record: TrackRecord) -> bool:
    """Whether this observation is from the operator's own RF (PRD §8.1).

    Read from provenance ``local_rf`` (the load-bearing flag), falling back to the
    record's ``locally_received`` — never inferred from the source *name*, so the
    engine stays generic over sources (PRD §37).
    """
    if any(p.local_rf for p in record.provenance):
        return True
    return record.locally_received


class FusionGroup:
    """Mutable per-key bag of contributors; fuses them into one track on demand."""

    def __init__(self, key: str) -> None:
        self.key = key
        self.contributors: dict[str, Contributor] = {}
        #: Most recent ``observed_at`` of *any* local-RF contributor ever seen.
        #: Monotonic — does not regress when a once-local contributor is pruned,
        #: so "when did my antenna last hear this?" survives local expiry (PRD §8.1).
        self.last_local_rf_at: datetime | None = None
        #: A contributor was added/removed since the last :meth:`fuse`; lets
        #: ``LiveState.expire`` recompute groups whose membership changed.
        self._dirty = False

    @property
    def dirty(self) -> bool:
        return self._dirty

    def mark_clean(self) -> None:
        self._dirty = False

    def update(self, record: TrackRecord) -> bool:
        """Add/replace this source's contribution. Returns False if discarded (out-of-order).

        An older observation from a source we already have a newer one for is
        dropped for fusion (the track must not jump backwards); an equal or newer
        ``observed_at`` replaces it. Duplicates are therefore idempotent.
        """
        local_rf = _is_local_rf(record)
        existing = self.contributors.get(record.source)
        if existing is not None and record.observed_at < existing.observed_at:
            return False
        self.contributors[record.source] = Contributor(
            source=record.source,
            record=record,
            local_rf=local_rf,
            observed_at=record.observed_at,
            received_at=record.received_at,
        )
        if local_rf and (
            self.last_local_rf_at is None or record.observed_at > self.last_local_rf_at
        ):
            self.last_local_rf_at = record.observed_at
        self._dirty = True
        return True

    def views(self, now: datetime, cfg: FusionConfig) -> list[ContributorView]:
        """Evaluate every contributor's freshness at ``now`` (pure)."""
        views: list[ContributorView] = []
        for contrib in self.contributors.values():
            window = window_for(contrib.source, cfg.freshness, cfg.fallback)
            fresh = classify(age_seconds(contrib.observed_at, now), window)
            views.append(
                ContributorView(
                    source=contrib.source,
                    record=contrib.record,
                    local_rf=contrib.local_rf,
                    observed_at=contrib.observed_at,
                    freshness=fresh,
                )
            )
        return views

    def all_expired(self, now: datetime, cfg: FusionConfig) -> bool:
        """True when every contributor has expired (the track should be removed)."""
        views = self.views(now, cfg)
        return bool(views) and all(v.freshness == "expired" for v in views)

    def prune_expired(self, now: datetime, cfg: FusionConfig) -> bool:
        """Drop expired contributors; return True if any were removed."""
        before = len(self.contributors)
        keep: dict[str, Contributor] = {}
        for view, contrib in zip(self.views(now, cfg), self.contributors.values(), strict=True):
            if view.freshness != "expired":
                keep[contrib.source] = contrib
        self.contributors = keep
        return len(self.contributors) != before

    def fuse(self, now: datetime, cfg: FusionConfig) -> TrackRecord:
        """Build the fused :class:`TrackRecord` from current contributors (pure)."""
        views = self.views(now, cfg)
        contribs = list(self.contributors.values())

        # Per-field winners drive both the values and the fusion provenance block.
        dyn: dict[str, FieldPick] = {f: pick_dynamic_field(f, views) for f in DYNAMIC_FIELDS}
        meta: dict[str, FieldPick] = {f: pick_metadata_field(f, views) for f in METADATA_FIELDS}

        # Headline source = whoever supplied the geometry (PRD §8.1); fall back to
        # any contributor when no one has a position.
        geometry_pick = dyn["geometry"]
        active_source = geometry_pick.source or (contribs[0].source if contribs else self.key)
        active_contrib = self.contributors.get(active_source)

        anchor = active_contrib.record if active_contrib else contribs[0].record
        track_type = anchor.track_type
        predicted = anchor.predicted

        # A track is locally received iff some local-RF contributor is not expired.
        locally_received = any(v.local_rf and v.freshness != "expired" for v in views)

        observed_at = max(c.observed_at for c in contribs)
        received_at = max(c.received_at for c in contribs)
        max_expire = max(
            window_for(c.source, cfg.freshness, cfg.fallback).expire_s for c in contribs
        )
        valid_until = max(c.observed_at for c in contribs) + timedelta(seconds=max_expire)

        tags = sorted({t for c in contribs for t in c.record.tags})
        attributes = self._merge_attributes(contribs)
        attributes[FUSION_ATTR_KEY] = self._fusion_block(views, dyn, active_source)

        return TrackRecord(
            id=self.key,
            source=active_source,
            observed_at=observed_at,
            received_at=received_at,
            published_at=now,
            correlation_key=self.key,
            track_type=track_type,
            label=meta["label"].value,
            geometry=dyn["geometry"].value,
            altitude_m=dyn["altitude_m"].value,
            speed_mps=dyn["speed_mps"].value,
            heading_deg=dyn["heading_deg"].value,
            vertical_rate_mps=dyn["vertical_rate_mps"].value,
            locally_received=locally_received,
            classification=meta["classification"].value,
            valid_until=valid_until,
            predicted=predicted,
            tags=tags,
            attributes=attributes,
            provenance=self._merged_provenance(),
        )

    def _merge_attributes(self, contribs: list[Contributor]) -> dict[str, Any]:
        """Shallow-merge contributor attributes, last writer (by received_at) wins.

        The inbound ``fusion`` key is dropped — fusion metadata is recomputed
        fresh each call, never re-fused from a prior fused record.
        """
        merged: dict[str, Any] = {}
        for contrib in sorted(contribs, key=lambda c: c.received_at):
            for k, v in contrib.record.attributes.items():
                if k == FUSION_ATTR_KEY:
                    continue
                merged[k] = v
        return merged

    def _merged_provenance(self) -> list[Provenance]:
        """One provenance entry per contributing source (sorted), copied from its record.

        RF and Internet paths stay separate entries (PRD §15.3 generalized): each
        source's own provenance entry is preserved so the operator can trace every
        field back to who observed it. Falls back to a synthesized entry if a
        source somehow published no provenance of its own.
        """
        entries: list[Provenance] = []
        for source in sorted(self.contributors):
            contrib = self.contributors[source]
            own = next((p for p in contrib.record.provenance if p.source == source), None)
            if own is not None:
                entries.append(own)
            else:
                entries.append(
                    Provenance(
                        source=source,
                        observed_at=contrib.observed_at,
                        received_at=contrib.received_at,
                        local_rf=contrib.local_rf,
                    )
                )
        return entries

    def _fusion_block(
        self,
        views: list[ContributorView],
        dyn: dict[str, FieldPick],
        active_source: str,
    ) -> dict[str, Any]:
        """Build the ``attributes['fusion']`` diagnostic block (all JSON-safe)."""
        by_source = {v.source: v for v in views}
        contributors = [
            {
                "source": v.source,
                "local_rf": v.local_rf,
                "observed_at": v.observed_at.isoformat(),
                "freshness": v.freshness,
            }
            for v in sorted(views, key=lambda v: v.source)
        ]
        field_sources = {f: dyn[f].source for f in DYNAMIC_FIELDS}
        field_freshness = {f: dyn[f].freshness for f in DYNAMIC_FIELDS}
        block: dict[str, Any] = {
            "active_source": active_source,
            "contributors": contributors,
            "field_sources": field_sources,
            "field_freshness": field_freshness,
            "last_local_rf_at": (
                self.last_local_rf_at.isoformat() if self.last_local_rf_at is not None else None
            ),
            "fused_count": len(self.contributors),
        }
        conflicts = self._conflicts(by_source, dyn)
        if conflicts:
            block["conflicts"] = conflicts
        return block

    def _conflicts(
        self,
        by_source: dict[str, ContributorView],
        dyn: dict[str, FieldPick],
    ) -> list[dict[str, Any]]:
        """Diagnostic: non-expired contributors disagreeing on a field beyond epsilon.

        Purely informational — the chosen value is always the deterministic
        precedence winner (PRD §15.5); this just records that others differed.
        """
        live = {s: v for s, v in by_source.items() if v.freshness != "expired"}
        if len(live) < 2:
            return []
        conflicts: list[dict[str, Any]] = []
        for fld in DYNAMIC_FIELDS:
            pick = dyn[fld]
            if pick.source is None or pick.source not in live:
                continue
            winning_value = pick.value
            others: list[dict[str, Any]] = []
            for source in sorted(live):
                if source == pick.source:
                    continue
                other_value = getattr(live[source].record, fld)
                if other_value is None:
                    continue
                if _disagrees(fld, winning_value, other_value):
                    others.append({"source": source, "value": _jsonable(other_value)})
            if others:
                conflicts.append(
                    {
                        "field": fld,
                        "winner": pick.source,
                        "winning_value": _jsonable(winning_value),
                        "others": others,
                    }
                )
        return conflicts


class FusionEngine:
    """Owns one :class:`FusionGroup` per ``correlation_key`` and fuses on ingest."""

    def __init__(self, cfg: FusionConfig | None = None) -> None:
        self._cfg = cfg if cfg is not None else FusionConfig()
        self._groups: dict[str, FusionGroup] = {}

    def ingest(self, record: TrackRecord, now: datetime) -> TrackRecord:
        """Merge a track into its group and return the recomputed fused track.

        Precondition: ``record.correlation_key`` is not ``None`` (callers route
        ``None``-key records around the engine). An out-of-order record is
        discarded for fusion but the *current* fused track is still returned, so
        ``apply`` always yields one idempotent upsert.
        """
        key = record.correlation_key
        assert key is not None  # routed here only with a correlation key
        group = self._groups.get(key)
        if group is None:
            group = FusionGroup(key)
            self._groups[key] = group
        group.update(record)
        fused = group.fuse(now, self._cfg)
        group.mark_clean()
        return fused

    def recompute(self, key: str, now: datetime) -> TrackRecord | None:
        """Re-fuse an existing group at ``now`` (no new record); ``None`` if unknown."""
        group = self._groups.get(key)
        if group is None:
            return None
        fused = group.fuse(now, self._cfg)
        group.mark_clean()
        return fused

    def expired_keys(self, now: datetime) -> list[str]:
        """Keys whose every contributor has expired — the track should be removed.

        Isolated per group: a single group whose freshness evaluation raises is
        logged and skipped rather than aborting the whole batch, so one poison
        group cannot stop every other track from being reaped (PRD §37).
        """
        keys: list[str] = []
        for key, group in self._groups.items():
            try:
                if group.all_expired(now, self._cfg):
                    keys.append(key)
            except Exception:
                log.warning("fusion expiry check failed for %s; skipping", key, exc_info=True)
        return keys

    def dirty_keys(self, now: datetime) -> list[str]:
        """Non-expired keys that lost a contributor since the last fuse.

        Pruning expired contributors here lets the UI see a LOCAL→NET handoff
        (``locally_received`` flips, ``active_source`` changes) without any new
        ingest — the fused track continues from the surviving network observation
        (FUSION-FR-004). Isolated per group like :meth:`expired_keys`.
        """
        dirty: list[str] = []
        for key, group in self._groups.items():
            try:
                if group.all_expired(now, self._cfg):
                    continue  # handled by expired_keys / drop
                if group.prune_expired(now, self._cfg):
                    dirty.append(key)
            except Exception:
                log.warning("fusion prune failed for %s; skipping", key, exc_info=True)
        return dirty

    def drop(self, key: str) -> None:
        """Forget a group entirely (its track has been removed)."""
        self._groups.pop(key, None)


def _disagrees(field_name: str, a: Any, b: Any) -> bool:
    """Whether two same-field values differ beyond the field's epsilon."""
    if field_name == "geometry":
        ca, cb = a.coordinates, b.coordinates
        d2 = (float(ca[0]) - float(cb[0])) ** 2 + (float(ca[1]) - float(cb[1])) ** 2
        return d2 > _GEOMETRY_EPS_SQ
    eps = _FIELD_EPS.get(field_name, 0.0)
    return abs(float(a) - float(b)) > eps


def _jsonable(value: Any) -> Any:
    """Coerce a field value to a JSON-safe form for the conflicts diagnostic."""
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return value

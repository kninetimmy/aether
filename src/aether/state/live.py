"""In-memory live state: the backend's current fused world (PRD §19.1).

Holds current tracks and geo-features (keyed by id), a bounded ring of recent
events, open alerts, and the latest status per source, plus a monotonic sequence
number bumped on every mutation. ``apply`` returns a ``StateChange`` describing
what happened; the websocket layer turns those into wire deltas (PRD §22.4) and
serves ``snapshot()`` to newly connected clients.

No persistence here — SQLite arrives at M4 with its first consumers. A single
asyncio loop owns this object, so it carries no locks.

Track fusion (M3.1, PRD §11.4/§15): a ``TrackRecord`` with a ``correlation_key``
is routed through the in-process :class:`~aether.fusion.engine.FusionEngine`, so
same-identity local-RF and network observations appear as *one* track keyed by
that correlation key — fresh local data privileged, network filling the gaps,
and the track continuing from a network observation when the local radio goes
quiet (FUSION-FR-001..005). The bus keeps carrying raw per-source records; this
backend, not the bus, is the source of truth for the fused world (PRD §13.3).
Records with no ``correlation_key`` are never fused — they stay keyed by their
own id (FUSION-FR-006: no merge without a reliable key).
"""

import logging
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, assert_never

from aether.fusion.engine import FusionEngine
from aether.schema.records import (
    AlertRecord,
    EventRecord,
    GeoFeatureRecord,
    Record,
    SourceStatusRecord,
    TrackRecord,
)
from aether.state.sequence import Sequence

log = logging.getLogger(__name__)

#: Default cap on the recent-events ring buffer kept in live state.
RECENT_EVENTS_MAX = 256

StateKind = Literal["track", "feature", "event", "alert", "source_status"]


@dataclass(frozen=True)
class StateChange:
    """One mutation of live state, ready for the websocket to render as a delta."""

    seq: int
    op: Literal["upsert", "remove", "event"]
    kind: StateKind
    id: str
    record: Record | None  # set for upsert/event; None for remove


@dataclass
class Snapshot:
    """A point-in-time view of all live state at sequence ``seq`` (PRD §22.3)."""

    seq: int
    tracks: list[TrackRecord]
    features: list[GeoFeatureRecord]
    events: list[EventRecord]
    alerts: list[AlertRecord]
    source_status: list[SourceStatusRecord]


class LiveState:
    def __init__(
        self,
        *,
        recent_events_max: int = RECENT_EVENTS_MAX,
        fusion: FusionEngine | None = None,
    ) -> None:
        self._seq = Sequence()
        self._tracks: dict[str, TrackRecord] = {}
        self._features: dict[str, GeoFeatureRecord] = {}
        self._alerts: dict[str, AlertRecord] = {}
        self._source_status: dict[str, SourceStatusRecord] = {}
        self._events: deque[EventRecord] = deque(maxlen=recent_events_max)
        self._fusion = fusion if fusion is not None else FusionEngine()

    @property
    def seq(self) -> int:
        return self._seq.current

    def apply(self, record: Record, now: datetime | None = None) -> StateChange:
        """Merge a normalized record into live state and return the change.

        ``now`` (the wall clock read once at the I/O edge) drives fusion freshness;
        it defaults to the record's ``published_at`` so the existing single-arg
        callers and tests keep working. A ``TrackRecord`` with a ``correlation_key``
        is fused (its fused id *is* the correlation key); ``None``-key tracks and
        all other kinds are stored as-is. One source record always yields exactly
        one ``StateChange`` (PRD §22.4), preserving the websocket contract.
        """
        seq = self._seq.next()
        if isinstance(record, TrackRecord):
            return self._apply_track(seq, record, now if now is not None else record.published_at)
        if isinstance(record, GeoFeatureRecord):
            self._features[record.id] = record
            return StateChange(seq, "upsert", "feature", record.id, record)
        if isinstance(record, AlertRecord):
            self._alerts[record.id] = record
            return StateChange(seq, "upsert", "alert", record.id, record)
        if isinstance(record, SourceStatusRecord):
            self._source_status[record.source] = record
            return StateChange(seq, "upsert", "source_status", record.id, record)
        if isinstance(record, EventRecord):
            self._events.append(record)
            return StateChange(seq, "event", "event", record.id, record)
        assert_never(record)

    def _apply_track(self, seq: int, record: TrackRecord, now: datetime) -> StateChange:
        """Store a track, fusing it on ``correlation_key`` when it has one.

        A fused track's id is its correlation key, so a local-only aircraft is
        byte-identical to the pre-fusion behavior (readsb already sets id ==
        correlation_key) — no marker churn. If fusion ever raises (e.g. a fused
        record fails validation), we degrade: store the raw record *rekeyed to its
        correlation key* and keep serving, never crash the loop (PRD §37).
        """
        if record.correlation_key is None:
            self._tracks[record.id] = record
            return StateChange(seq, "upsert", "track", record.id, record)
        try:
            fused = self._fusion.ingest(record, now)
        except Exception:
            log.warning(
                "fusion failed for %s; storing raw record",
                record.correlation_key,
                exc_info=True,
            )
            # The degraded record must still obey the fused-id contract: its own
            # ``.id`` is a per-source id (e.g. "demo-net:abc"), but it is stored
            # under — and broadcast as — the correlation key, so a later engine
            # expiry ``remove`` (keyed by correlation key) matches and cleans it
            # up instead of stranding a ghost on every client (PRD §22.4/§37).
            degraded = record.model_copy(update={"id": record.correlation_key})
            self._tracks[record.correlation_key] = degraded
            return StateChange(seq, "upsert", "track", record.correlation_key, degraded)
        self._tracks[fused.id] = fused
        return StateChange(seq, "upsert", "track", fused.id, fused)

    def remove(self, kind: StateKind, id: str) -> StateChange:
        """Drop a track/feature/alert by id and return the removal change."""
        seq = self._seq.next()
        if kind == "track":
            self._tracks.pop(id, None)
            self._fusion.drop(id)  # a removed fused track must forget its group
        elif kind == "feature":
            self._features.pop(id, None)
        elif kind == "alert":
            self._alerts.pop(id, None)
        return StateChange(seq, "remove", kind, id, None)

    def expire(self, now: datetime) -> list[StateChange]:
        """Age out stale tracks/features at ``now`` and emit the resulting changes.

        Order (PRD §15.4, FUSION-FR-004):

        1. Fused groups whose *every* contributor expired are removed (and the
           engine forgets them).
        2. Fused groups that merely *lost* a contributor are recomputed and
           re-upserted, so a LOCAL→NET handoff (``locally_received`` flips,
           ``active_source`` changes) reaches clients with no new ingest.
        3. The existing ``valid_until`` sweep catches ``None``-key tracks and
           features; ids the engine already handled are skipped (and ``remove``
           is idempotent via ``pop`` regardless).

        Each per-group recompute is isolated like :meth:`apply`: a single poison
        group whose ``fuse`` raises is logged and dropped from the engine rather
        than aborting the whole sweep — otherwise no track (fused or ``None``-key)
        would ever expire and live state would grow unbounded (PRD §37 failure
        isolation + resource bounds).
        """
        changes: list[StateChange] = []
        engine_handled: set[str] = set()

        for key in self._fusion.expired_keys(now):
            engine_handled.add(key)
            changes.append(self.remove("track", key))
        for key in self._fusion.dirty_keys(now):
            if key in engine_handled:
                continue
            try:
                fused = self._fusion.recompute(key, now)
            except Exception:
                # A permanently-poison group must not pin the sweep forever: drop
                # it so its last-known track ages out via the valid_until sweep
                # below and the loop keeps reaping every other group.
                log.warning("fusion recompute failed for %s; dropping group", key, exc_info=True)
                self._fusion.drop(key)
                engine_handled.add(key)
                continue
            if fused is None:
                continue
            engine_handled.add(fused.id)
            self._tracks[fused.id] = fused
            changes.append(StateChange(self._seq.next(), "upsert", "track", fused.id, fused))

        for tid in [
            tid
            for tid, t in self._tracks.items()
            if tid not in engine_handled and t.valid_until is not None and t.valid_until <= now
        ]:
            changes.append(self.remove("track", tid))
        for fid in [
            fid
            for fid, f in self._features.items()
            if f.valid_until is not None and f.valid_until <= now
        ]:
            changes.append(self.remove("feature", fid))
        return changes

    def get_track(self, track_id: str) -> TrackRecord | None:
        """Return the current track stored under ``track_id``, or ``None``.

        Backs ``GET /api/v2/tracks/{track_id}`` (PRD §21.3): a fused track's id *is*
        its correlation key, so the same id the snapshot/websocket exposes is the
        lookup key here — no separate index needed.
        """
        return self._tracks.get(track_id)

    def snapshot(self) -> Snapshot:
        """Return the full current state at the current sequence number."""
        return Snapshot(
            seq=self._seq.current,
            tracks=list(self._tracks.values()),
            features=list(self._features.values()),
            events=list(self._events),
            alerts=list(self._alerts.values()),
            source_status=list(self._source_status.values()),
        )

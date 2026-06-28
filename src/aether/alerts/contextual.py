"""Contextual alert-operator evaluation (M4.6c, PRD §20.2/§20.3, §12 #6/#7).

The stateless core answers "does this one snapshot satisfy a stateless leaf?". A
*contextual* leaf needs more: a geofence shape (entered/exited_geofence), the
station/geofence position + a geometry (distance_*/elevation_crossed), a per-subject
timestamp history (count_within_window), the subject's *prior* field value
(changed_to/from), or a time-bounded feature's activation clock (became_active). This
module holds exactly that missing per-subject (or clock-relative) state and
collapses a contextual rule's whole AND into the SAME boolean *level* (plus a
discrete flag) the engine already consumes for stateless rules, so the engine's
transition/cooldown/dedup/auto-resolve machinery is reused verbatim. One firing
model. State is per (rule_id, RAW subject), pruned in lockstep with the engine
(PRD §37 bounded maps). Geofences are synced in, mirroring the ruleset sync, so
evaluation never reads the store on the hot path (PRD §5). No I/O; the clock (now)
is injected by the engine.
"""

from __future__ import annotations

import logging
from collections import OrderedDict, deque
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from aether.alerts import geo
from aether.alerts.conditions import (
    _MISSING,
    STATELESS_OPERATORS,
    evaluate_leaf,
    resolve_field,
)
from aether.alerts.identity import watchlist_key
from aether.schema.alert_rule import AlertCondition, AlertRule
from aether.schema.geofence import Geofence
from aether.schema.geometry import MultiPolygon, Point, Polygon, Position
from aether.schema.records import GeoFeatureRecord, Record, TrackRecord

log = logging.getLogger(__name__)

#: The two contextual operators that are *discrete* triggers (a point-in-time flip)
#: rather than a continuous level — they route through the engine's discrete path.
_CHANGED_OPERATORS: frozenset[str] = frozenset({"changed_to", "changed_from"})

#: Hard cap on retained per-subject contextual state (PRD §37 — bounded, no runaway).
#: Track subjects are pruned precisely on removal (``forget_subject``), but a
#: ``changed_*``/``count_within_window`` rule over *events* (each a unique id that is
#: never "removed") would otherwise accumulate one entry per event forever. An LRU
#: backstop keeps the map bounded; the cap is far above the live-subject working set,
#: so in normal operation it never evicts an active subject's state.
_MAX_SUBJECT_STATES = 50_000


@dataclass(frozen=True)
class StationRef:
    """The home-station position contextual distance/elevation operators measure from.

    ``configured`` is precomputed (``not null-island``) so the hot path never has to
    re-derive it: at 0,0 the station is unset and any station-relative leaf degrades
    to *unevaluable* rather than silently measuring from null island (PRD §5/§37).
    """

    lon: float
    lat: float
    configured: bool


@dataclass
class _SubjectState:
    """Per (rule_id, subject) memory the stateless core cannot hold.

    ``seen`` flips True after the first evaluation so a changed_* leaf suppresses an
    initial sighting. ``prior_values`` caches each changed_* leaf field's last
    resolved value (keyed by field path; _MISSING until first seen). ``window`` is
    the push-and-prune timestamp deque for count_within_window.
    """

    seen: bool = False
    prior_values: dict[str, Any] = field(default_factory=dict)
    window: deque[datetime] = field(default_factory=deque)


@dataclass(frozen=True)
class ContextResult:
    """The verdict the engine consumes for one contextual ``(rule, subject)`` tick.

    ``evaluable`` False ⇒ the engine does nothing at all (no fire, no auto-resolve):
    a contextual leaf could not be answered (missing geofence/geometry/station), so
    the rule is honestly "unknown" rather than a confident False (PRD §37).
    """

    evaluable: bool
    level: bool
    discrete: bool


def _record_point(record: Record) -> tuple[float, float, float | None] | None:
    """A record's representative ``(lon, lat, altitude_m)`` for geometry leaves, or None.

    A track contributes its point geometry plus its altitude. A geo-feature contributes
    its ``Point`` geometry with **no** altitude (a quake's depth or a fire pixel is not
    an elevation, so altitude stays ``None`` → ``elevation_crossed`` is unevaluable for
    a feature, an honest unknown rather than a bogus 0 deg). A track without geometry,
    or a feature whose geometry is not a ``Point`` (a polygon TFR — areal distance lands
    with that slice), yields ``None`` so the geometry leaf reports unevaluable.
    """
    if isinstance(record, TrackRecord):
        if record.geometry is None:
            return None
        return record.geometry.coordinates[0], record.geometry.coordinates[1], record.altitude_m
    if isinstance(record, GeoFeatureRecord) and isinstance(record.geometry, Point):
        return record.geometry.coordinates[0], record.geometry.coordinates[1], None
    return None


def _feature_exterior_rings(record: Record) -> list[list[Position]] | None:
    """A geo-feature's exterior area rings for the areal ``geofence_intersects`` leaf.

    A ``Polygon`` contributes its exterior ring; a ``MultiPolygon`` each polygon's
    exterior ring. Holes are intentionally excluded (the TFR adapter emits hole-free
    areas, and the overlap test is coarse). A track, a point feature (a quake epicentre,
    a fire pixel), or a feature with no areal geometry yields ``None`` so the leaf reports
    unevaluable — an honest unknown, never an invented area (PRD §37)."""
    if isinstance(record, GeoFeatureRecord):
        geometry = record.geometry
        if isinstance(geometry, Polygon) and geometry.coordinates:
            return [geometry.coordinates[0]]
        if isinstance(geometry, MultiPolygon):
            return [poly[0] for poly in geometry.coordinates if poly]
    return None


class ContextualEvaluator:
    """Turns a contextual rule into the engine's ``(level, discrete)`` firing input.

    Owns the per-subject state the stateless core lacks and an in-memory mirror of
    the geofence set (synced exactly like the ruleset), so evaluation never reads the
    store and never blocks (PRD §5). The clock is passed in by the engine — this
    module reads no wall clock of its own, keeping evaluation deterministic.
    """

    def __init__(self, *, station: StationRef) -> None:
        self._station = station
        self._geofences: dict[str, Geofence] = {}
        self._refs: dict[str, Position] = {}  # cached reference points (distance_*)
        self._watchlist: set[str] = set()  # canonical watchlist membership set
        # LRU-ordered so the rare overflow eviction drops the least-recently-touched
        # subject (see _MAX_SUBJECT_STATES); the precise prunes below keep it small in
        # practice and this is only a runaway backstop.
        self._state: OrderedDict[tuple[str, str], _SubjectState] = OrderedDict()

    # -- geofence sync (mirrors AlertEngine.set_rules/upsert_rule/remove_rule) --

    def set_geofences(self, geofences: Iterable[Geofence]) -> None:
        """Replace the whole geofence set (startup load); rebuild the ref cache."""
        self._geofences = {gf.id: gf for gf in geofences}
        self._refs = {gf.id: geo.geofence_reference_point(gf) for gf in self._geofences.values()}

    def upsert_geofence(self, geofence: Geofence) -> None:
        """Add or replace one geofence (after a successful create/patch)."""
        self._geofences[geofence.id] = geofence
        self._refs[geofence.id] = geo.geofence_reference_point(geofence)

    def remove_geofence(self, geofence_id: str) -> None:
        """Drop one geofence and its cached reference point (after a delete)."""
        self._geofences.pop(geofence_id, None)
        self._refs.pop(geofence_id, None)

    # -- watchlist sync (mirrors the geofence sync; feeds the watchlist operator) --

    def set_watchlist(self, keys: Iterable[str]) -> None:
        """Replace the whole watchlist membership set (startup load)."""
        self._watchlist = set(keys)

    def upsert_watchlist(self, key: str) -> None:
        """Add one key to the membership set (after a successful PUT/PATCH)."""
        self._watchlist.add(key)

    def remove_watchlist(self, key: str) -> None:
        """Drop one key from the membership set (after a successful DELETE)."""
        self._watchlist.discard(key)

    # -- state pruning (lockstep with the engine's firing map) --

    def set_rules(self, rule_ids: frozenset[str]) -> None:
        """Drop per-subject state for rules no longer present (startup reload)."""
        self._state = OrderedDict(
            (key, st) for key, st in self._state.items() if key[0] in rule_ids
        )

    def forget_rule(self, rule_id: str) -> None:
        """Drop all state a deleted (or edited) rule owned (mirrors remove_rule)."""
        self._state = OrderedDict((key, st) for key, st in self._state.items() if key[0] != rule_id)

    def forget_subject(self, subject: str) -> None:
        """Drop every rule's state for a removed track subject (mirrors _on_remove)."""
        self._state = OrderedDict((key, st) for key, st in self._state.items() if key[1] != subject)

    def _touch_state(self, key: tuple[str, str]) -> _SubjectState:
        """Get-or-create one subject's state, maintaining LRU order and the cap.

        An existing entry is bumped to most-recently-used; a new one evicts the
        least-recently-used first if the map is at :data:`_MAX_SUBJECT_STATES` (the
        backstop that bounds unbounded event-subject growth, PRD §37).
        """
        state = self._state.get(key)
        if state is not None:
            self._state.move_to_end(key)
            return state
        if len(self._state) >= _MAX_SUBJECT_STATES:
            self._state.popitem(last=False)
        state = _SubjectState()
        self._state[key] = state
        return state

    # -- evaluation --

    def evaluate(
        self, rule: AlertRule, subject: str, record: Record, dump: dict[str, Any], now: datetime
    ) -> ContextResult:
        """Collapse a contextual rule's AND into ``(evaluable, level, discrete)``.

        Two passes. First the non-count leaves are evaluated — the stateless core
        (verbatim), the changed_* baseline reads, and the geometry predicates — giving
        ``other_level`` and whether every geometry leaf was answerable. A leaf that
        cannot be answered makes the whole result *unevaluable*, so the engine does
        nothing (honest unknown, PRD §37). Then each ``count_within_window`` leaf is
        folded in: this observation is recorded only when it *qualifies* — the rest of
        the rule matched and the rule is evaluable — so the window counts qualifying
        sightings, not raw ticks (PRD §20.2).

        Per-subject state (the changed_* baselines, ``seen``, the count window) mutates
        **only on an evaluable tick**: an unevaluable tick leaves no residue, so a later
        evaluable tick is measured against real prior state rather than a phantom one.
        Any unexpected error is caught and reported unevaluable (and likewise leaves
        state untouched) so one poison record never crashes the hub observer.
        """
        state = self._touch_state((rule.id, subject))
        first_obs = not state.seen
        discrete = False
        evaluable = True
        other_level = True  # AND of every non-count leaf
        count_conds: list[AlertCondition] = []
        try:
            for cond in rule.conditions:
                op = cond.operator
                if op == "count_within_window":
                    count_conds.append(cond)  # folded in once the rest is known
                    continue
                if op in STATELESS_OPERATORS:
                    ok = evaluate_leaf(cond, dump)  # reuse the core verbatim
                elif op in _CHANGED_OPERATORS:
                    discrete = True
                    ok = self._eval_changed(cond, state, dump, first_obs)
                elif op == "watchlist":
                    ok = self._eval_watchlist(cond, record)  # always evaluable (pure level)
                elif op == "became_active":
                    ok, ev = self._eval_became_active(record, now)
                    evaluable = evaluable and ev
                else:  # geometry leaves
                    ok, ev = self._eval_geometry(op, cond, rule, record)
                    evaluable = evaluable and ev
                other_level = other_level and ok
            count_level = self._eval_counts(
                count_conds, state, now, qualifying=evaluable and other_level
            )
            level = other_level and count_level
        except Exception:  # poison record / geometry — degrade, never crash
            log.warning(
                "contextual rule %s evaluation failed; treating as unevaluable",
                rule.id,
                exc_info=True,
            )
            return ContextResult(evaluable=False, level=False, discrete=discrete)
        if evaluable:  # only an answerable tick advances baselines / marks the subject seen
            state.seen = True
            self._commit_changed(rule, state, dump)
        return ContextResult(evaluable=evaluable, level=level and evaluable, discrete=discrete)

    # -- per-operator helpers --

    def _eval_changed(
        self, cond: AlertCondition, state: _SubjectState, dump: dict[str, Any], first_obs: bool
    ) -> bool:
        """A ``changed_to``/``changed_from`` leaf's current truth (discrete).

        Suppressed on the first observation (an initial sighting is not a change) and
        whenever the baseline is still ``_MISSING`` (no concrete prior value yet), so
        no spurious change fires before a real prior→new transition exists. A real
        flip fires when ``changed_to`` reaches the target value or ``changed_from``
        leaves it. Baselines are advanced separately in :meth:`_commit_changed`.
        """
        if first_obs:
            return False
        prior = state.prior_values.get(cond.field, _MISSING)
        if prior is _MISSING:
            return False
        cur = resolve_field(dump, cond.field)
        if prior == cur:
            return False
        if cond.operator == "changed_to":
            return bool(cur == cond.value)
        return bool(prior == cond.value)  # changed_from

    def _eval_counts(
        self,
        conds: list[AlertCondition],
        state: _SubjectState,
        now: datetime,
        *,
        qualifying: bool,
    ) -> bool:
        """AND of every ``count_within_window`` leaf against the subject's window.

        ``qualifying`` (the rule's other leaves matched *and* the rule is evaluable)
        gates whether this observation is recorded — so the window counts only sightings
        that satisfied the rest of the rule, and an unevaluable or non-matching tick
        leaves no residue (PRD §20.2: a count of qualifying observations, not raw ticks).
        The shared deque is pruned to the widest leaf window so it stays bounded; each
        leaf then tests its own ``window_s``/``threshold`` against it. The engine's
        rising-edge detection still gives the §20.3 "not on every unchanged update"
        guarantee, and a window draining below N auto-resolves an open enter-rule alert.
        """
        if not conds:
            return True
        if qualifying:
            state.window.append(now)
        widest = max((c.window_s if c.window_s is not None else 0.0) for c in conds)
        cutoff = now - timedelta(seconds=widest)
        while state.window and state.window[0] < cutoff:
            state.window.popleft()
        return all(self._count_leaf_ok(c, state, now) for c in conds)

    def _count_leaf_ok(self, cond: AlertCondition, state: _SubjectState, now: datetime) -> bool:
        """Whether qualifying observations within this leaf's own window meet its threshold."""
        window_s = cond.window_s if cond.window_s is not None else 0.0
        threshold = cond.threshold if cond.threshold is not None else 0.0
        cutoff = now - timedelta(seconds=window_s)
        count = sum(1 for t in state.window if t >= cutoff)
        return count >= threshold

    def _eval_geometry(
        self, op: str, cond: AlertCondition, rule: AlertRule, record: Record
    ) -> tuple[bool, bool]:
        """A geometry leaf's ``(level, evaluable)``.

        ``geofence_intersects`` is *areal* — it works on the feature's polygon rings
        directly, so it is handled before the point reduction (a TFR has no single
        representative point). Every other geometry leaf needs a representative point —
        a track's position+altitude, or a point geo-feature's location (an earthquake
        epicentre, a FIRMS pixel; M5 environmental alerts, USGS-FR-005). A record with no
        usable point (a track without geometry, or a point-less feature) makes those
        leaves unevaluable (can't test containment/distance/elevation without a position).
        Each operator then dispatches to the pure :mod:`aether.alerts.geo` predicates
        against the synced geofence / station.
        """
        if op == "geofence_intersects":
            return self._eval_geofence_intersects(rule, record)
        point = _record_point(record)
        if point is None:
            return False, False
        lon, lat, alt_m = point
        if op in ("entered_geofence", "exited_geofence"):
            return self._eval_geofence(op, rule, lon, lat, alt_m)
        if op in ("distance_below", "distance_above"):
            return self._eval_distance(op, cond, rule, lon, lat)
        if op == "elevation_crossed":
            return self._eval_elevation(cond, lon, lat, alt_m)
        return False, False  # pragma: no cover - exhaustive above

    def _eval_geofence(
        self, op: str, rule: AlertRule, lon: float, lat: float, alt_m: float | None
    ) -> tuple[bool, bool]:
        """Containment level for ``entered_geofence`` / ``exited_geofence``.

        Resolves ``rule.geofence_id`` against the synced set — a ``None`` or absent id
        is unevaluable (a missing fence is "unknown", never "outside"; PRD §37).
        ``entered`` is the containment level (fired via the enter edge); ``exited`` is
        its negation (also fired via the enter edge — see the template note).
        """
        gf = self._geofences.get(rule.geofence_id) if rule.geofence_id is not None else None
        if gf is None:
            return False, False
        contained = geo.geofence_contains(gf, lon, lat, alt_m)
        return (contained if op == "entered_geofence" else not contained), True

    def _eval_geofence_intersects(self, rule: AlertRule, record: Record) -> tuple[bool, bool]:
        """Areal overlap of a geo-feature's polygon with ``rule.geofence_id`` (level, evaluable).

        Unevaluable when the rule names no/absent geofence (a missing fence is "unknown",
        never "no overlap"; PRD §37) or the record has no areal geometry (a point feature
        or a track has no polygon to overlap). Otherwise the feature's exterior rings are
        tested against the synced fence's authoritative shape — the level is True while
        any ring overlaps, so an ``enter`` rule fires once when an intersecting TFR first
        appears and auto-resolves when it ages out (PRD §32 #15)."""
        gf = self._geofences.get(rule.geofence_id) if rule.geofence_id is not None else None
        if gf is None:
            return False, False
        rings = _feature_exterior_rings(record)
        if rings is None:
            return False, False
        return geo.geofence_intersects_rings(gf, rings), True

    def _eval_became_active(self, record: Record, now: datetime) -> tuple[bool, bool]:
        """Temporal level for ``became_active``: is a feature inside its active window now?

        The "becomes active" edge of a time-bounded geo-feature (a TFR now, a NOTAM
        geometry later). The level goes True once ``now`` reaches the feature's
        ``valid_from`` — that rising edge fires under the ``enter`` transition. The
        upper bound needs no test here: when ``valid_until`` passes, the live-state
        sweep removes the feature and the engine auto-resolves the open alert
        (``_on_remove``). Unevaluable for a record with no ``valid_from`` (a track, or
        a TFR with no effective time) — an honest "unknown", never a confident "not
        active yet" (PRD §37).

        No observation arrives *at* ``valid_from`` (a feed may dedupe an unchanged
        revision), so the clock-driven live-state sweep re-drives a feature the moment
        it crosses ``valid_from`` to deliver this rising edge with no new ingest
        (``LiveState.expire``, PRD §32 #16)."""
        if not isinstance(record, GeoFeatureRecord) or record.valid_from is None:
            return False, False
        return record.valid_from <= now, True

    def _eval_distance(
        self, op: str, cond: AlertCondition, rule: AlertRule, lon: float, lat: float
    ) -> tuple[bool, bool]:
        """Distance-below/above level against a geofence center or the station.

        Measures to the geofence reference point when ``rule.geofence_id`` is set and
        present, else to the station. A set-but-absent geofence, or no geofence with
        an unconfigured (null-island) station, is unevaluable. ``below`` is strict
        ``<`` and ``above`` strict ``>``, matching the ``less_than``/``greater_than``
        convention (a point exactly at the threshold is in neither).
        """
        ref = self._reference_for_distance(rule)
        if ref is None:
            return False, False
        threshold = cond.threshold if cond.threshold is not None else 0.0
        dist = geo.haversine_m(lon, lat, ref[0], ref[1])
        return (dist < threshold if op == "distance_below" else dist > threshold), True

    def _reference_for_distance(self, rule: AlertRule) -> Position | None:
        """The point a distance_* leaf measures to: geofence ref, else station, else None."""
        if rule.geofence_id is not None:
            ref = self._refs.get(rule.geofence_id)
            return ref  # None ⇒ geofence_id set but absent → unevaluable
        if not self._station.configured:
            return None
        return [self._station.lon, self._station.lat]

    def _eval_elevation(
        self, cond: AlertCondition, lon: float, lat: float, alt_m: float | None
    ) -> tuple[bool, bool]:
        """Elevation-angle level for ``elevation_crossed`` (vs the station).

        Unevaluable without a configured station or without a track altitude (an
        elevation angle is undefined without height — honest unknown, not 0 deg). The
        angle uses the ground (haversine) distance to the station and the track's
        altitude; ``>= threshold`` matches "crossed/at or above" semantics.
        """
        if not self._station.configured or alt_m is None:
            return False, False
        ground = geo.haversine_m(lon, lat, self._station.lon, self._station.lat)
        angle = geo.elevation_angle_deg(ground, alt_m)
        threshold = cond.threshold if cond.threshold is not None else 0.0
        return angle >= threshold, True

    def _eval_watchlist(self, cond: AlertCondition, record: Record) -> bool:
        """Watchlist membership level for the ``watchlist`` operator.

        Computes the canonical ``watchlist_key`` for ``record`` and checks it against
        the in-memory membership set.  Always evaluable (membership is always answerable
        — the key is either present or not); never touches the store on the hot path.

        ``value`` semantics (mirroring ``local_rf``): omitted/``None`` → desired True
        (the common "is on the watchlist" rule); explicit ``True`` → same; explicit
        ``False`` → matches a record NOT on the watchlist.

        The leaf's ``field`` (required min_length≥1; convention: set to ``"watchlist"``)
        is intentionally IGNORED — membership is identity-derived, not a record field.
        Non-track records return ``None`` from ``watchlist_key`` and are never members,
        so a ``watchlist:true`` leaf is False for features/events (consistent).
        """
        key = watchlist_key(record)
        is_member = key is not None and key in self._watchlist
        desired = True if cond.value is None else bool(cond.value)
        return is_member == desired

    def _commit_changed(self, rule: AlertRule, state: _SubjectState, dump: dict[str, Any]) -> None:
        """Advance every changed_* leaf's baseline to the current resolved value.

        Runs on every *evaluable* tick (independent of the AND outcome / fire), so a
        tick the rule didn't fire still moves the baseline forward — a later flip is
        detected against the most recent value, never a stale one. An *unevaluable*
        tick is skipped by the caller, so the baseline only tracks ticks the rule could
        actually be judged on.
        """
        for cond in rule.conditions:
            if cond.operator in _CHANGED_OPERATORS:
                state.prior_values[cond.field] = resolve_field(dump, cond.field)

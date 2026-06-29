"""Contextual alert operators driven through the engine (M4.6c, PRD §20.2/§12 #6/#7).

Each operator is exercised end-to-end through :class:`aether.alerts.engine.AlertEngine`
— not the evaluator in isolation — so the test pins the *whole* firing model: the
contextual evaluator collapses a rule to a ``(level, discrete)`` and the engine's
existing transition/cooldown/dedup/auto-resolve path produces the alerts. An injected
clock + deterministic id factory keep edges/cooldown deterministic; records are real
schema-v2 tracks/events (dumped through ``dump_record`` like the condition core).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from aether.alerts import contextual
from aether.alerts.contextual import ContextualEvaluator, StationRef
from aether.alerts.engine import AlertEngine
from aether.schema.alert_rule import AlertCondition, AlertRule, AlertRuleCreate
from aether.schema.geofence import CircleShape, Geofence, GeofenceCreate
from aether.schema.geometry import MultiPolygon, Point, Polygon
from aether.schema.records import EventRecord, GeoFeatureRecord, Record, TrackRecord
from aether.state.live import StateChange

T0 = datetime(2026, 6, 19, 12, 0, 0, tzinfo=UTC)

# A station near the demo aircraft cluster, so station-relative leaves have a real
# reference (0,0 would be unconfigured → unevaluable).
STATION_LON, STATION_LAT = -95.0, 40.0


class _Clock:
    def __init__(self, t: datetime) -> None:
        self.t = t

    def __call__(self) -> datetime:
        return self.t


def _id_factory() -> Any:
    counter = {"n": 0}

    def make() -> str:
        counter["n"] += 1
        return f"alert-{counter['n']}"

    return make


def _engine(
    clock: _Clock,
    rules: list[AlertRule],
    *,
    geofences: list[Geofence] | None = None,
    station_lat: float = STATION_LAT,
    station_lon: float = STATION_LON,
) -> AlertEngine:
    engine = AlertEngine(
        clock=clock, id_factory=_id_factory(), station_lat=station_lat, station_lon=station_lon
    )
    engine.set_rules(rules)
    if geofences:
        engine.set_geofences(geofences)
    return engine


def _rule(*, id: str = "rule-x", **kw: Any) -> AlertRule:
    body = AlertRuleCreate(
        name=kw.pop("name", "Test rule"),
        severity=kw.pop("severity", "high"),
        subject_types=kw.pop("subject_types", ["aircraft"]),
        conditions=kw.pop("conditions", []),
        channels=kw.pop("channels", ["dashboard"]),
        **kw,
    )
    return AlertRule.create(body, id=id, now=T0)


def _circle(id: str = "gf-ring", *, center: tuple[float, float] = (-95.0, 40.0)) -> Geofence:
    return Geofence.create(
        GeofenceCreate(name="ring", shape=CircleShape(center=list(center), radius_m=5000.0)),
        id=id,
        now=T0,
    )


def _track(
    lon: float,
    lat: float,
    *,
    alt_m: float | None = 3000.0,
    geometry: bool = True,
    id: str = "aircraft:icao:abc",
    attrs: dict[str, Any] | None = None,
) -> TrackRecord:
    return TrackRecord(
        id=id,
        source="local_adsb",
        observed_at=T0,
        received_at=T0,
        published_at=T0,
        correlation_key=id,
        track_type="aircraft",
        geometry=Point(coordinates=[lon, lat, alt_m if alt_m is not None else 0.0])
        if geometry
        else None,
        altitude_m=alt_m,
        locally_received=True,
        attributes=attrs or {},
    )


def _quake(
    lon: float,
    lat: float,
    *,
    magnitude: float = 5.0,
    id: str = "earthquake:usgs:nc1",
) -> GeoFeatureRecord:
    return GeoFeatureRecord(
        id=id,
        source="usgs",
        observed_at=T0,
        received_at=T0,
        published_at=T0,
        correlation_key=id,
        feature_type="earthquake",
        geometry=Point(coordinates=[lon, lat]),
        attributes={"magnitude": magnitude},
    )


def _box_ring(west: float, south: float, east: float, north: float) -> list[list[float]]:
    return [[west, south], [east, south], [east, north], [west, north], [west, south]]


def _tfr(
    geometry: Polygon | MultiPolygon,
    *,
    id: str = "tfr:faa:6_9513",
) -> GeoFeatureRecord:
    return GeoFeatureRecord(
        id=id,
        source="faa_tfr",
        observed_at=T0,
        received_at=T0,
        published_at=T0,
        correlation_key=id,
        feature_type="tfr",
        geometry=geometry,
        label="Test TFR",
    )


def _change(record: Record, *, op: str | None = None) -> StateChange:
    if isinstance(record, TrackRecord):
        kind, op = "track", op or "upsert"
    elif isinstance(record, GeoFeatureRecord):
        kind, op = "feature", op or "upsert"
    elif isinstance(record, EventRecord):
        kind, op = "event", op or "event"
    else:  # pragma: no cover
        raise AssertionError(record)
    return StateChange(seq=1, op=op, kind=kind, id=record.id, record=record)  # type: ignore[arg-type]


def _event(*, attrs: dict[str, Any], id: str = "evt-1") -> EventRecord:
    return EventRecord(
        id=id,
        source="local_adsb",
        observed_at=T0,
        received_at=T0,
        published_at=T0,
        event_type="aircraft",
        subject_id="aircraft:icao:abc",
        summary="x",
        attributes=attrs,
    )


# --- geofence enter / exit ----------------------------------------------------


def test_entered_geofence_fires_once_dedups_and_auto_resolves() -> None:
    clock = _Clock(T0)
    rule = _rule(
        conditions=[AlertCondition(field="geometry", operator="entered_geofence")],
        geofence_id="gf-ring",
        transition="enter",
    )
    engine = _engine(clock, [rule], geofences=[_circle()])

    out = engine.evaluate(_change(_track(-95.0, 40.0)))  # inside
    assert len(out) == 1 and out[0].state == "open"
    assert engine.evaluate(_change(_track(-95.0, 40.0))) == []  # still inside → dedup

    clock.t = T0 + timedelta(seconds=30)
    resolved = engine.evaluate(_change(_track(-90.0, 40.0)))  # left
    assert len(resolved) == 1 and resolved[0].state == "resolved"

    # A second, never-inside track does not fire.
    assert engine.evaluate(_change(_track(-80.0, 40.0, id="aircraft:icao:other"))) == []


def test_exited_geofence_fires_when_track_leaves() -> None:
    clock = _Clock(T0)
    rule = _rule(
        conditions=[AlertCondition(field="geometry", operator="exited_geofence")],
        geofence_id="gf-ring",
        transition="enter",  # the exited level is negated containment; enter rides it
    )
    engine = _engine(clock, [rule], geofences=[_circle()])
    # Inside → exited level False → no fire.
    assert engine.evaluate(_change(_track(-95.0, 40.0))) == []
    # Outside → exited level True → fires.
    out = engine.evaluate(_change(_track(-90.0, 40.0)))
    assert len(out) == 1 and out[0].state == "open"


# --- areal TFR intersection (geofence_intersects, PRD §32 #15) ----------------


def _tfr_rule(*, geofence_id: str | None = "gf-ring") -> AlertRule:
    return _rule(
        subject_types=["tfr"],
        conditions=[AlertCondition(field="geometry", operator="geofence_intersects")],
        geofence_id=geofence_id,
        transition="enter",
    )


def test_geofence_intersects_tfr_fires_once_and_auto_resolves() -> None:
    clock = _Clock(T0)
    engine = _engine(clock, [_tfr_rule()], geofences=[_circle()])  # circle at (-95, 40)
    tfr = _tfr(Polygon(coordinates=[_box_ring(-95.2, 39.9, -94.8, 40.1)]))  # straddles the fence

    out = engine.evaluate(_change(tfr))
    assert len(out) == 1 and out[0].state == "open"
    # A revision re-publish (same id, still overlapping) dedups against the open alert.
    assert engine.evaluate(_change(tfr)) == []

    # The TFR ages out of live state → feature remove → the open alert auto-resolves.
    clock.t = T0 + timedelta(minutes=5)
    resolved = engine.evaluate(
        StateChange(seq=2, op="remove", kind="feature", id=tfr.id, record=None)
    )
    assert len(resolved) == 1 and resolved[0].state == "resolved"


def test_geofence_intersects_disjoint_tfr_does_not_fire() -> None:
    clock = _Clock(T0)
    engine = _engine(clock, [_tfr_rule()], geofences=[_circle()])
    far = _tfr(Polygon(coordinates=[_box_ring(-80.2, 39.9, -79.8, 40.1)]))  # ~1150 km east
    assert engine.evaluate(_change(far)) == []


def test_geofence_intersects_multipolygon_one_area_overlaps() -> None:
    clock = _Clock(T0)
    engine = _engine(clock, [_tfr_rule()], geofences=[_circle()])
    # One area far away, one straddling the fence — the rule fires on the overlapping area.
    geom = MultiPolygon(
        coordinates=[
            [_box_ring(-80.2, 39.9, -79.8, 40.1)],
            [_box_ring(-95.2, 39.9, -94.8, 40.1)],
        ]
    )
    out = engine.evaluate(_change(_tfr(geom)))
    assert len(out) == 1 and out[0].state == "open"


def test_geofence_intersects_unevaluable_without_geofence() -> None:
    # An overlapping TFR but the rule names no (or an absent) geofence → unevaluable, so
    # the engine does nothing — never a phantom overlap against a missing fence (PRD §37).
    clock = _Clock(T0)
    overlapping = _tfr(Polygon(coordinates=[_box_ring(-95.2, 39.9, -94.8, 40.1)]))

    no_fence = _engine(clock, [_tfr_rule(geofence_id=None)], geofences=[_circle()])
    assert no_fence.evaluate(_change(overlapping)) == []

    absent = _engine(clock, [_tfr_rule(geofence_id="gf-missing")], geofences=[_circle()])
    assert absent.evaluate(_change(overlapping)) == []


def test_geofence_intersects_unevaluable_for_point_feature() -> None:
    # geofence_intersects is areal: a point feature (a quake) has no polygon, so the leaf
    # is an honest unknown (no fire), not a False that would masquerade as "no overlap".
    clock = _Clock(T0)
    rule = _rule(
        subject_types=["earthquake"],
        conditions=[AlertCondition(field="geometry", operator="geofence_intersects")],
        geofence_id="gf-ring",
        transition="enter",
    )
    engine = _engine(clock, [rule], geofences=[_circle()])
    assert engine.evaluate(_change(_quake(STATION_LON, STATION_LAT))) == []  # on the fence center


# --- TFR activation (became_active, PRD §32 #16) ------------------------------


def _timed_tfr(
    *,
    valid_from: datetime | None,
    valid_until: datetime | None = None,
    id: str = "tfr:faa:6_9513",
) -> GeoFeatureRecord:
    return GeoFeatureRecord(
        id=id,
        source="faa_tfr",
        observed_at=T0,
        received_at=T0,
        published_at=T0,
        correlation_key=id,
        feature_type="tfr",
        geometry=Polygon(coordinates=[_box_ring(-95.2, 39.9, -94.8, 40.1)]),
        valid_from=valid_from,
        valid_until=valid_until,
        label="Test TFR",
    )


def _became_active_rule() -> AlertRule:
    return _rule(
        subject_types=["tfr"],
        conditions=[AlertCondition(field="valid_from", operator="became_active")],
        transition="enter",
    )


def test_became_active_fires_when_clock_crosses_valid_from() -> None:
    clock = _Clock(T0)
    engine = _engine(clock, [_became_active_rule()])
    tfr = _timed_tfr(valid_from=T0 + timedelta(minutes=10), valid_until=T0 + timedelta(hours=2))

    # Ingested while pending → level False → no fire, but the baseline is established.
    assert engine.evaluate(_change(tfr)) == []

    # The live-state sweep re-drives the unchanged TFR once now ≥ valid_from → fires.
    clock.t = T0 + timedelta(minutes=10)
    out = engine.evaluate(_change(tfr))
    assert len(out) == 1 and out[0].state == "open"

    # A further re-drive while still active dedups against the still-open alert.
    clock.t = T0 + timedelta(minutes=11)
    assert engine.evaluate(_change(tfr)) == []

    # The TFR ages out (valid_until) → feature remove → the open alert auto-resolves.
    resolved = engine.evaluate(
        StateChange(seq=2, op="remove", kind="feature", id=tfr.id, record=None)
    )
    assert len(resolved) == 1 and resolved[0].state == "resolved"


def test_became_active_fires_on_first_sight_already_active() -> None:
    # Decided semantics: a TFR first received already inside its active window fires on
    # first sight (active-now), not only on an observed pending→active transition.
    clock = _Clock(T0)
    engine = _engine(clock, [_became_active_rule()])
    already = _timed_tfr(valid_from=T0 - timedelta(minutes=5), valid_until=T0 + timedelta(hours=1))
    out = engine.evaluate(_change(already))
    assert len(out) == 1 and out[0].state == "open"


def test_became_active_unevaluable_without_valid_from() -> None:
    # A TFR with no parsed effective time is an honest unknown — never a confident
    # "active" (no fire), and the engine does nothing (PRD §37).
    clock = _Clock(T0)
    engine = _engine(clock, [_became_active_rule()])
    assert engine.evaluate(_change(_timed_tfr(valid_from=None))) == []


def test_became_active_unevaluable_for_track() -> None:
    # A track carries no validity window, so the operator is unevaluable for it — the
    # rule never fires on a non-feature subject even if mistakenly pointed at one.
    clock = _Clock(T0)
    rule = _rule(
        subject_types=["aircraft"],
        conditions=[AlertCondition(field="valid_from", operator="became_active")],
        transition="enter",
    )
    engine = _engine(clock, [rule])
    assert engine.evaluate(_change(_track(STATION_LON, STATION_LAT))) == []


# --- distance -----------------------------------------------------------------


def test_distance_below_against_station() -> None:
    clock = _Clock(T0)
    rule = _rule(
        conditions=[AlertCondition(field="geometry", operator="distance_below", threshold=10000.0)],
        transition="enter",
    )
    engine = _engine(clock, [rule])
    # On the station → distance 0 < 10 km → fires.
    out = engine.evaluate(_change(_track(STATION_LON, STATION_LAT)))
    assert len(out) == 1 and out[0].state == "open"


def test_distance_below_fires_on_earthquake_feature() -> None:
    # A point geo-feature (earthquake) reaches the SAME geometry path a track does, so
    # an operator can alert on a quake within N metres of the station (USGS-FR-005).
    clock = _Clock(T0)
    rule = _rule(
        subject_types=["earthquake"],
        conditions=[AlertCondition(field="geometry", operator="distance_below", threshold=10000.0)],
        transition="enter",
    )
    engine = _engine(clock, [rule])
    out = engine.evaluate(_change(_quake(STATION_LON, STATION_LAT)))  # on the station
    assert len(out) == 1 and out[0].state == "open"
    # A quake well outside the radius does not fire.
    assert engine.evaluate(_change(_quake(-80.0, 40.0, id="earthquake:usgs:far"))) == []


def test_elevation_crossed_unevaluable_for_feature() -> None:
    # A feature has no altitude — an elevation angle is undefined — so the leaf is an
    # honest unknown (no fire), never a bogus 0 deg (PRD §37).
    clock = _Clock(T0)
    rule = _rule(
        subject_types=["earthquake"],
        conditions=[AlertCondition(field="geometry", operator="elevation_crossed", threshold=10.0)],
        transition="enter",
    )
    engine = _engine(clock, [rule])
    assert engine.evaluate(_change(_quake(STATION_LON, STATION_LAT))) == []


def test_record_point_extraction() -> None:
    from aether.schema.geometry import Polygon

    # Track with a point → its coordinates + altitude.
    assert contextual._record_point(_track(-95.0, 40.0, alt_m=3000.0)) == (-95.0, 40.0, 3000.0)
    # Track without geometry → None (unevaluable).
    assert contextual._record_point(_track(-95.0, 40.0, geometry=False)) is None
    # Point feature → coordinates with no altitude.
    assert contextual._record_point(_quake(-95.0, 40.0)) == (-95.0, 40.0, None)
    # Non-point feature (a polygon TFR-like) → None: areal distance lands with that slice.
    polygon_feature = GeoFeatureRecord(
        id="tfr:1",
        source="faa",
        observed_at=T0,
        received_at=T0,
        published_at=T0,
        correlation_key="tfr:1",
        feature_type="tfr",
        geometry=Polygon(
            coordinates=[[[-95.0, 40.0], [-94.0, 40.0], [-94.0, 41.0], [-95.0, 40.0]]]
        ),
    )
    assert contextual._record_point(polygon_feature) is None


def test_distance_above_against_geofence_center() -> None:
    clock = _Clock(T0)
    rule = _rule(
        conditions=[AlertCondition(field="geometry", operator="distance_above", threshold=50000.0)],
        geofence_id="gf-ring",
        transition="enter",
    )
    engine = _engine(clock, [rule], geofences=[_circle(center=(-95.0, 40.0))])
    # Far from the geofence center (~85 km east) → above 50 km → fires.
    out = engine.evaluate(_change(_track(-94.0, 40.0)))
    assert len(out) == 1 and out[0].state == "open"


# --- elevation ----------------------------------------------------------------


def test_elevation_crossed_fires_above_threshold() -> None:
    clock = _Clock(T0)
    rule = _rule(
        conditions=[AlertCondition(field="geometry", operator="elevation_crossed", threshold=80.0)],
        transition="enter",
    )
    engine = _engine(clock, [rule])
    # Nearly overhead the station at altitude → high elevation angle → fires.
    out = engine.evaluate(_change(_track(STATION_LON, STATION_LAT, alt_m=5000.0)))
    assert len(out) == 1 and out[0].state == "open"


def test_elevation_unevaluable_without_altitude() -> None:
    clock = _Clock(T0)
    rule = _rule(
        conditions=[AlertCondition(field="geometry", operator="elevation_crossed", threshold=10.0)],
        transition="enter",
    )
    engine = _engine(clock, [rule])
    assert engine.evaluate(_change(_track(STATION_LON, STATION_LAT, alt_m=None))) == []


# --- count within window ------------------------------------------------------


def test_count_within_window_reaches_n_then_drains() -> None:
    clock = _Clock(T0)
    rule = _rule(
        conditions=[
            AlertCondition(field="id", operator="count_within_window", threshold=3.0, window_s=60.0)
        ],
        transition="enter",
    )
    engine = _engine(clock, [rule])
    assert engine.evaluate(_change(_track(-95.0, 40.0))) == []  # count 1
    assert engine.evaluate(_change(_track(-95.0, 40.0))) == []  # count 2
    out = engine.evaluate(_change(_track(-95.0, 40.0)))  # count 3 → fires
    assert len(out) == 1 and out[0].state == "open"

    # Let the window drain (advance past window_s with no new hits) → auto-resolve.
    clock.t = T0 + timedelta(seconds=120)
    resolved = engine.evaluate(_change(_track(-95.0, 40.0)))  # only this one in window → 1 < 3
    assert len(resolved) == 1 and resolved[0].state == "resolved"


def test_count_within_window_counts_only_qualifying_observations() -> None:
    # count ANDed with a stateless equals leaf: only observations that ALSO satisfy the
    # equals leaf count toward the window (qualifying observations, not raw ticks).
    clock = _Clock(T0)
    rule = _rule(
        conditions=[
            AlertCondition(field="attributes.squawk", operator="equals", value="7700"),
            AlertCondition(
                field="id", operator="count_within_window", threshold=2.0, window_s=60.0
            ),
        ],
        transition="enter",
        cooldown_s=0.0,
    )
    engine = _engine(clock, [rule])
    # Non-qualifying squawk does not advance the count (raw counting would have).
    assert engine.evaluate(_change(_track(-95.0, 40.0, attrs={"squawk": "1200"}))) == []
    assert engine.evaluate(_change(_track(-95.0, 40.0, attrs={"squawk": "1200"}))) == []
    assert engine.evaluate(_change(_track(-95.0, 40.0, attrs={"squawk": "7700"}))) == []  # qual 1<2
    out = engine.evaluate(
        _change(_track(-95.0, 40.0, attrs={"squawk": "7700"}))
    )  # qual 2≥2 → fires
    assert len(out) == 1 and out[0].state == "open"


def test_count_state_unpolluted_by_unevaluable_ticks() -> None:
    # count ANDed with a geofence leaf: while the geofence is unsynced the rule is
    # unevaluable, and those ticks neither fire nor pollute the window (finding-E
    # hygiene). Once the fence is synced, only the qualifying (inside) sightings count.
    clock = _Clock(T0)
    rule = _rule(
        conditions=[
            AlertCondition(field="geometry", operator="entered_geofence"),
            AlertCondition(
                field="id", operator="count_within_window", threshold=2.0, window_s=60.0
            ),
        ],
        geofence_id="gf-ring",
        transition="enter",
        cooldown_s=0.0,
    )
    engine = _engine(clock, [rule])  # no geofence synced yet → unevaluable
    assert engine.evaluate(_change(_track(-95.0, 40.0))) == []
    assert engine.evaluate(_change(_track(-95.0, 40.0))) == []
    engine.set_geofences([_circle()])  # now inside-observations qualify and count
    assert engine.evaluate(_change(_track(-95.0, 40.0))) == []  # qualifying count 1 < 2
    out = engine.evaluate(_change(_track(-95.0, 40.0)))  # count 2 ≥ 2 → fires
    assert len(out) == 1 and out[0].state == "open"


# --- changed_to / changed_from ------------------------------------------------


def test_changed_to_suppresses_first_obs_then_fires_on_transition() -> None:
    clock = _Clock(T0)
    rule = _rule(
        conditions=[AlertCondition(field="attributes.squawk", operator="changed_to", value="7700")],
        transition="change",
        cooldown_s=0.0,
    )
    engine = _engine(clock, [rule])
    # First observation, already at 7700 → suppressed (initial sighting is not a change).
    assert engine.evaluate(_change(_track(-95.0, 40.0, attrs={"squawk": "1200"}))) == []
    # A real transition 1200 → 7700 fires (discrete).
    out = engine.evaluate(_change(_track(-95.0, 40.0, attrs={"squawk": "7700"})))
    assert len(out) == 1 and out[0].state == "open"


def test_changed_from_fires_when_leaving_value() -> None:
    clock = _Clock(T0)
    rule = _rule(
        conditions=[
            AlertCondition(field="attributes.squawk", operator="changed_from", value="7700")
        ],
        transition="change",
        cooldown_s=0.0,
    )
    engine = _engine(clock, [rule])
    assert engine.evaluate(_change(_track(-95.0, 40.0, attrs={"squawk": "7700"}))) == []  # first
    out = engine.evaluate(_change(_track(-95.0, 40.0, attrs={"squawk": "1200"})))  # left 7700
    assert len(out) == 1 and out[0].state == "open"


# --- watchlist (membership-derived, PRD §24.6/§21.5) --------------------------
# These drive the engine end-to-end and are the regression guard for the
# *always-falsy* bug: before M6.6b the ``watchlist`` operator read a record field no
# adapter ever set (``attributes.watchlist``), so a watchlist rule never fired. The
# operator now lives in the contextual evaluator and matches on the canonical
# ``watchlist_key`` against the engine's synced membership set, so it actually fires.


def _watchlist_rule(*, value: bool | None = True, **kw: Any) -> AlertRule:
    return _rule(
        subject_types=kw.pop("subject_types", ["aircraft"]),
        conditions=[AlertCondition(field="watchlist", operator="watchlist", value=value)],
        transition="enter",
        **kw,
    )


def _network_track(lon: float, lat: float, *, id: str, alt_m: float = 3000.0) -> TrackRecord:
    """A non-local (Internet-feed) aircraft track — ``locally_received=False`` — for
    the combined watchlist AND local_rf case (the helper ``_track`` is always local)."""
    return TrackRecord(
        id=id,
        source="net_adsb",
        observed_at=T0,
        received_at=T0,
        published_at=T0,
        correlation_key=id,
        track_type="aircraft",
        geometry=Point(coordinates=[lon, lat, alt_m]),
        altitude_m=alt_m,
        locally_received=False,
    )


def test_watchlist_member_fires_and_dedups() -> None:
    # REGRESSION (always-falsy bug): a watchlisted track now produces exactly one open
    # alert, and a repeat observation dedups against it (continuous level, enter edge).
    clock = _Clock(T0)
    engine = _engine(clock, [_watchlist_rule()])
    engine.set_watchlist({"aircraft:icao:abc123"})
    track = _track(-95.0, 40.0, id="aircraft:icao:abc123")

    out = engine.evaluate(_change(track))
    assert len(out) == 1 and out[0].state == "open"
    assert engine.evaluate(_change(track)) == []  # still on the list → dedup


def test_watchlist_non_member_does_not_fire() -> None:
    clock = _Clock(T0)
    engine = _engine(clock, [_watchlist_rule()])
    engine.set_watchlist({"aircraft:icao:abc123"})
    # A different identity, not on the watchlist → no fire (membership is identity-keyed).
    assert engine.evaluate(_change(_track(-95.0, 40.0, id="aircraft:icao:def456"))) == []


def test_watchlist_upsert_adds_membership_then_fires() -> None:
    # Mirrors the API PUT path: engine.upsert_watchlist makes a previously-silent track
    # fire on its next observation (level rises False→True under the enter edge).
    clock = _Clock(T0)
    engine = _engine(clock, [_watchlist_rule()])
    track = _track(-95.0, 40.0, id="aircraft:icao:abc123")
    assert engine.evaluate(_change(track)) == []  # not yet a member
    engine.upsert_watchlist("aircraft:icao:abc123")
    out = engine.evaluate(_change(track))
    assert len(out) == 1 and out[0].state == "open"


def test_watchlist_value_false_fires_for_non_member_only() -> None:
    # value:false inverts the desired membership ("alert on anything NOT on my list").
    clock = _Clock(T0)
    engine = _engine(clock, [_watchlist_rule(value=False)])
    engine.set_watchlist({"aircraft:icao:abc123"})
    # Member → desired False but is_member True → no match.
    assert engine.evaluate(_change(_track(-95.0, 40.0, id="aircraft:icao:abc123"))) == []
    # Non-member → desired False, is_member False → matches → fires.
    out = engine.evaluate(_change(_track(-95.0, 40.0, id="aircraft:icao:def456")))
    assert len(out) == 1 and out[0].state == "open"


def test_watchlist_removal_auto_resolves_open_alert() -> None:
    # Mirrors the API DELETE path: removing the key drops the level True→False, so the
    # open alert auto-resolves on the next observation (the continuous-level closing edge).
    clock = _Clock(T0)
    engine = _engine(clock, [_watchlist_rule()])
    engine.set_watchlist({"aircraft:icao:abc123"})
    track = _track(-95.0, 40.0, id="aircraft:icao:abc123")
    out = engine.evaluate(_change(track))
    assert len(out) == 1 and out[0].state == "open"

    engine.remove_watchlist("aircraft:icao:abc123")
    clock.t = T0 + timedelta(seconds=30)
    resolved = engine.evaluate(_change(track))
    assert len(resolved) == 1 and resolved[0].state == "resolved"


def test_watchlist_combined_with_local_rf_fires_only_for_watchlisted_local() -> None:
    # watchlist:true AND local_rf:true — both leaves ANDed in the same contextual rule.
    # Fires only for a track that is BOTH on the watchlist AND received by my own radio;
    # a watchlisted Internet-only track, or a local track not on the list, does not.
    clock = _Clock(T0)
    rule = _rule(
        conditions=[
            AlertCondition(field="watchlist", operator="watchlist", value=True),
            AlertCondition(field="locally_received", operator="local_rf", value=True),
        ],
        transition="enter",
    )
    engine = _engine(clock, [rule])
    engine.set_watchlist({"aircraft:icao:abc123", "aircraft:icao:def456"})

    # Watchlisted + locally received (distinct subjects keep firing state independent).
    fires = engine.evaluate(_change(_track(-95.0, 40.0, id="aircraft:icao:abc123")))
    assert len(fires) == 1 and fires[0].state == "open"
    # Watchlisted but Internet-only (locally_received=False) → local_rf leaf fails.
    assert engine.evaluate(_change(_network_track(-95.0, 40.0, id="aircraft:icao:def456"))) == []
    # Locally received but not on the watchlist → watchlist leaf fails.
    assert engine.evaluate(_change(_track(-95.0, 40.0, id="aircraft:icao:ghi789"))) == []


def test_watchlist_non_track_subject_never_member() -> None:
    # A non-track subject (an earthquake feature) has no watchlist_key → never a member,
    # so a watchlist:true rule pointed at a feature type stays silent (consistent, no crash).
    clock = _Clock(T0)
    rule = _watchlist_rule(subject_types=["earthquake"])
    engine = _engine(clock, [rule])
    engine.set_watchlist({"earthquake:usgs:nc1"})  # even if the corr key is "on the list"
    # value:true on a non-track → key is None → not a member → no fire.
    assert engine.evaluate(_change(_quake(STATION_LON, STATION_LAT))) == []


def _orbital(elevation_deg: float, *, norad: int = 25544) -> TrackRecord:
    """A minimal SGP4-PREDICTED orbital_object track: only the fields the satellite-rise
    rule reads are load-bearing — ``attributes.elevation_deg`` (greater_than leaf) and
    ``correlation_key`` (watchlist leaf). Mirrors celestrak.py: id == correlation_key ==
    ``orbital:celestrak:<norad>`` and predicted=True."""
    rid = f"orbital:celestrak:{norad}"
    return TrackRecord(
        id=rid,
        source="celestrak",
        observed_at=T0,
        received_at=T0,
        published_at=T0,
        correlation_key=rid,
        track_type="orbital_object",
        geometry=Point(coordinates=[-95.0, 40.0]),
        altitude_m=420_000.0,
        locally_received=False,
        predicted=True,
        attributes={"elevation_deg": elevation_deg, "norad_id": norad},
    )


def _satellite_rise_rule(**kw: Any) -> AlertRule:
    # Same condition shape as the shipped rule-satellite-rise template.
    return _rule(
        subject_types=["orbital_object"],
        conditions=[
            AlertCondition(field="attributes.elevation_deg", operator="greater_than", value=10.0),
            AlertCondition(field="watchlist", operator="watchlist"),
        ],
        transition="enter",
        **kw,
    )


def test_satellite_rise_fires_once_then_auto_resolves() -> None:
    # A WATCHED satellite whose SGP4 elevation rises below->above 10 deg fires exactly
    # once (rising edge), dedups while above, and auto-resolves when it sets back below.
    clock = _Clock(T0)
    engine = _engine(clock, [_satellite_rise_rule()])
    engine.set_watchlist({"orbital:celestrak:25544"})

    assert engine.evaluate(_change(_orbital(3.0))) == []  # below floor → level False baseline
    out = engine.evaluate(_change(_orbital(15.0)))  # crosses up through 10 deg → rising edge
    assert len(out) == 1 and out[0].state == "open"
    assert engine.evaluate(_change(_orbital(40.0))) == []  # still up → dedup against open alert

    clock.t = T0 + timedelta(minutes=5)
    resolved = engine.evaluate(_change(_orbital(2.0)))  # sets below → level True->False
    assert len(resolved) == 1 and resolved[0].state == "resolved"


def test_satellite_rise_auto_resolves_when_satellite_drops_out_of_live_state() -> None:
    # Real-world set path: the CelesTrak adapter stops emitting a sat below the display
    # floor, so the track is removed from live state → _on_remove auto-resolves the alert.
    clock = _Clock(T0)
    engine = _engine(clock, [_satellite_rise_rule()])
    engine.set_watchlist({"orbital:celestrak:25544"})

    out = engine.evaluate(_change(_orbital(15.0)))
    assert len(out) == 1 and out[0].state == "open"

    clock.t = T0 + timedelta(minutes=5)
    resolved = engine.evaluate(_change(_orbital(15.0), op="remove"))
    assert len(resolved) == 1 and resolved[0].state == "resolved"


def test_satellite_rise_does_not_fire_for_unwatched_satellite() -> None:
    # High elevation but a DIFFERENT norad on the watchlist → watchlist leaf False → inert.
    clock = _Clock(T0)
    engine = _engine(clock, [_satellite_rise_rule()])
    engine.set_watchlist({"orbital:celestrak:99999"})
    assert engine.evaluate(_change(_orbital(40.0, norad=25544))) == []


# --- unevaluable degradation --------------------------------------------------


def test_unknown_geofence_id_does_not_fire() -> None:
    clock = _Clock(T0)
    rule = _rule(
        conditions=[AlertCondition(field="geometry", operator="entered_geofence")],
        geofence_id="gf-missing",
        transition="enter",
    )
    engine = _engine(clock, [rule], geofences=[_circle()])  # different id synced
    assert engine.evaluate(_change(_track(-95.0, 40.0))) == []


def test_geometry_leaf_unevaluable_without_geometry() -> None:
    clock = _Clock(T0)
    rule = _rule(
        conditions=[AlertCondition(field="geometry", operator="distance_below", threshold=1e9)],
        transition="enter",
    )
    engine = _engine(clock, [rule])
    assert engine.evaluate(_change(_track(0.0, 0.0, geometry=False))) == []


def test_distance_unevaluable_at_null_island_station() -> None:
    clock = _Clock(T0)
    rule = _rule(
        conditions=[AlertCondition(field="geometry", operator="distance_below", threshold=1e9)],
        transition="enter",
    )
    engine = _engine(clock, [rule], station_lat=0.0, station_lon=0.0)
    assert engine.evaluate(_change(_track(-95.0, 40.0))) == []


def test_event_geometry_leaf_unevaluable() -> None:
    clock = _Clock(T0)
    rule = _rule(
        subject_types=["aircraft"],
        conditions=[AlertCondition(field="geometry", operator="distance_below", threshold=1e9)],
        transition="enter",
    )
    engine = _engine(clock, [rule])
    # An event has no track point → geometry leaf unevaluable → no fire.
    assert engine.evaluate(_change(_event(attrs={}))) == []


# --- pruning ------------------------------------------------------------------


def test_track_remove_forgets_contextual_state() -> None:
    clock = _Clock(T0)
    rule = _rule(
        conditions=[AlertCondition(field="attributes.squawk", operator="changed_to", value="7700")],
        transition="change",
        cooldown_s=0.0,
    )
    engine = _engine(clock, [rule])
    engine.evaluate(_change(_track(-95.0, 40.0, attrs={"squawk": "1200"})))  # first, baseline set
    engine.evaluate(StateChange(1, "remove", "track", "aircraft:icao:abc", None))
    # After forgetting, the next observation is again a first_obs → suppressed even
    # though it arrives directly as 7700 (no remembered 1200 baseline).
    assert engine.evaluate(_change(_track(-95.0, 40.0, attrs={"squawk": "7700"}))) == []


def test_remove_rule_drops_contextual_state() -> None:
    clock = _Clock(T0)
    rule = _rule(
        conditions=[AlertCondition(field="geometry", operator="entered_geofence")],
        geofence_id="gf-ring",
        transition="enter",
    )
    engine = _engine(clock, [rule], geofences=[_circle()])
    engine.evaluate(_change(_track(-95.0, 40.0)))
    engine.remove_rule(rule.id)
    assert engine.rule_count == 0
    assert engine.evaluate(_change(_track(-90.0, 40.0))) == []


def test_upsert_rule_rebaselines_contextual_state() -> None:
    # An edit via upsert re-baselines a rule's contextual state (symmetric with
    # remove_rule/set_rules): the next observation of each subject reads as a fresh
    # first sighting, so a stale prior value can't fire a spurious changed_* transition.
    clock = _Clock(T0)
    rule = _rule(
        conditions=[AlertCondition(field="attributes.squawk", operator="changed_to", value="7700")],
        transition="change",
        cooldown_s=0.0,
    )
    engine = _engine(clock, [rule])
    engine.evaluate(_change(_track(-95.0, 40.0, attrs={"squawk": "1200"})))  # baseline 1200
    engine.upsert_rule(rule)  # an edit → re-baseline
    # The 1200 baseline is gone, so arriving directly at 7700 reads as a first obs.
    assert engine.evaluate(_change(_track(-95.0, 40.0, attrs={"squawk": "7700"}))) == []
    # A genuine later transition (1200 → 7700) still fires.
    engine.evaluate(_change(_track(-95.0, 40.0, attrs={"squawk": "1200"})))
    out = engine.evaluate(_change(_track(-95.0, 40.0, attrs={"squawk": "7700"})))
    assert len(out) == 1 and out[0].state == "open"


def test_contextual_state_is_bounded_by_lru_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    # The per-subject state map is a runaway backstop (PRD §37): at the cap, inserting a
    # new subject evicts the least-recently-used. Patch the cap low to exercise eviction
    # without 50k inserts; re-touching a subject keeps it (LRU, not FIFO).
    monkeypatch.setattr(contextual, "_MAX_SUBJECT_STATES", 2)
    ev = ContextualEvaluator(station=StationRef(lon=STATION_LON, lat=STATION_LAT, configured=True))
    rule = _rule(
        conditions=[
            AlertCondition(field="id", operator="count_within_window", threshold=1.0, window_s=60.0)
        ],
    )
    track = _track(-95.0, 40.0)
    ev.evaluate(rule, "subj-A", track, {}, T0)
    ev.evaluate(rule, "subj-B", track, {}, T0)
    ev.evaluate(rule, "subj-A", track, {}, T0)  # re-touch A → most-recently-used
    ev.evaluate(rule, "subj-C", track, {}, T0)  # over cap → evict the LRU (subj-B)
    assert {key[1] for key in ev._state} == {"subj-A", "subj-C"}

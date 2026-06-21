"""Stateful alert engine (M4.6b, PRD §20.3, §20.5).

Drives :class:`aether.alerts.engine.AlertEngine` with an injected clock and a
deterministic id factory so transition edges, cooldown, dedup, schedule/quiet-hours
gating, and auto-resolution are all exercised without wall-clock flakiness. Records
are built as real schema-v2 models (the engine dumps them through ``dump_record``,
the same contract the condition core is tested against).

T0 (2026-06-19 12:00 UTC) is a **Friday** (``weekday() == 4``) at midday — used by
the schedule/quiet-hours cases below.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from aether.alerts.engine import AlertEngine, preview_rule
from aether.schema.alert_rule import (
    AlertCondition,
    AlertRule,
    AlertRuleCreate,
    Schedule,
    TimeWindow,
)
from aether.schema.geofence import CircleShape, Geofence, GeofenceCreate
from aether.schema.geometry import Point
from aether.schema.records import (
    Classification,
    EventRecord,
    GeoFeatureRecord,
    Record,
    SourceStatusRecord,
    TrackRecord,
)
from aether.state.live import StateChange

T0 = datetime(2026, 6, 19, 12, 0, 0, tzinfo=UTC)  # Friday, midday UTC
MONDAY = datetime(2026, 6, 22, 12, 0, 0, tzinfo=UTC)


class _Clock:
    """A settable callable clock; advance ``t`` to drive cooldown/schedule logic."""

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


def _engine(clock: _Clock, rules: list[AlertRule]) -> AlertEngine:
    engine = AlertEngine(clock=clock, id_factory=_id_factory())
    engine.set_rules(rules)
    return engine


def _squawk_cond() -> AlertCondition:
    return AlertCondition(field="attributes.squawk", operator="equals", value="7700")


def _rule(*, id: str = "rule-x", **kw: Any) -> AlertRule:
    body = AlertRuleCreate(
        name=kw.pop("name", "Test rule"),
        severity=kw.pop("severity", "high"),
        subject_types=kw.pop("subject_types", ["aircraft"]),
        conditions=kw.pop("conditions", [_squawk_cond()]),
        channels=kw.pop("channels", ["dashboard"]),
        **kw,
    )
    return AlertRule.create(body, id=id, now=T0)


def _aircraft(
    *,
    squawk: str | None = None,
    locally_received: bool = True,
    classification: Classification | None = None,
    track_type: str = "aircraft",
    id: str = "aircraft:icao:abc",
) -> TrackRecord:
    return TrackRecord(
        id=id,
        source="local_adsb",
        observed_at=T0,
        received_at=T0,
        published_at=T0,
        correlation_key=id,
        track_type=track_type,  # type: ignore[arg-type]
        locally_received=locally_received,
        classification=classification,
        attributes={"squawk": squawk} if squawk is not None else {},
    )


def _aircraft_at(
    lon: float, lat: float, *, alt_m: float = 3000.0, id: str = "aircraft:icao:abc"
) -> TrackRecord:
    return TrackRecord(
        id=id,
        source="local_adsb",
        observed_at=T0,
        received_at=T0,
        published_at=T0,
        correlation_key=id,
        track_type="aircraft",
        geometry=Point(coordinates=[lon, lat, alt_m]),
        altitude_m=alt_m,
        locally_received=True,
    )


def _source_status(status: str, source: str = "local_adsb") -> SourceStatusRecord:
    return SourceStatusRecord(
        id=f"source_status:{source}",
        source=source,
        observed_at=T0,
        received_at=T0,
        published_at=T0,
        status=status,  # type: ignore[arg-type]
    )


def _quake(
    *,
    magnitude: float | None = 5.0,
    lon: float = -95.0,
    lat: float = 40.0,
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
        label=f"M{magnitude}" if magnitude is not None else "earthquake",
        attributes={"magnitude": magnitude},
    )


def _event(code: str, *, event_type: str = "emergency_squawk", id: str = "evt-1") -> EventRecord:
    return EventRecord(
        id=id,
        source="local_adsb",
        observed_at=T0,
        received_at=T0,
        published_at=T0,
        event_type=event_type,
        subject_id="aircraft:icao:abc",
        summary="emergency squawk",
        attributes={"code": code},
    )


def _change(record: Record, *, op: str | None = None) -> StateChange:
    if isinstance(record, TrackRecord):
        kind, op = "track", op or "upsert"
    elif isinstance(record, GeoFeatureRecord):
        kind, op = "feature", op or "upsert"
    elif isinstance(record, SourceStatusRecord):
        kind, op = "source_status", op or "upsert"
    elif isinstance(record, EventRecord):
        kind, op = "event", op or "event"
    else:  # pragma: no cover - tests only build the above
        raise AssertionError(record)
    return StateChange(seq=1, op=op, kind=kind, id=record.id, record=record)  # type: ignore[arg-type]


# --- enter / level lifecycle -------------------------------------------------


def test_enter_fires_once_then_dedups_while_condition_holds() -> None:
    clock = _Clock(T0)
    rule = _rule(transition="enter")
    engine = _engine(clock, [rule])

    assert engine.evaluate(_change(_aircraft())) == []  # no squawk → no match
    out = engine.evaluate(_change(_aircraft(squawk="7700")))
    assert len(out) == 1
    alert = out[0]
    assert alert.state == "open"
    assert alert.rule_id == rule.id
    assert alert.subject_id == "aircraft:icao:abc"
    assert alert.severity == "high"
    # dashboard is the in-app alert centre: delivered by the alert reaching live
    # state, so the engine stamps it ``delivered`` at creation (no dispatcher needed).
    assert alert.delivery_status == {"dashboard": "delivered"}

    # Still matching → no second alert (the open one dedups).
    assert engine.evaluate(_change(_aircraft(squawk="7700"))) == []


def test_enter_auto_resolves_when_condition_clears() -> None:
    clock = _Clock(T0)
    engine = _engine(clock, [_rule(transition="enter")])
    opened = engine.evaluate(_change(_aircraft(squawk="7700")))[0]

    clock.t = T0 + timedelta(seconds=30)
    resolved = engine.evaluate(_change(_aircraft(squawk=None)))
    assert len(resolved) == 1
    assert resolved[0].id == opened.id  # same alert, now closed
    assert resolved[0].state == "resolved"
    assert resolved[0].resolved_at == clock.t


def test_cooldown_suppresses_refire_across_episodes() -> None:
    clock = _Clock(T0)
    engine = _engine(clock, [_rule(transition="enter", cooldown_s=600.0)])

    assert len(engine.evaluate(_change(_aircraft(squawk="7700")))) == 1  # fire @ T0
    assert engine.evaluate(_change(_aircraft(squawk=None)))[0].state == "resolved"
    # Re-enter inside the cooldown window → suppressed.
    assert engine.evaluate(_change(_aircraft(squawk="7700"))) == []
    engine.evaluate(_change(_aircraft(squawk=None)))  # clear again (no open alert)

    clock.t = T0 + timedelta(seconds=601)  # cooldown elapsed
    assert len(engine.evaluate(_change(_aircraft(squawk="7700")))) == 1


def test_remove_auto_resolves_open_alert_and_forgets_subject() -> None:
    clock = _Clock(T0)
    engine = _engine(clock, [_rule(transition="enter")])
    opened = engine.evaluate(_change(_aircraft(squawk="7700")))[0]

    removed = engine.evaluate(StateChange(1, "remove", "track", "aircraft:icao:abc", None))
    assert len(removed) == 1
    assert removed[0].id == opened.id
    assert removed[0].state == "resolved"


# --- other transition modes --------------------------------------------------


def test_exit_fires_when_condition_stops_holding() -> None:
    clock = _Clock(T0)
    engine = _engine(clock, [_rule(transition="exit")])
    assert engine.evaluate(_change(_aircraft(squawk="7700"))) == []  # enter ≠ exit
    out = engine.evaluate(_change(_aircraft(squawk=None)))
    assert len(out) == 1 and out[0].state == "open"


def test_change_fires_on_every_flip() -> None:
    clock = _Clock(T0)
    engine = _engine(clock, [_rule(transition="change", cooldown_s=0.0)])
    assert len(engine.evaluate(_change(_aircraft(squawk="7700")))) == 1  # False→True
    assert engine.evaluate(_change(_aircraft(squawk="7700"))) == []  # unchanged
    assert len(engine.evaluate(_change(_aircraft(squawk=None)))) == 1  # True→False


def test_event_records_fire_discretely() -> None:
    clock = _Clock(T0)
    rule = _rule(
        subject_types=["emergency_squawk"],
        conditions=[AlertCondition(field="attributes.code", operator="equals", value="7700")],
        cooldown_s=0.0,
    )
    engine = _engine(clock, [rule])
    out = engine.evaluate(_change(_event("7700", id="evt-1")))
    assert len(out) == 1 and out[0].subject_id == "aircraft:icao:abc"
    # A non-matching event does nothing; a second matching one fires again (cd=0).
    assert engine.evaluate(_change(_event("1200", id="evt-2"))) == []
    assert len(engine.evaluate(_change(_event("7700", id="evt-3")))) == 1


# --- dedup, gating, and matching --------------------------------------------


def test_static_dedup_key_collapses_subjects() -> None:
    clock = _Clock(T0)
    engine = _engine(clock, [_rule(transition="enter", dedup_key="squawk-group")])
    a1 = _aircraft(squawk="7700", id="aircraft:icao:a1")
    a2 = _aircraft(squawk="7700", id="aircraft:icao:a2")
    assert len(engine.evaluate(_change(a1))) == 1
    assert engine.evaluate(_change(a2)) == []  # same group already open


def test_schedule_gates_firing_by_day() -> None:
    rule = _rule(transition="enter", schedule=Schedule(days_of_week=[0]))  # Mondays only
    # T0 is a Friday → suppressed.
    assert _engine(_Clock(T0), [rule]).evaluate(_change(_aircraft(squawk="7700"))) == []
    # A Monday → fires.
    assert len(_engine(_Clock(MONDAY), [rule]).evaluate(_change(_aircraft(squawk="7700")))) == 1


def test_quiet_hours_suppresses_firing() -> None:
    rule = _rule(transition="enter", quiet_hours=TimeWindow(start="11:00", end="13:00"))
    # T0 = 12:00 is inside quiet hours → suppressed.
    assert _engine(_Clock(T0), [rule]).evaluate(_change(_aircraft(squawk="7700"))) == []
    # 14:00 is outside → fires.
    after = T0.replace(hour=14)
    assert len(_engine(_Clock(after), [rule]).evaluate(_change(_aircraft(squawk="7700")))) == 1


def test_source_offline_transition_fires() -> None:
    clock = _Clock(T0)
    rule = _rule(
        subject_types=["source"],
        conditions=[AlertCondition(field="status", operator="source_offline")],
        transition="enter",
    )
    engine = _engine(clock, [rule])
    assert engine.evaluate(_change(_source_status("connected"))) == []
    out = engine.evaluate(_change(_source_status("offline")))
    assert len(out) == 1 and out[0].subject_id == "local_adsb"


def test_geofence_enter_rule_fires_via_engine() -> None:
    clock = _Clock(T0)
    rule = _rule(
        conditions=[AlertCondition(field="geometry", operator="entered_geofence")],
        geofence_id="gf-ring",
        transition="enter",
    )
    engine = _engine(clock, [rule])
    engine.set_geofences(
        [
            Geofence.create(
                GeofenceCreate(
                    name="ring", shape=CircleShape(center=[-95.0, 40.0], radius_m=5000.0)
                ),
                id="gf-ring",
                now=T0,
            )
        ]
    )
    # A track inside the fence fires one open alert (contextual level → enter edge,
    # driven through the SAME firing machinery as a stateless rule).
    inside = _aircraft_at(-95.0, 40.0)
    out = engine.evaluate(_change(inside))
    assert len(out) == 1 and out[0].state == "open"
    # Move it well outside → the open alert auto-resolves on the level's closing edge.
    clock.t = T0 + timedelta(seconds=30)
    resolved = engine.evaluate(_change(_aircraft_at(-90.0, 40.0)))
    assert len(resolved) == 1 and resolved[0].state == "resolved"


def test_subject_type_mismatch_and_disabled_rule_do_not_fire() -> None:
    clock = _Clock(T0)
    vessel_rule = _rule(subject_types=["vessel"])
    disabled = _rule(id="rule-off", enabled=False)
    engine = _engine(clock, [vessel_rule, disabled])
    assert engine.evaluate(_change(_aircraft(squawk="7700"))) == []


def test_remove_rule_forgets_its_firings() -> None:
    clock = _Clock(T0)
    rule = _rule(transition="enter")
    engine = _engine(clock, [rule])
    engine.evaluate(_change(_aircraft(squawk="7700")))
    engine.remove_rule(rule.id)
    assert engine.rule_count == 0
    # With the rule gone, nothing evaluates (and no stale firing lingers).
    assert engine.evaluate(_change(_aircraft(squawk=None))) == []


# --- geo-feature (environmental) alerts: earthquakes, USGS-FR-005 -----------


def _quake_rule(**kw: Any) -> AlertRule:
    """A magnitude rule over earthquakes — the M5 environmental-alert default shape."""
    return _rule(
        subject_types=kw.pop("subject_types", ["earthquake"]),
        conditions=kw.pop(
            "conditions",
            [AlertCondition(field="attributes.magnitude", operator="greater_than", value=4.5)],
        ),
        transition=kw.pop("transition", "change"),
        **kw,
    )


def test_subject_type_of_geo_feature_is_its_feature_type() -> None:
    # A rule targets the specific layer ("earthquake"), not the bare "feature" kind —
    # so a quake-magnitude rule never matches a fire/TFR/lightning feature.
    from aether.alerts.engine import subject_type_of

    assert subject_type_of(_quake()) == "earthquake"


def test_earthquake_magnitude_rule_fires_on_feature_change() -> None:
    # A geo-feature now DRIVES the engine (was excluded pre-M5). The ``change``
    # transition makes each new/revised quake a single point-in-time alert.
    clock = _Clock(T0)
    engine = _engine(clock, [_quake_rule()])

    assert engine.evaluate(_change(_quake(magnitude=3.0))) == []  # below M4.5 → no fire
    out = engine.evaluate(_change(_quake(magnitude=5.2, id="earthquake:usgs:big")))
    assert len(out) == 1
    assert out[0].state == "open"
    assert out[0].subject_id == "earthquake:usgs:big"
    assert out[0].severity == "high"


def test_earthquake_change_rule_does_not_refire_same_quake() -> None:
    # The same quake re-upserted (a revision that still matches) is not a *change* in
    # the rule's level, so it fires once, not on every update.
    clock = _Clock(T0)
    engine = _engine(clock, [_quake_rule(cooldown_s=0.0)])
    assert len(engine.evaluate(_change(_quake(magnitude=6.0)))) == 1
    assert engine.evaluate(_change(_quake(magnitude=6.1))) == []  # still above → no new change


def test_subject_type_mismatch_geo_feature_does_not_fire() -> None:
    # A rule scoped to a different feature layer ignores an earthquake.
    clock = _Clock(T0)
    fire_rule = _rule(
        subject_types=["fire_detection"],
        conditions=[
            AlertCondition(field="attributes.magnitude", operator="greater_than", value=0.0)
        ],
        transition="change",
    )
    engine = _engine(clock, [fire_rule])
    assert engine.evaluate(_change(_quake(magnitude=9.0))) == []


def test_feature_removal_auto_resolves_open_enter_alert() -> None:
    # An ENTER-transition rule on a feature opens an alert when the quake appears; when
    # the quake ages out of the feed (a feature remove) the open alert auto-resolves —
    # the same lifecycle a track gets, so feature-driven enter-rules don't leak.
    clock = _Clock(T0)
    engine = _engine(clock, [_quake_rule(transition="enter")])
    opened = engine.evaluate(_change(_quake(magnitude=5.0)))
    assert len(opened) == 1 and opened[0].state == "open"

    clock.t = T0 + timedelta(seconds=30)
    resolved = engine.evaluate(
        StateChange(seq=2, op="remove", kind="feature", id="earthquake:usgs:nc1", record=None)
    )
    assert len(resolved) == 1
    assert resolved[0].id == opened[0].id
    assert resolved[0].state == "resolved"


# --- geo-feature (environmental) alerts: FIRMS active fire, FIRMS-FR-005 -----


def _fire(
    *,
    frp_mw: float | None = 80.0,
    lon: float = -95.0,
    lat: float = 40.0,
    id: str = "fire:firms:nc1",
) -> GeoFeatureRecord:
    return GeoFeatureRecord(
        id=id,
        source="firms",
        observed_at=T0,
        received_at=T0,
        published_at=T0,
        correlation_key=id,
        feature_type="fire_detection",
        geometry=Point(coordinates=[lon, lat]),
        label="Fire detection",
        attributes={"frp_mw": frp_mw},
    )


def test_fire_detection_drives_engine_by_frp() -> None:
    # A FIRMS fire_detection drives the engine like any other geo-feature, but on its own
    # layer: it reports subject_type "fire_detection" (never "earthquake"), and the
    # high-intensity default fires on reported FRP above its threshold (change transition,
    # one alert per newly-seen detection).
    from aether.alerts.engine import subject_type_of

    assert subject_type_of(_fire()) == "fire_detection"

    rule = _rule(
        subject_types=["fire_detection"],
        conditions=[AlertCondition(field="attributes.frp_mw", operator="greater_than", value=50.0)],
        transition="change",
    )
    engine = _engine(_Clock(T0), [rule])

    assert engine.evaluate(_change(_fire(frp_mw=12.0))) == []  # below 50 MW → no fire
    out = engine.evaluate(_change(_fire(frp_mw=120.0, id="fire:firms:big")))
    assert len(out) == 1
    assert out[0].state == "open"
    assert out[0].subject_id == "fire:firms:big"


def test_fire_detection_unknown_frp_does_not_fire() -> None:
    # FIRMS may not report FRP for a row; greater_than on a null attribute is unevaluable,
    # so the high-intensity rule stays honestly inert (no fire) rather than firing on a
    # bogus 0 — the same fail-visibly stance as elevation on a feature (PRD §37).
    rule = _rule(
        subject_types=["fire_detection"],
        conditions=[AlertCondition(field="attributes.frp_mw", operator="greater_than", value=50.0)],
        transition="change",
    )
    engine = _engine(_Clock(T0), [rule])
    assert engine.evaluate(_change(_fire(frp_mw=None))) == []


# --- preview (the /test endpoint core) --------------------------------------


def test_preview_reports_matches_for_stateless_rule() -> None:
    rule = _rule()  # squawk == 7700, aircraft
    records: list[Record] = [
        _aircraft(squawk="7700", id="a1"),
        _aircraft(squawk="1200", id="a2"),
        _aircraft(squawk="7700", track_type="vessel", id="v1"),  # wrong subject type
    ]
    res = preview_rule(rule, records)
    assert res["evaluable"] is True
    assert res["evaluated"] == 2  # only the two aircraft
    assert res["matched"] == 1
    assert {m["subject_id"] for m in res["matches"]} == {"a1", "a2"}


def test_preview_marks_contextual_rule_unevaluable() -> None:
    rule = _rule(conditions=[AlertCondition(field="geometry", operator="entered_geofence")])
    res = preview_rule(rule, [_aircraft(squawk="7700")])
    assert res["evaluable"] is False
    assert res["matched"] == 0
    assert res["matches"][0]["matched"] is None

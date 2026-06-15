"""Unit tests for the in-memory live state and its sequence numbering."""

from datetime import UTC, datetime, timedelta

from aether.schema.records import (
    AlertRecord,
    EventRecord,
    GeoFeatureRecord,
    SourceStatusRecord,
    TrackRecord,
)
from aether.state import LiveState

T0 = datetime(2026, 6, 15, 12, 0, 0, tzinfo=UTC)


def _track(id: str = "aircraft:1", valid_until: datetime | None = None) -> TrackRecord:
    return TrackRecord(
        id=id,
        source="demo",
        observed_at=T0,
        received_at=T0,
        published_at=T0,
        track_type="aircraft",
        locally_received=True,
        valid_until=valid_until,
    )


def _feature(id: str = "tfr:1", valid_until: datetime | None = None) -> GeoFeatureRecord:
    return GeoFeatureRecord(
        id=id,
        source="demo",
        observed_at=T0,
        received_at=T0,
        published_at=T0,
        feature_type="tfr",
        geometry={"type": "Point", "coordinates": [-95.0, 40.0]},
        valid_until=valid_until,
    )


def _event(id: str) -> EventRecord:
    return EventRecord(
        id=id,
        source="demo",
        observed_at=T0,
        received_at=T0,
        published_at=T0,
        event_type="demo",
        summary="hello",
    )


def test_apply_track_upserts_and_bumps_seq() -> None:
    state = LiveState()
    assert state.seq == 0
    change = state.apply(_track())
    assert (change.op, change.kind, change.id, change.seq) == ("upsert", "track", "aircraft:1", 1)
    assert state.seq == 1
    assert [t.id for t in state.snapshot().tracks] == ["aircraft:1"]


def test_apply_same_id_updates_in_place() -> None:
    state = LiveState()
    state.apply(_track(id="aircraft:1"))
    state.apply(_track(id="aircraft:1"))
    snap = state.snapshot()
    assert len(snap.tracks) == 1
    assert snap.seq == 2  # two mutations, one row


def test_apply_routes_each_kind() -> None:
    state = LiveState()
    state.apply(_track())
    state.apply(_feature())
    state.apply(_event("event:1"))
    state.apply(
        AlertRecord(
            id="alert:1",
            source="demo",
            observed_at=T0,
            received_at=T0,
            published_at=T0,
            rule_id="r1",
            state="open",
            severity="high",
            title="t",
            summary="s",
            triggered_at=T0,
        )
    )
    state.apply(
        SourceStatusRecord(
            id="source_status:demo",
            source="demo",
            observed_at=T0,
            received_at=T0,
            published_at=T0,
            status="connected",
        )
    )
    snap = state.snapshot()
    assert len(snap.tracks) == 1
    assert len(snap.features) == 1
    assert len(snap.events) == 1
    assert len(snap.alerts) == 1
    assert len(snap.source_status) == 1
    assert snap.seq == 5


def test_remove_track() -> None:
    state = LiveState()
    state.apply(_track(id="aircraft:1"))
    change = state.remove("track", "aircraft:1")
    assert (change.op, change.kind, change.record) == ("remove", "track", None)
    assert state.snapshot().tracks == []


def test_expire_removes_past_valid_until() -> None:
    state = LiveState()
    state.apply(_track(id="stale", valid_until=T0))
    state.apply(_track(id="fresh", valid_until=T0 + timedelta(minutes=5)))
    changes = state.expire(now=T0 + timedelta(minutes=1))
    assert [c.id for c in changes] == ["stale"]
    assert [t.id for t in state.snapshot().tracks] == ["fresh"]


def test_recent_events_are_bounded() -> None:
    state = LiveState(recent_events_max=3)
    for i in range(5):
        state.apply(_event(f"event:{i}"))
    events = state.snapshot().events
    assert [e.id for e in events] == ["event:2", "event:3", "event:4"]


def test_seq_is_monotonic_across_ops() -> None:
    state = LiveState()
    seqs = [
        state.apply(_track(id="a")).seq,
        state.apply(_feature(id="f")).seq,
        state.remove("track", "a").seq,
        state.apply(_event("e")).seq,
    ]
    assert seqs == [1, 2, 3, 4]

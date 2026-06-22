"""Unit tests for the in-memory live state and its sequence numbering."""

from datetime import UTC, datetime, timedelta

from aether.fusion.engine import FUSION_ATTR_KEY, FusionEngine
from aether.schema.geometry import Point
from aether.schema.provenance import Provenance
from aether.schema.records import (
    AlertRecord,
    EventRecord,
    GeoFeatureRecord,
    SourceStatusRecord,
    TrackRecord,
)
from aether.state import LiveState

T0 = datetime(2026, 6, 15, 12, 0, 0, tzinfo=UTC)
CORR = "aircraft:icao:abc"


def _local_track(observed_at: datetime = T0) -> TrackRecord:
    return TrackRecord(
        id="local_adsb:abc",
        source="local_adsb",
        observed_at=observed_at,
        received_at=observed_at,
        published_at=observed_at,
        correlation_key=CORR,
        track_type="aircraft",
        geometry=Point(coordinates=[-95.0, 40.0, 3000.0]),
        altitude_m=3000.0,
        locally_received=True,
        provenance=[
            Provenance(
                source="local_adsb", observed_at=observed_at, received_at=observed_at, local_rf=True
            )
        ],
    )


def _net_track(observed_at: datetime = T0) -> TrackRecord:
    return TrackRecord(
        id="demo-net:abc",
        source="demo-net",
        observed_at=observed_at,
        received_at=observed_at,
        published_at=observed_at,
        correlation_key=CORR,
        track_type="aircraft",
        label="DEMO-FUSE",
        geometry=Point(coordinates=[-95.001, 40.001, 3000.0]),
        altitude_m=3000.0,
        speed_mps=120.0,
        locally_received=False,
        provenance=[
            Provenance(
                source="demo-net", observed_at=observed_at, received_at=observed_at, local_rf=False
            )
        ],
    )


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


def _feature(
    id: str = "tfr:1",
    valid_until: datetime | None = None,
    valid_from: datetime | None = None,
) -> GeoFeatureRecord:
    return GeoFeatureRecord(
        id=id,
        source="demo",
        observed_at=T0,
        received_at=T0,
        published_at=T0,
        feature_type="tfr",
        geometry={"type": "Point", "coordinates": [-95.0, 40.0]},
        valid_from=valid_from,
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


# --- Feature activation re-drive (became_active edge, PRD §32 #16) ----------


def test_expire_redrives_feature_crossing_valid_from() -> None:
    # A TFR ingested while still pending: when a sweep's clock crosses its valid_from,
    # the feature is re-driven (re-upserted unchanged) so a clock-aware became_active
    # alert sees the rising edge with no new ingest.
    state = LiveState()
    state.apply(
        _feature(
            id="tfr:soon",
            valid_from=T0 + timedelta(minutes=10),
            valid_until=T0 + timedelta(hours=2),
        )
    )
    # A sweep before valid_from sets the baseline clock and re-drives nothing.
    assert state.expire(now=T0 + timedelta(minutes=1)) == []
    # A sweep after valid_from re-drives exactly that feature as an upsert.
    changes = state.expire(now=T0 + timedelta(minutes=11))
    assert [(c.op, c.kind, c.id) for c in changes] == [("upsert", "feature", "tfr:soon")]
    # The re-drive carries the unchanged record (idempotent for clients and the engine).
    assert changes[0].record is not None and changes[0].record.id == "tfr:soon"
    # The crossing happened once: a later sweep does not re-drive it again.
    assert state.expire(now=T0 + timedelta(minutes=12)) == []


def test_expire_does_not_redrive_before_valid_from() -> None:
    state = LiveState()
    state.apply(_feature(id="tfr:later", valid_from=T0 + timedelta(hours=1)))
    state.expire(now=T0)  # baseline
    assert state.expire(now=T0 + timedelta(minutes=5)) == []  # still pending


def test_expire_does_not_redrive_feature_without_valid_from() -> None:
    # A TFR with no parsed effective time has nothing to activate — never re-driven.
    state = LiveState()
    state.apply(_feature(id="tfr:open", valid_until=T0 + timedelta(hours=1)))
    state.expire(now=T0)
    assert state.expire(now=T0 + timedelta(minutes=5)) == []


def test_expire_does_not_redrive_already_expired_feature() -> None:
    # valid_from and valid_until both fall in one sweep gap: the feature is removed and
    # never re-driven — no became_active for a window already entirely in the past.
    state = LiveState()
    state.apply(
        _feature(
            id="tfr:brief",
            valid_from=T0 + timedelta(minutes=1),
            valid_until=T0 + timedelta(minutes=2),
        )
    )
    state.expire(now=T0)  # baseline before the window
    changes = state.expire(now=T0 + timedelta(minutes=5))  # both bounds crossed
    assert [(c.op, c.id) for c in changes] == [("remove", "tfr:brief")]


def test_first_sweep_does_not_redrive_already_active_feature() -> None:
    # The very first sweep has no previous clock, so it re-drives nothing even for an
    # already-active feature — that feature was driven on apply(), not the sweep.
    state = LiveState()
    state.apply(
        _feature(
            id="tfr:active",
            valid_from=T0 - timedelta(minutes=5),
            valid_until=T0 + timedelta(hours=1),
        )
    )
    assert state.expire(now=T0) == []


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


# --- Fusion routing (M3.1, FUSION-FR-001/004/006) --------------------------


def test_correlation_key_track_yields_one_upsert_at_key() -> None:
    state = LiveState()
    change = state.apply(_local_track(), now=T0)
    assert (change.op, change.kind, change.id, change.seq) == ("upsert", "track", CORR, 1)
    assert state.seq == 1  # exactly one mutation per source record


def test_local_plus_network_same_key_is_one_track() -> None:
    state = LiveState()
    state.apply(_local_track(), now=T0)
    state.apply(_net_track(), now=T0)
    snap = state.snapshot()
    assert len(snap.tracks) == 1
    fused = snap.tracks[0]
    assert fused.id == CORR
    assert fused.attributes[FUSION_ATTR_KEY]["fused_count"] == 2


def test_none_key_track_keyed_by_own_id_without_fusion() -> None:
    # FUSION-FR-006: no correlation key → no fusion path, keyed by record.id.
    state = LiveState()
    change = state.apply(_track(id="aircraft:loose"), now=T0)
    assert change.id == "aircraft:loose"
    fused = state.snapshot().tracks[0]
    assert FUSION_ATTR_KEY not in fused.attributes


def test_expire_removes_fused_only_after_all_contributors_expire() -> None:
    state = LiveState()
    state.apply(_local_track(), now=T0)
    state.apply(_net_track(), now=T0)

    # At 90s local has expired but network survives: continuation upsert, no remove.
    changes = state.expire(now=T0 + timedelta(seconds=90))
    ops = {(c.op, c.id) for c in changes}
    assert ("upsert", CORR) in ops
    assert ("remove", CORR) not in ops
    fused = state.snapshot().tracks[0]
    assert fused.locally_received is False  # flipped to network continuation

    # Past the network window too: the fused track is removed.
    changes2 = state.expire(now=T0 + timedelta(seconds=200))
    assert ("remove", CORR) in {(c.op, c.id) for c in changes2}
    assert state.snapshot().tracks == []


def test_remove_clears_engine_group() -> None:
    state = LiveState()
    state.apply(_local_track(), now=T0)
    state.remove("track", CORR)
    # Re-ingesting starts a fresh group (fused_count back to 1, not 2).
    state.apply(_net_track(), now=T0)
    fused = state.snapshot().tracks[0]
    assert fused.attributes[FUSION_ATTR_KEY]["fused_count"] == 1


# --- Failure isolation (review findings) -----------------------------------


class _IngestRaises(FusionEngine):
    """Engine whose ingest always raises, to exercise the degraded fallback."""

    def ingest(self, record: TrackRecord, now: datetime) -> TrackRecord:
        raise RuntimeError("boom")


def test_degraded_fallback_rekeys_record_to_correlation_key() -> None:
    # Finding 1: a transient fusion failure on a source whose id != correlation
    # key must store/emit the record under the correlation key, so the delta's
    # record.id == StateChange.id and a later expiry remove can clean it up
    # (no permanent ghost track on clients).
    state = LiveState(fusion=_IngestRaises())
    change = state.apply(_net_track(), now=T0)  # _net_track().id == "demo-net:abc" != CORR
    assert change.id == CORR
    assert change.record is not None
    assert change.record.id == CORR  # rewritten to match the StateChange id
    assert [t.id for t in state.snapshot().tracks] == [CORR]

    # Because the dict key == the stored record's id == CORR, a remove keyed by
    # CORR (what a later engine expiry emits) actually deletes it — no ghost.
    remove = state.remove("track", CORR)
    assert (remove.op, remove.id) == ("remove", CORR)
    assert state.snapshot().tracks == []


class _RecomputeRaises(FusionEngine):
    """Engine that reports one poison key dirty whose recompute always raises."""

    def __init__(self, poison: str) -> None:
        super().__init__()
        self._poison = poison

    def dirty_keys(self, now: datetime) -> list[str]:
        return [self._poison]

    def recompute(self, key: str, now: datetime) -> TrackRecord | None:
        raise RuntimeError("poison fuse")


def test_poison_group_does_not_abort_expiry_sweep() -> None:
    # Finding 2: a single group whose recompute raises must be dropped/logged,
    # not propagate out of expire() and strand every other expiry forever.
    state = LiveState(fusion=_RecomputeRaises(poison=CORR))
    # A None-key track that should still age out despite the poison group.
    state.apply(_track(id="loose", valid_until=T0), now=T0)
    changes = state.expire(now=T0 + timedelta(minutes=1))  # must not raise
    assert ("remove", "loose") in {(c.op, c.id) for c in changes}
    assert state.snapshot().tracks == []


def test_get_track_returns_fused_track_by_id() -> None:
    # Backs GET /api/v2/tracks/{id}: the fused track's id is its correlation key,
    # so the same id the snapshot exposes is the detail-lookup key (PRD §21.3).
    state = LiveState()
    state.apply(_local_track(), now=T0)
    assert state.get_track(CORR) is not None
    assert state.get_track(CORR).id == CORR  # type: ignore[union-attr]
    assert state.get_track("local_adsb:abc") is None  # per-source id is not the key
    assert state.get_track("nope") is None

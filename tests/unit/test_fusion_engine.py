"""Unit tests for the stateful fusion engine (FUSION-FR-001..007, PRD §15)."""

from datetime import UTC, datetime, timedelta
from typing import Any

from aether.fusion.engine import FUSION_ATTR_KEY, FusionEngine
from aether.schema.geometry import Point
from aether.schema.provenance import Provenance
from aether.schema.records import TrackRecord

KEY = "aircraft:icao:abc"
T0 = datetime(2026, 6, 15, 12, 0, 0, tzinfo=UTC)


def _local(observed_at: datetime = T0, **over: Any) -> TrackRecord:
    base: dict[str, Any] = dict(
        id="local_adsb:abc",
        source="local_adsb",
        observed_at=observed_at,
        received_at=observed_at,
        published_at=observed_at,
        correlation_key=KEY,
        track_type="aircraft",
        geometry=Point(coordinates=[-95.0, 40.0, 3000.0]),
        altitude_m=3000.0,
        heading_deg=90.0,
        locally_received=True,
        provenance=[
            Provenance(
                source="local_adsb", observed_at=observed_at, received_at=observed_at, local_rf=True
            )
        ],
    )
    base.update(over)
    return TrackRecord(**base)


def _net(observed_at: datetime = T0, **over: Any) -> TrackRecord:
    base: dict[str, Any] = dict(
        id="demo-net:abc",
        source="demo-net",
        observed_at=observed_at,
        received_at=observed_at,
        published_at=observed_at,
        correlation_key=KEY,
        track_type="aircraft",
        label="DEMO-FUSE",
        geometry=Point(coordinates=[-95.001, 40.001, 3000.0]),
        altitude_m=3000.0,
        speed_mps=120.0,
        heading_deg=90.0,
        locally_received=False,
        provenance=[
            Provenance(
                source="demo-net", observed_at=observed_at, received_at=observed_at, local_rf=False
            )
        ],
    )
    base.update(over)
    return TrackRecord(**base)


def _fusion(track: TrackRecord) -> dict[str, Any]:
    block = track.attributes[FUSION_ATTR_KEY]
    assert isinstance(block, dict)
    return block


def test_local_plus_network_fuse_to_one_track() -> None:
    eng = FusionEngine()
    eng.ingest(_local(), T0)
    fused = eng.ingest(_net(), T0)

    # FUSION-FR-001/005: one track, id == correlation_key, both sources preserved.
    assert fused.id == KEY
    assert fused.correlation_key == KEY
    assert {p.source for p in fused.provenance} == {"local_adsb", "demo-net"}
    block = _fusion(fused)
    assert block["fused_count"] == 2
    # Local wins position (FR-002); network fills speed + label (FR-003).
    assert block["field_sources"]["geometry"] == "local_adsb"
    assert block["field_sources"]["speed_mps"] == "demo-net"
    assert fused.speed_mps == 120.0
    assert fused.label == "DEMO-FUSE"
    assert fused.geometry is not None
    assert fused.geometry.coordinates[0] == -95.0  # local position, not blended
    assert fused.locally_received is True
    assert block["active_source"] == "local_adsb"


def test_fusion_block_shape() -> None:
    eng = FusionEngine()
    eng.ingest(_local(), T0)
    block = _fusion(eng.ingest(_net(), T0))
    assert set(block) >= {
        "active_source",
        "contributors",
        "field_sources",
        "field_freshness",
        "last_local_rf_at",
        "fused_count",
    }
    sources = [c["source"] for c in block["contributors"]]
    assert sources == sorted(sources)  # contributors sorted by source
    assert block["last_local_rf_at"] == T0.isoformat()


def test_locally_received_flips_false_after_local_expires() -> None:
    eng = FusionEngine()
    eng.ingest(_local(), T0)
    eng.ingest(_net(), T0)
    # Local window expires at 60s; jump past it with only the network still inside
    # its own window (network expires at 120s).
    later = T0 + timedelta(seconds=90)
    fused = eng.recompute(KEY, later)
    assert fused is not None
    assert fused.locally_received is False
    assert _fusion(fused)["active_source"] == "demo-net"


def test_last_local_rf_at_monotonic_and_persists() -> None:
    eng = FusionEngine()
    eng.ingest(_local(observed_at=T0), T0)
    t1 = T0 + timedelta(seconds=2)
    eng.ingest(_local(observed_at=t1), t1)
    # An out-of-order older local must not regress last_local_rf_at.
    eng.ingest(_local(observed_at=T0), t1)
    fused = eng.recompute(KEY, t1)
    assert fused is not None
    assert _fusion(fused)["last_local_rf_at"] == t1.isoformat()
    # After local expires entirely, last_local_rf_at still reports when it last heard.
    late = t1 + timedelta(seconds=200)
    eng.ingest(_net(observed_at=late), late)
    fused2 = eng.recompute(KEY, late)
    assert fused2 is not None
    assert _fusion(fused2)["last_local_rf_at"] == t1.isoformat()


def test_out_of_order_record_ignored_but_returns_current() -> None:
    eng = FusionEngine()
    t1 = T0 + timedelta(seconds=5)
    eng.ingest(_local(observed_at=t1, altitude_m=5000.0), t1)
    fused = eng.ingest(_local(observed_at=T0, altitude_m=1000.0), t1)
    # The older observation is discarded; the newer altitude stands.
    assert fused.altitude_m == 5000.0


def test_duplicate_is_idempotent() -> None:
    eng = FusionEngine()
    eng.ingest(_local(), T0)
    a = eng.ingest(_local(), T0)
    b = eng.ingest(_local(), T0)
    assert a.model_dump(mode="json") == b.model_dump(mode="json")
    assert _fusion(b)["fused_count"] == 1


def test_fused_id_stability_local_only() -> None:
    # A local-only aircraft fuses to an id byte-identical to readsb's own id.
    eng = FusionEngine()
    fused = eng.ingest(_local(), T0)
    assert fused.id == KEY


def test_expired_keys_only_when_all_contributors_expired() -> None:
    eng = FusionEngine()
    eng.ingest(_local(), T0)
    eng.ingest(_net(), T0)
    # At 90s local is expired but network is not → not yet expired.
    assert eng.expired_keys(T0 + timedelta(seconds=90)) == []
    # Past the network window too → all expired.
    assert eng.expired_keys(T0 + timedelta(seconds=200)) == [KEY]


def test_dirty_keys_prunes_and_enables_continuation() -> None:
    eng = FusionEngine()
    eng.ingest(_local(), T0)
    eng.ingest(_net(), T0)
    later = T0 + timedelta(seconds=90)  # local expired, network alive
    dirty = eng.dirty_keys(later)
    assert dirty == [KEY]
    fused = eng.recompute(KEY, later)
    assert fused is not None
    assert _fusion(fused)["fused_count"] == 1  # local pruned
    assert fused.locally_received is False


def test_conflict_determinism() -> None:
    eng = FusionEngine()
    # Network altitude differs from local by > 30 m epsilon → a conflict is recorded.
    eng.ingest(_local(altitude_m=3000.0), T0)
    fused_a = eng.ingest(_net(altitude_m=3500.0), T0)

    eng2 = FusionEngine()
    eng2.ingest(_local(altitude_m=3000.0), T0)
    fused_b = eng2.ingest(_net(altitude_m=3500.0), T0)

    assert fused_a.model_dump(mode="json") == fused_b.model_dump(mode="json")
    conflicts = _fusion(fused_a).get("conflicts", [])
    alt_conflicts = [c for c in conflicts if c["field"] == "altitude_m"]
    assert alt_conflicts and alt_conflicts[0]["winner"] == "local_adsb"


def test_drop_forgets_group() -> None:
    eng = FusionEngine()
    eng.ingest(_local(), T0)
    eng.drop(KEY)
    assert eng.recompute(KEY, T0) is None


def test_future_observed_at_eventually_expires() -> None:
    # Finding 3: a contributor with a far-future observed_at must NOT pin its
    # group forever. Once "now" passes the future timestamp by more than the
    # expire window, the group reports as all-expired and can be reaped.
    eng = FusionEngine()
    future = T0 + timedelta(hours=1)
    eng.ingest(_local(observed_at=future), now=T0)
    # Within skew tolerance of the future stamp it still reads live (no expiry).
    assert eng.expired_keys(future) == []
    # Well past the future stamp + the expire window, it finally expires.
    assert eng.expired_keys(future + timedelta(seconds=200)) == [KEY]

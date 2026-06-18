"""Unit tests for per-field precedence (FUSION-FR-002/003/004, PRD §15.2)."""

from datetime import UTC, datetime, timedelta
from typing import Any

from aether.fusion.freshness import FreshnessClass
from aether.fusion.precedence import (
    ContributorView,
    pick_dynamic_field,
    pick_metadata_field,
)
from aether.schema.geometry import Point
from aether.schema.records import Classification, TrackRecord

T0 = datetime(2026, 6, 15, 12, 0, 0, tzinfo=UTC)


def _track(source: str, **over: Any) -> TrackRecord:
    base: dict[str, Any] = dict(
        id="aircraft:icao:abc",
        source=source,
        observed_at=T0,
        received_at=T0,
        published_at=T0,
        correlation_key="aircraft:icao:abc",
        track_type="aircraft",
        locally_received=over.pop("locally_received", False),
    )
    base.update(over)
    return TrackRecord(**base)


def _view(
    source: str,
    *,
    local_rf: bool,
    freshness: FreshnessClass,
    observed_at: datetime = T0,
    **track_over: Any,
) -> ContributorView:
    return ContributorView(
        source=source,
        record=_track(source, observed_at=observed_at, **track_over),
        local_rf=local_rf,
        observed_at=observed_at,
        freshness=freshness,
    )


def test_fresh_local_beats_fresh_network() -> None:
    # FUSION-FR-002: a fresh local-RF dynamic field wins outright.
    local = _view("local", local_rf=True, freshness="live", altitude_m=900.0)
    net = _view("net", local_rf=False, freshness="live", altitude_m=901.0)
    pick = pick_dynamic_field("altitude_m", [net, local])
    assert (pick.value, pick.source, pick.freshness) == (900.0, "local", "live")


def test_network_fills_field_local_omits() -> None:
    # FUSION-FR-003: local has no speed → network wins speed while still being
    # excluded only from the fields it lacks.
    local = _view("local", local_rf=True, freshness="live", speed_mps=None)
    net = _view("net", local_rf=False, freshness="live", speed_mps=130.0)
    pick = pick_dynamic_field("speed_mps", [local, net])
    assert (pick.value, pick.source) == (130.0, "net")


def test_live_network_beats_stale_local() -> None:
    # FUSION-FR-004: a live network observation continues the track over a stale
    # local one (tier 1 < tier 2).
    local = _view("local", local_rf=True, freshness="stale", altitude_m=900.0)
    net = _view("net", local_rf=False, freshness="live", altitude_m=950.0)
    pick = pick_dynamic_field("altitude_m", [local, net])
    assert pick.source == "net"


def test_all_stale_prefers_local() -> None:
    # Stale local (tier 2) still beats stale network (tier 3).
    local = _view("local", local_rf=True, freshness="stale", altitude_m=900.0)
    net = _view("net", local_rf=False, freshness="stale", altitude_m=950.0)
    pick = pick_dynamic_field("altitude_m", [net, local])
    assert pick.source == "local"


def test_no_contributor_has_field() -> None:
    a = _view("a", local_rf=True, freshness="live", speed_mps=None)
    b = _view("b", local_rf=False, freshness="live", speed_mps=None)
    pick = pick_dynamic_field("speed_mps", [a, b])
    assert (pick.value, pick.source, pick.freshness) == (None, None, None)


def test_tie_breaks_on_source_name_ascending() -> None:
    # Same tier, same observed_at → deterministic source-name tie-break.
    z = _view("zsrc", local_rf=True, freshness="live", altitude_m=1.0)
    a = _view("asrc", local_rf=True, freshness="live", altitude_m=2.0)
    pick = pick_dynamic_field("altitude_m", [z, a])
    assert pick.source == "asrc"


def test_geometry_taken_whole_from_one_contributor() -> None:
    # PRD §15.5: position is selected from one source, never averaged.
    local = _view(
        "local", local_rf=True, freshness="live", geometry=Point(coordinates=[-95.0, 40.0])
    )
    net = _view("net", local_rf=False, freshness="live", geometry=Point(coordinates=[-94.0, 41.0]))
    pick = pick_dynamic_field("geometry", [net, local])
    assert pick.source == "local"
    assert pick.value.coordinates == [-95.0, 40.0]  # exact, not blended


def test_metadata_label_from_network_while_local_wins_dynamics() -> None:
    # Label is metadata: local_rf does not privilege it, freshest non-expired wins.
    local = _view("local", local_rf=True, freshness="live", label=None, altitude_m=900.0)
    net = _view(
        "net",
        local_rf=False,
        freshness="live",
        label="DEMO-FUSE",
        observed_at=T0 + timedelta(seconds=1),
    )
    label_pick = pick_metadata_field("label", [local, net])
    assert label_pick.value == "DEMO-FUSE"
    alt_pick = pick_dynamic_field("altitude_m", [local, net])
    assert alt_pick.source == "local"


def test_metadata_excludes_expired() -> None:
    expired = _view("old", local_rf=True, freshness="expired", label="STALE")
    pick = pick_metadata_field("label", [expired])
    assert pick.value is None


def test_classification_metadata_picked() -> None:
    cls = Classification(military=True, basis="provider", confidence="high")
    net = _view("net", local_rf=False, freshness="live", classification=cls)
    pick = pick_metadata_field("classification", [net])
    assert pick.value is cls

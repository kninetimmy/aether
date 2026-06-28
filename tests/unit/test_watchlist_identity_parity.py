"""Parity contract: backend watchlist_key() ↔ frontend watchlistKey() (M6.6b).

Each case mirrors the equivalent assertion in ``frontend/src/state/selectors.test.ts``
(lines ~549-606) so both test suites pin both ends of the client–server key contract
to a shared table.  A divergence breaks exactly one side's test, making it immediately
visible.

Cases A-I cover every code branch:
  A/B - primary branch (correlation_key present)
  C/D - aircraft fallback (icao / hex)
  E   - vessel fallback (mmsi)
  F   - aprs_station / aprs_object fallback (label)
  G   - orbital_object primary (correlation_key always present)
  H   - aircraft no-identity last-resort (raw id)
  I   - non-track records (GeoFeatureRecord, EventRecord → None)
"""

from __future__ import annotations

from datetime import UTC, datetime

from aether.alerts.identity import watchlist_key
from aether.schema.records import (
    EventRecord,
    GeoFeatureRecord,
    TrackRecord,
)

T0 = datetime(2026, 6, 28, 12, 0, 0, tzinfo=UTC)


def _track(
    *,
    id: str,
    track_type: str,
    correlation_key: str | None = None,
    label: str | None = None,
    attributes: dict | None = None,
) -> TrackRecord:
    return TrackRecord(
        id=id,
        source="test",
        observed_at=T0,
        received_at=T0,
        published_at=T0,
        correlation_key=correlation_key,
        track_type=track_type,  # type: ignore[arg-type]
        locally_received=True,
        label=label,
        attributes=attributes or {},
    )


def _geo_feature(*, id: str = "feat-1") -> GeoFeatureRecord:
    return GeoFeatureRecord(
        id=id,
        source="test",
        observed_at=T0,
        received_at=T0,
        published_at=T0,
        feature_type="earthquake",
        geometry={"type": "Point", "coordinates": [0.0, 0.0]},
    )


def _event(*, id: str = "evt-1") -> EventRecord:
    return EventRecord(
        id=id,
        source="test",
        observed_at=T0,
        received_at=T0,
        published_at=T0,
        event_type="emergency_squawk",
        summary="test event",
    )


# ---------------------------------------------------------------------------
# Case A: net-adsb track with correlation_key — primary branch, != id
# ---------------------------------------------------------------------------


def test_case_a_primary_branch_prefers_correlation_key() -> None:
    track = _track(
        id="net-adsb:aircraft:icao:abc123",
        track_type="aircraft",
        correlation_key="aircraft:icao:abc123",
    )
    assert watchlist_key(track) == "aircraft:icao:abc123"
    # Key is different from the raw id (primary branch wins)
    assert watchlist_key(track) != track.id


# ---------------------------------------------------------------------------
# Case B: fusion-handoff stability — local and net legs give the same key
# ---------------------------------------------------------------------------


def test_case_b_local_and_net_legs_agree() -> None:
    local = _track(
        id="local_adsb:aircraft:icao:abc123",
        track_type="aircraft",
        correlation_key="aircraft:icao:abc123",
    )
    net = _track(
        id="net-adsb:aircraft:icao:abc123",
        track_type="aircraft",
        correlation_key="aircraft:icao:abc123",
    )
    assert watchlist_key(local) == watchlist_key(net) == "aircraft:icao:abc123"


# ---------------------------------------------------------------------------
# Case C: aircraft fallback via icao attribute (lowercased)
# ---------------------------------------------------------------------------


def test_case_c_aircraft_icao_fallback_lowercased() -> None:
    track = _track(
        id="orphan:1",
        track_type="aircraft",
        correlation_key=None,
        attributes={"icao": "ABC123"},
    )
    assert watchlist_key(track) == "aircraft:icao:abc123"


# ---------------------------------------------------------------------------
# Case D: aircraft fallback via hex attribute (no icao)
# ---------------------------------------------------------------------------


def test_case_d_aircraft_hex_fallback_lowercased() -> None:
    track = _track(
        id="orphan:2",
        track_type="aircraft",
        correlation_key=None,
        attributes={"hex": "DEAD01"},
    )
    assert watchlist_key(track) == "aircraft:icao:dead01"


# ---------------------------------------------------------------------------
# Case E: vessel fallback via mmsi attribute
# ---------------------------------------------------------------------------


def test_case_e_vessel_mmsi_fallback() -> None:
    track = _track(
        id="orphan:3",
        track_type="vessel",
        correlation_key=None,
        attributes={"mmsi": "366000001"},
    )
    assert watchlist_key(track) == "mmsi:366000001"


# ---------------------------------------------------------------------------
# Case F: aprs_station and aprs_object fallback via label
# ---------------------------------------------------------------------------


def test_case_f_aprs_station_label_fallback() -> None:
    track = _track(
        id="orphan:4",
        track_type="aprs_station",
        correlation_key=None,
        label="N0CALL-9",
    )
    assert watchlist_key(track) == "aprs:N0CALL-9"


def test_case_f_aprs_object_label_fallback() -> None:
    track = _track(
        id="orphan:5",
        track_type="aprs_object",
        correlation_key=None,
        label="N0CALL-9",
    )
    assert watchlist_key(track) == "aprs:N0CALL-9"


# ---------------------------------------------------------------------------
# Case G: orbital_object — always has correlation_key (primary branch)
# ---------------------------------------------------------------------------


def test_case_g_orbital_primary_branch() -> None:
    track = _track(
        id="orbital:celestrak:25544",
        track_type="orbital_object",
        correlation_key="orbital:celestrak:25544",
    )
    assert watchlist_key(track) == "orbital:celestrak:25544"


# ---------------------------------------------------------------------------
# Case H: aircraft with no identity attributes — last-resort raw id
# ---------------------------------------------------------------------------


def test_case_h_aircraft_no_identity_raw_id() -> None:
    track = _track(
        id="orphan:42",
        track_type="aircraft",
        correlation_key=None,
        attributes={},
    )
    assert watchlist_key(track) == "orphan:42"


# ---------------------------------------------------------------------------
# Case I: non-track records → None
# ---------------------------------------------------------------------------


def test_case_i_geo_feature_returns_none() -> None:
    assert watchlist_key(_geo_feature()) is None


def test_case_i_event_returns_none() -> None:
    assert watchlist_key(_event()) is None


# ---------------------------------------------------------------------------
# Additional edge-case: empty-string correlation_key is falsy (uses fallback)
# ---------------------------------------------------------------------------


def test_empty_correlation_key_treated_as_absent() -> None:
    track = _track(
        id="orphan:6",
        track_type="aircraft",
        correlation_key="",  # empty string → falsy, same as JS
        attributes={"icao": "aabbcc"},
    )
    assert watchlist_key(track) == "aircraft:icao:aabbcc"


def test_empty_label_treated_as_absent_for_aprs() -> None:
    track = _track(
        id="orphan:7",
        track_type="aprs_station",
        correlation_key=None,
        label="",
    )
    # Empty label → falls to raw id
    assert watchlist_key(track) == "orphan:7"

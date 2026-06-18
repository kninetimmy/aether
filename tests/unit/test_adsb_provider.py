"""Unit tests for the network ADS-B provider layer (PRD §18.2)."""

import asyncio
import json
import math
from datetime import UTC, datetime
from pathlib import Path

from aether.adapters.adsb_provider import (
    SOURCE,
    AdsbFiProvider,
    AircraftObservation,
    dedupe_observations,
    observation_to_track,
    parse_response,
)
from aether.adapters.aoi import GeoRegion
from aether.schema.validation import dump_record_json, parse_record

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "adsbfi" / "region.json"
RECEIVED_AT = datetime(2026, 6, 17, 12, 0, 0, tzinfo=UTC)

#: The fixture's top-level ``now`` (epoch *milliseconds*) → the snapshot instant.
_SNAPSHOT_NOW = datetime.fromtimestamp(1781742864002 / 1000.0, tz=UTC)


def _payload() -> dict:
    return json.loads(FIXTURE.read_text())


def _obs_by_hex() -> dict[str, AircraftObservation]:
    return {o.icao_hex: o for o in parse_response(_payload(), received_at=RECEIVED_AT)}


def test_parse_yields_observations_for_each_identified_aircraft() -> None:
    obs = _obs_by_hex()
    assert set(obs) == {"a1b2c3", "bbccdd", "c0ffee", "ddeeff", "abc123", "ae1234"}


def test_now_is_parsed_as_milliseconds_not_seconds() -> None:
    # 1781742864002 read as seconds would land ~57000 AD; as ms it is 2026.
    obs = _obs_by_hex()["a1b2c3"]
    assert obs.observed_at.year == 2026
    assert obs.observed_at.tzinfo is not None
    # observed_at = snapshot ``now`` minus the position age (seen_pos = 0.2s).
    assert abs((_SNAPSHOT_NOW - obs.observed_at).total_seconds() - 0.2) < 1e-6


def test_normal_aircraft_normalized_to_si() -> None:
    obs = _obs_by_hex()["a1b2c3"]
    assert obs.label == "UAL123"  # flight trimmed
    assert obs.non_icao is False
    assert obs.altitude_m == 35000 * 0.3048
    assert math.isclose(obs.speed_mps, 450.2 * 1852.0 / 3600.0)
    assert math.isclose(obs.vertical_rate_mps, -64.0 * 0.3048 / 60.0)
    assert obs.heading_deg == 270.5
    assert obs.geometry is not None
    assert obs.geometry.coordinates == [-95.2231, 40.7128, 35000 * 0.3048]


def test_on_ground_aircraft() -> None:
    obs = _obs_by_hex()["bbccdd"]
    assert obs.on_ground is True
    assert obs.altitude_m == 0.0
    assert obs.attributes["on_ground"] is True


def test_emergency_squawk_flagged() -> None:
    obs = _obs_by_hex()["c0ffee"]
    assert obs.emergency is True
    track = observation_to_track(obs)
    assert "emergency" in track.tags


def test_missing_position_keeps_identity_drops_geometry() -> None:
    obs = _obs_by_hex()["ddeeff"]
    assert obs.geometry is None
    assert obs.bad_position is False  # no lat/lon at all, not a *bad* one
    assert obs.altitude_m == 8000 * 0.3048


def test_non_icao_address_stripped_and_flagged() -> None:
    obs = _obs_by_hex()["abc123"]  # fixture hex was "~abc123"
    assert obs.non_icao is True
    track = observation_to_track(obs)
    assert "non_icao" in track.tags
    assert track.id == "aircraft:icao:abc123"


def test_military_dbflags_preserved_but_not_classified_here() -> None:
    # M3.2 preserves the provider military bit for the later classification slice;
    # it must not assert a (possibly inferred) classification on its own.
    obs = _obs_by_hex()["ae1234"]
    assert obs.attributes["dbFlags"] == 1
    track = observation_to_track(obs)
    assert track.classification is None
    assert "military" not in track.tags


def test_observation_to_track_carries_network_provenance() -> None:
    track = observation_to_track(_obs_by_hex()["a1b2c3"])
    assert track.source == SOURCE
    assert track.locally_received is False
    assert track.id == track.correlation_key == "aircraft:icao:a1b2c3"
    assert len(track.provenance) == 1
    prov = track.provenance[0]
    assert prov.local_rf is False
    assert prov.provider == "adsb.fi"
    assert prov.source == SOURCE


def test_network_track_round_trips_through_schema() -> None:
    # A bad value (e.g. NaN) would crash serialization; assert the record is clean.
    track = observation_to_track(_obs_by_hex()["a1b2c3"])
    restored = parse_record(json.loads(dump_record_json(track)))
    assert restored.id == track.id


def test_dedupe_keeps_freshest_per_icao() -> None:
    older = AircraftObservation(
        icao_hex="a1b2c3",
        observed_at=datetime(2026, 6, 17, 12, 0, 0, tzinfo=UTC),
        received_at=RECEIVED_AT,
        altitude_m=1000.0,
    )
    newer = AircraftObservation(
        icao_hex="a1b2c3",
        observed_at=datetime(2026, 6, 17, 12, 0, 5, tzinfo=UTC),
        received_at=RECEIVED_AT,
        altitude_m=2000.0,
    )
    other = AircraftObservation(
        icao_hex="bbccdd",
        observed_at=datetime(2026, 6, 17, 12, 0, 1, tzinfo=UTC),
        received_at=RECEIVED_AT,
    )
    deduped = dedupe_observations([older, other, newer])
    by_hex = {o.icao_hex: o for o in deduped}
    assert len(deduped) == 2
    assert by_hex["a1b2c3"].altitude_m == 2000.0  # the fresher one won
    # First-seen order preserved: a1b2c3 (first) then bbccdd.
    assert [o.icao_hex for o in deduped] == ["a1b2c3", "bbccdd"]


def test_build_url_formats_and_clamps_radius() -> None:
    provider = AdsbFiProvider()
    url = provider.build_url(GeoRegion(40.5, -95.25, 500.0))
    # Radius is clamped to the provider's 250 NM cap.
    assert url == "https://opendata.adsb.fi/api/v3/lat/40.500000/lon/-95.250000/dist/250"


def test_fetch_region_uses_injected_fetch_no_live_call() -> None:
    calls: list[str] = []

    async def fake_fetch(url: str) -> bytes:
        calls.append(url)
        return FIXTURE.read_bytes()

    provider = AdsbFiProvider(fetch=fake_fetch)
    observations = asyncio.run(provider.fetch_region(GeoRegion(40.0, -95.0, 250.0)))
    assert len(calls) == 1
    assert calls[0].startswith("https://opendata.adsb.fi/api/v3/")
    assert {o.icao_hex for o in observations} == {
        "a1b2c3",
        "bbccdd",
        "c0ffee",
        "ddeeff",
        "abc123",
        "ae1234",
    }


def test_parse_response_tolerates_malformed_rows() -> None:
    payload = {
        "now": 1781742864002,
        "ac": [
            {"hex": "abc111", "lat": 40.0, "lon": -95.0, "seen": 0.1},
            "not-a-dict",
            {"no_hex": True},
            {"hex": "abc222", "lat": 41.0, "lon": -94.0, "seen": 0.2},
        ],
    }
    obs = parse_response(payload, received_at=RECEIVED_AT)
    assert {o.icao_hex for o in obs} == {"abc111", "abc222"}


def test_parse_response_handles_missing_ac_array() -> None:
    assert parse_response({"now": 1781742864002}, received_at=RECEIVED_AT) == []
    assert parse_response({"ac": "nope"}, received_at=RECEIVED_AT) == []

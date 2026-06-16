"""Unit tests for the local ADS-B (`readsb`) snapshot parser (PRD §18.1)."""

import json
import math
from datetime import UTC, datetime
from pathlib import Path

from aether.adapters.readsb import SOURCE, parse_aircraft_snapshot
from aether.schema.validation import dump_record_json, parse_record

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "readsb" / "aircraft.json"
RECEIVED_AT = datetime(2026, 6, 16, 12, 0, 0, tzinfo=UTC)


def _snapshot() -> dict:
    return json.loads(FIXTURE.read_text())


def _tracks_by_hex() -> dict[str, object]:
    tracks = parse_aircraft_snapshot(_snapshot(), received_at=RECEIVED_AT)
    return {t.id: t for t in tracks}


def test_snapshot_yields_tracks_for_identified_aircraft() -> None:
    tracks = parse_aircraft_snapshot(_snapshot(), received_at=RECEIVED_AT)
    ids = {t.id for t in tracks}
    # Six entries carry a hex; the seventh (no hex) is dropped.
    assert ids == {
        "aircraft:icao:a1b2c3",
        "aircraft:icao:abcdef",
        "aircraft:icao:c0ffee",
        "aircraft:icao:d00d11",
        "aircraft:icao:badc00",
        "aircraft:icao:tis123",
    }


def test_normal_aircraft_normalized_to_si() -> None:
    track = _tracks_by_hex()["aircraft:icao:a1b2c3"]
    assert track.track_type == "aircraft"
    assert track.label == "UAL123"  # flight trimmed
    assert track.source == SOURCE
    assert track.locally_received is True
    assert track.correlation_key == "aircraft:icao:a1b2c3"
    # 35000 ft -> m, 450 kt -> m/s, -640 ft/min -> m/s.
    assert track.altitude_m == 35000 * 0.3048
    assert math.isclose(track.speed_mps, 450.0 * 1852.0 / 3600.0)
    assert math.isclose(track.vertical_rate_mps, -640.0 * 0.3048 / 60.0)
    assert track.heading_deg == 271.4
    assert track.geometry is not None
    assert track.geometry.coordinates == [-95.2, 40.7128, 35000 * 0.3048]


def test_provenance_marks_local_rf() -> None:
    track = _tracks_by_hex()["aircraft:icao:a1b2c3"]
    assert len(track.provenance) == 1
    prov = track.provenance[0]
    assert prov.source == SOURCE
    assert prov.local_rf is True


def test_native_fields_preserved_in_attributes() -> None:
    track = _tracks_by_hex()["aircraft:icao:a1b2c3"]
    assert track.attributes["r"] == "N12345"
    assert track.attributes["t"] == "B738"
    assert track.attributes["rssi"] == -12.3
    assert track.attributes["category"] == "A3"
    assert track.attributes["messages"] == 2345


def test_observed_at_uses_snapshot_now_minus_position_age() -> None:
    track = _tracks_by_hex()["aircraft:icao:a1b2c3"]
    # snapshot now = 1781000000.0, seen_pos = 0.3
    expected = datetime.fromtimestamp(1781000000.0 - 0.3, tz=UTC)
    assert track.observed_at == expected


def test_emergency_squawk_tagged() -> None:
    track = _tracks_by_hex()["aircraft:icao:abcdef"]
    assert "emergency" in track.tags
    assert track.attributes["squawk"] == "7700"


def test_on_ground_altitude_zero_and_tagged() -> None:
    track = _tracks_by_hex()["aircraft:icao:c0ffee"]
    assert track.altitude_m == 0.0
    assert "on_ground" in track.tags
    assert track.attributes["on_ground"] is True
    assert track.geometry is not None
    assert track.geometry.coordinates == [-95.1, 40.8, 0.0]


def test_aircraft_without_position_keeps_identity_no_geometry() -> None:
    track = _tracks_by_hex()["aircraft:icao:d00d11"]
    assert track.geometry is None
    assert "bad_position" not in track.tags


def test_impossible_coordinates_drop_geometry_keep_track() -> None:
    track = _tracks_by_hex()["aircraft:icao:badc00"]
    assert track.geometry is None
    assert "bad_position" in track.tags
    # altitude is still trusted/normalized even when position is rejected
    assert track.altitude_m == 5000 * 0.3048


def test_non_icao_address_stripped_and_tagged() -> None:
    track = _tracks_by_hex()["aircraft:icao:tis123"]
    assert "non_icao" in track.tags
    assert track.geometry is not None


def test_malformed_entries_skipped_without_dropping_snapshot() -> None:
    data = {
        "now": 1781000000.0,
        "aircraft": [
            "not-a-dict",
            {"hex": ""},  # empty identity -> dropped
            {"hex": "a1b2c3", "lat": 40.0, "lon": -95.0, "alt_baro": 1000},
        ],
    }
    tracks = parse_aircraft_snapshot(data, received_at=RECEIVED_AT)
    assert [t.id for t in tracks] == ["aircraft:icao:a1b2c3"]


def test_missing_aircraft_list_returns_empty() -> None:
    assert parse_aircraft_snapshot({"now": 1.0}, received_at=RECEIVED_AT) == []


def test_snapshot_without_now_falls_back_to_received_at() -> None:
    data = {"aircraft": [{"hex": "a1b2c3", "lat": 40.0, "lon": -95.0, "seen": 0.0}]}
    track = parse_aircraft_snapshot(data, received_at=RECEIVED_AT)[0]
    assert track.observed_at == RECEIVED_AT


def test_tracks_round_trip_through_the_bus_codec() -> None:
    # Every produced track must serialize and re-parse as a valid schema-v2 record.
    for track in parse_aircraft_snapshot(_snapshot(), received_at=RECEIVED_AT):
        reparsed = parse_record(dump_record_json(track))
        assert reparsed.id == track.id
        assert reparsed.kind == "track"

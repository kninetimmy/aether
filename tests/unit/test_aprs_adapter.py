"""Unit tests for the local APRS packet parser (PRD §18.3)."""

from datetime import UTC, datetime
from pathlib import Path

from aether.adapters.aprs import SOURCE, parse_aprs_lines, parse_aprs_packet
from aether.schema.validation import dump_record_json, parse_record

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "aprs" / "packets.txt"
# A receipt time chosen to match the fixtures' day-of-month (09) and month (10) so
# parsed DHM/MDHM timestamps resolve cleanly without a month rollback.
RECEIVED_AT = datetime(2026, 10, 9, 12, 0, 0, tzinfo=UTC)

_KT_TO_MS = 1852.0 / 3600.0
_FT_TO_M = 0.3048


def _lines() -> list[str]:
    return FIXTURE.read_text().splitlines()


def _by_id() -> dict[str, object]:
    return {t.id: t for t in parse_aprs_lines(_lines(), received_at=RECEIVED_AT)}


def _one(line: str) -> object:
    return parse_aprs_packet(line, received_at=RECEIVED_AT)


# --- batch / identity ---------------------------------------------------------


def test_fixture_yields_expected_identities() -> None:
    ids = set(_by_id())
    # Mic-E, the telemetry frame, and the junk line are skipped; everything else
    # produces exactly one record (the third-party frame as its inner station).
    assert ids == {
        "aprs:station:KK6ABC-9",
        "aprs:station:N0CALL",
        "aprs:station:K7XYZ-7",
        "aprs:object:LEADER",
        "aprs:object:AID#2",
        "aprs:station:WX1STA",
        "aprs:station:WX2POS",
        "aprs:station:N0STAT",
        "aprs:station:N0AMB",
        "aprs:station:KK6XYZ-1",
    }


def test_identity_is_id_and_correlation_key() -> None:
    track = _by_id()["aprs:station:KK6ABC-9"]
    assert track.id == track.correlation_key == "aprs:station:KK6ABC-9"
    assert track.source == SOURCE
    assert track.locally_received is True


# --- uncompressed position, units, provenance ---------------------------------


def test_uncompressed_position_normalized_to_si() -> None:
    track = _by_id()["aprs:station:KK6ABC-9"]
    assert track.track_type == "aprs_station"
    # 4903.50N -> 49 + 3.50/60 ; 07201.75W -> -(72 + 1.75/60)
    assert track.geometry is not None
    lon, lat, alt = track.geometry.coordinates
    assert abs(lat - (49 + 3.50 / 60)) < 1e-9
    assert abs(lon - -(72 + 1.75 / 60)) < 1e-9
    # 088/036 course/speed ; /A=001234 altitude (feet -> m)
    assert track.heading_deg == 88.0
    assert abs(track.speed_mps - 36.0 * _KT_TO_MS) < 1e-9
    assert abs(track.altitude_m - 1234 * _FT_TO_M) < 1e-9
    assert abs(alt - 1234 * _FT_TO_M) < 1e-9
    assert track.attributes["aprs_symbol"] == "/>"
    assert track.attributes["comment"] == "Hello from the road"


def test_provenance_marks_local_rf() -> None:
    track = _by_id()["aprs:station:KK6ABC-9"]
    assert len(track.provenance) == 1
    prov = track.provenance[0]
    assert prov.source == SOURCE
    assert prov.local_rf is True


def test_timestamped_position_sets_observed_at() -> None:
    # /092345z -> day 09, 23:45 UTC, in the receipt month/year.
    track = _by_id()["aprs:station:N0CALL"]
    assert track.observed_at == datetime(2026, 10, 9, 23, 45, 0, tzinfo=UTC)


# --- compressed position ------------------------------------------------------


def test_compressed_position_decoded() -> None:
    track = _by_id()["aprs:station:K7XYZ-7"]
    assert track.geometry is not None
    lon, lat = track.geometry.coordinates
    assert abs(lat - 49.5) < 1e-4
    assert abs(lon - -72.75) < 1e-4
    # cs bytes "7P": course (ord('7')-33)*4 = 88 deg ; speed 1.08^(ord('P')-33)-1 kt
    assert track.heading_deg == 88.0
    assert abs(track.speed_mps - (1.08 ** (ord("P") - 33) - 1.0) * _KT_TO_MS) < 1e-9


# --- objects and items --------------------------------------------------------


def test_object_uses_object_identity_and_reporter() -> None:
    track = _by_id()["aprs:object:LEADER"]
    assert track.track_type == "aprs_object"
    assert track.label == "LEADER"
    assert "object" in track.tags
    assert track.attributes["reported_by"] == "W1OBJ"
    assert track.heading_deg == 88.0
    assert track.observed_at == datetime(2026, 10, 9, 23, 45, 0, tzinfo=UTC)


def test_item_uses_object_identity() -> None:
    track = _by_id()["aprs:object:AID#2"]
    assert track.track_type == "aprs_object"
    assert "item" in track.tags
    assert track.attributes["reported_by"] == "W1ITM"
    assert track.geometry is not None


def test_killed_object_tagged() -> None:
    track = _one("W1OBJ>APRS:;DEADOBJ  _092345z4903.50N/07201.75W>killed")
    assert track is not None
    assert "killed" in track.tags


# --- weather ------------------------------------------------------------------


def test_positionless_weather_has_no_geometry() -> None:
    track = _by_id()["aprs:station:WX1STA"]
    assert track.geometry is None
    assert "weather" in track.tags
    wx = track.attributes["weather"]
    assert wx["wind_dir_deg"] == 220
    assert wx["wind_speed_mph"] == 4
    assert wx["temp_f"] == 77
    assert wx["temp_c"] == 25.0
    assert wx["humidity_pct"] == 50
    assert wx["pressure_hpa"] == 990.0
    # _10090556 -> Oct 09 05:56 UTC
    assert track.observed_at == datetime(2026, 10, 9, 5, 56, 0, tzinfo=UTC)


def test_position_weather_keeps_geometry_and_does_not_set_heading() -> None:
    track = _by_id()["aprs:station:WX2POS"]
    assert track.geometry is not None
    assert "weather" in track.tags
    # 220/004 here is wind, not station course/speed.
    assert track.heading_deg is None
    assert track.speed_mps is None
    assert track.attributes["weather"]["wind_dir_deg"] == 220


def test_humidity_zero_encodes_full() -> None:
    track = _one("WX9>APRS:_10090556c000s000g000t050h00b10130")
    assert track is not None
    assert track.attributes["weather"]["humidity_pct"] == 100


# --- status -------------------------------------------------------------------


def test_status_frame_has_text_no_geometry() -> None:
    track = _by_id()["aprs:station:N0STAT"]
    assert track.geometry is None
    assert "status" in track.tags
    assert track.attributes["status"] == "Net control tonight 8PM"


# --- ambiguity, third-party, robustness ---------------------------------------


def test_position_ambiguity_tagged() -> None:
    track = _by_id()["aprs:station:N0AMB"]
    assert "position_ambiguity" in track.tags
    assert track.geometry is not None


def test_third_party_unwrapped_to_inner_station() -> None:
    track = _by_id()["aprs:station:KK6XYZ-1"]
    assert "third_party" in track.tags
    assert track.attributes["relayed_by"] == "N0RLY"
    assert track.geometry is not None


def test_nested_third_party_not_unwrapped_twice() -> None:
    # A third-party frame whose inner frame is itself third-party is dropped, not
    # recursed indefinitely.
    line = "A>B:}C>D:}E>F:!4903.50N/07201.75W>x"
    assert _one(line) is None


def test_mic_e_and_unsupported_types_skipped() -> None:
    assert _one("KK6ABC-9>S32U6T,WIDE1-1:`c52l!Tk/'\"4T}Mic-E") is None
    assert _one("N0CALL>APRS:Tmsg telemetry") is None
    assert _one("N0CALL>APRS::WU2Z     :a message") is None


def test_malformed_lines_skipped_without_dropping_batch() -> None:
    lines = [
        "garbage with no delimiters",
        "",
        ">no source",
        "N0CALL>APRS:!4903.50N/07201.75W>good",
    ]
    tracks = parse_aprs_lines(lines, received_at=RECEIVED_AT)
    assert [t.id for t in tracks] == ["aprs:station:N0CALL"]


def test_impossible_coordinates_rejected() -> None:
    # Latitude 9903.50 -> 99 degrees, out of WGS 84 bounds -> whole position drops.
    assert _one("N0CALL>APRS:!9903.50N/07201.75W>bad") is None


def test_overlong_line_rejected() -> None:
    assert _one("N0CALL>APRS:!4903.50N/07201.75W>" + "x" * 2000) is None


def test_every_record_round_trips_through_the_bus_codec() -> None:
    for track in parse_aprs_lines(_lines(), received_at=RECEIVED_AT):
        reparsed = parse_record(dump_record_json(track))
        assert reparsed.id == track.id
        assert reparsed.kind == "track"

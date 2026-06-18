"""Unit tests for the pure military Mode-S classifier (PRD §11.5, MIL-FR-001..005).

Covers the two — and only two — permitted bases (provider ``dbFlags`` bit and
configured ICAO address blocks), the ``None`` (unclassified) default, the
non-authoritative confidence cap, the ``non_icao`` skip, and the lenient config
parser. Also asserts the wiring at both ADS-B edges through the pure parser
functions, so the classifier reaches a real ``TrackRecord`` identically on each.
"""

from datetime import UTC, datetime

from aether.adapters.adsb_provider import AircraftObservation, observation_to_track
from aether.adapters.mil_classify import (
    IcaoRange,
    classify_military,
    parse_ranges,
)
from aether.adapters.readsb import aircraft_to_track

NOW = datetime(2026, 6, 18, 12, 0, 0, tzinfo=UTC)
_US_MIL = parse_ranges("adf7c8-afffff")  # one notional block for the address tests


# --- provider basis (dbFlags bit 0) ----------------------------------------


def test_provider_flag_classifies_military() -> None:
    cls = classify_military("ae1234", db_flags=1)
    assert cls is not None
    assert cls.military is True
    assert cls.basis == "provider"
    assert cls.confidence == "medium"


def test_provider_flag_uses_only_bit_zero() -> None:
    # dbFlags is a bitmask; "interesting"/"PIA"/"LADD" (bits 1-3) are NOT military.
    assert classify_military("abc123", db_flags=2) is None  # interesting only
    assert classify_military("abc123", db_flags=8) is None  # LADD only
    assert classify_military("abc123", db_flags=3) is not None  # military|interesting


def test_provider_flag_rejects_bool_and_non_int() -> None:
    # bool is an int subclass but never a real bitmask; strings/None are no opinion.
    assert classify_military("abc123", db_flags=True) is None
    assert classify_military("abc123", db_flags="1") is None
    assert classify_military("abc123", db_flags=None) is None


# --- address-block basis ----------------------------------------------------


def test_address_block_classifies_in_range() -> None:
    cls = classify_military("adf800", ranges=_US_MIL)
    assert cls is not None
    assert cls.basis == "address_block"
    assert cls.confidence == "low"  # weakest basis: an allocation, not a report


def test_address_block_inclusive_bounds() -> None:
    assert classify_military("adf7c8", ranges=_US_MIL) is not None  # start
    assert classify_military("afffff", ranges=_US_MIL) is not None  # end
    assert classify_military("adf7c7", ranges=_US_MIL) is None  # just below
    assert classify_military("b00000", ranges=_US_MIL) is None  # just above


def test_no_blocks_means_basis_inert() -> None:
    # The shipped default: no ranges configured → address basis never fires.
    assert classify_military("adf800") is None


def test_non_icao_address_skips_block_match() -> None:
    # A ~-prefixed (TIS-B / non-ICAO) address is not a real allocation; even if the
    # bare hex falls in a block it must not be address-classified (only provider can).
    assert classify_military("adf800", non_icao=True, ranges=_US_MIL) is None
    cls = classify_military("adf800", non_icao=True, db_flags=1, ranges=_US_MIL)
    assert cls is not None and cls.basis == "provider"


def test_bad_hex_does_not_crash_address_match() -> None:
    assert classify_military("nothex", ranges=_US_MIL) is None
    assert classify_military(None, ranges=_US_MIL) is None


# --- both / none ------------------------------------------------------------


def test_both_bases_corroborate_without_certainty() -> None:
    cls = classify_military("adf800", db_flags=1, ranges=_US_MIL)
    assert cls is not None
    assert cls.basis == "both"
    # Two non-authoritative signals do NOT become "high"/certain (MIL-FR-005).
    assert cls.confidence != "high"


def test_unclassified_returns_none() -> None:
    assert classify_military("3c6dd2", db_flags=0, ranges=_US_MIL) is None


# --- parse_ranges -----------------------------------------------------------


def test_parse_ranges_pairs_singletons_and_whitespace() -> None:
    ranges = parse_ranges("  adf7c8-afffff , 43c000-43cfff ,  abcdef ")
    assert ranges == (
        IcaoRange(0xADF7C8, 0xAFFFFF),
        IcaoRange(0x43C000, 0x43CFFF),
        IcaoRange(0xABCDEF, 0xABCDEF),
    )


def test_parse_ranges_skips_malformed_entries() -> None:
    # Empty string, non-hex, and inverted bounds are dropped, not fatal (PRD §37).
    assert parse_ranges("") == ()
    assert parse_ranges("zzz, 10-1, , adf7c8-afffff") == (IcaoRange(0xADF7C8, 0xAFFFFF),)


# --- wiring: the same classifier reaches a TrackRecord at both edges --------


def test_local_edge_sets_classification_from_dbflags() -> None:
    ac = {"hex": "ae1234", "lat": 40.0, "lon": -95.0, "dbFlags": 1}
    track = aircraft_to_track(ac, snapshot_now=NOW, received_at=NOW)
    assert track is not None
    assert track.classification is not None
    assert track.classification.basis == "provider"
    assert "military" in track.tags


def test_local_edge_address_block_needs_config() -> None:
    ac = {"hex": "adf900", "lat": 40.0, "lon": -95.0}
    assert aircraft_to_track(ac, snapshot_now=NOW, received_at=NOW).classification is None
    track = aircraft_to_track(ac, snapshot_now=NOW, received_at=NOW, mil_ranges=_US_MIL)
    assert track is not None
    assert track.classification is not None
    assert track.classification.basis == "address_block"


def test_network_edge_matches_local_edge() -> None:
    obs = AircraftObservation(
        icao_hex="adf900", observed_at=NOW, received_at=NOW, attributes={"dbFlags": 1}
    )
    track = observation_to_track(obs, mil_ranges=_US_MIL)
    assert track.classification is not None
    assert track.classification.basis == "both"  # provider flag + address block
    assert "military" in track.tags

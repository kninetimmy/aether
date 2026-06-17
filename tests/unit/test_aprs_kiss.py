"""Unit tests for the pure KISS de-framing + AX.25 UI decode (PRD §18.3, §37).

No sockets, no async: canned byte frames in, TNC2 strings out. Round-trips the
:mod:`aether.adapters.aprs_fake_feeder` builders against the decoder, covers the
error-prone bit fields (SSID shift, digipeater '*', C/R-not-'*'), the framing
edge cases (split reads, empty/back-to-back FEND, over-cap remainder), and asserts
malformed frames yield ``None`` rather than raising (failure isolation).
"""

from datetime import UTC, datetime

from tests.fixtures.aprs import kiss_frames as kf

from aether.adapters.aprs import parse_aprs_packet
from aether.adapters.aprs_fake_feeder import build_ax25_ui, build_kiss_data_frame
from aether.adapters.aprs_kiss import (
    FEND,
    FESC,
    MAX_LINE_BYTES,
    TFEND,
    TFESC,
    decode_ax25_ui,
    iter_kiss_frames,
    kiss_frame_to_tnc2,
    kiss_unescape,
)

T0 = datetime(2026, 6, 17, 12, 0, 0, tzinfo=UTC)


# --- kiss_unescape ------------------------------------------------------------


def test_unescape_translates_fend_and_fesc() -> None:
    raw = bytes([0x01, FESC, TFEND, 0x02, FESC, TFESC, 0x03])
    assert kiss_unescape(raw) == bytes([0x01, FEND, 0x02, FESC, 0x03])


def test_unescape_is_lenient_on_trailing_fesc() -> None:
    # A lone trailing FESC has no following byte; emit it as-is, don't crash.
    assert kiss_unescape(bytes([0x01, FESC])) == bytes([0x01, FESC])


def test_unescape_roundtrips_a_payload_with_embedded_delimiters() -> None:
    # A 0xC0 inside the AX.25 payload must survive framing as FESC TFEND.
    ax25 = build_ax25_ui("N0CALL", "APRS", [], bytes([0x21, FEND, FESC, 0x22]))
    frame = build_kiss_data_frame(ax25)
    de = kiss_unescape(frame[1:-1])  # strip the bracketing FENDs
    assert de[1:] == ax25  # type byte then the exact AX.25 frame back


# --- iter_kiss_frames ---------------------------------------------------------


def test_iter_splits_multiple_frames_and_ignores_empty_chunks() -> None:
    a = build_kiss_data_frame(build_ax25_ui("N0CALL", "APRS", [], b">one"))
    b = build_kiss_data_frame(build_ax25_ui("W1ABC", "APRS", [], b">two"))
    # Leading + back-to-back FENDs produce empty chunks that must be ignored.
    stream = bytes([FEND]) + a + b
    frames, remainder = iter_kiss_frames(stream)
    assert len(frames) == 2
    assert remainder == b""
    assert kiss_frame_to_tnc2(frames[0]) == "N0CALL>APRS:>one"
    assert kiss_frame_to_tnc2(frames[1]) == "W1ABC>APRS:>two"


def test_iter_returns_partial_remainder_across_reads() -> None:
    frame = build_kiss_data_frame(build_ax25_ui("N0CALL", "APRS", [], b">split"))
    cut = len(frame) - 4
    frames, remainder = iter_kiss_frames(frame[:cut])  # no closing FEND yet
    assert frames == []
    frames2, remainder2 = iter_kiss_frames(remainder + frame[cut:])
    assert remainder2 == b""
    assert kiss_frame_to_tnc2(frames2[0]) == "N0CALL>APRS:>split"


def test_iter_buffer_without_fend_is_all_remainder() -> None:
    frames, remainder = iter_kiss_frames(b"\x00\x01\x02")
    assert frames == []
    assert remainder == b"\x00\x01\x02"


def test_iter_discards_over_cap_remainder() -> None:
    blob = b"\x00" * (MAX_LINE_BYTES + 10)  # no FEND, grows unbounded otherwise
    frames, remainder = iter_kiss_frames(blob)
    assert frames == []
    assert remainder == b""  # discarded to bound memory (PRD §37)


# --- kiss_frame_to_tnc2 gating -----------------------------------------------


def test_non_data_type_frame_is_gated_out() -> None:
    # TYPE low-nibble 6 (SetHardware) is not a DATA frame -> None.
    assert kiss_frame_to_tnc2(bytes([0x06, 0x01, 0x02])) is None


def test_command_frame_low_nibble_nonzero_gated_out() -> None:
    assert kiss_frame_to_tnc2(bytes([0xFF])) is None  # exit-KISS
    assert kiss_frame_to_tnc2(b"") is None  # empty de-escaped frame


def test_data_frame_high_nibble_port_is_ignored() -> None:
    ax25 = build_ax25_ui("N0CALL", "APRS", [], b">port")
    frame = build_kiss_data_frame(ax25, port=5)  # port in the high nibble
    chunks, _ = iter_kiss_frames(frame)  # de-frame first (the real contract)
    assert kiss_frame_to_tnc2(chunks[0]) == "N0CALL>APRS:>port"


# --- decode_ax25_ui: known frames, SSID, '*' ---------------------------------


def test_decode_known_frame_exact_tnc2_line() -> None:
    ax25 = build_ax25_ui("N0CALL", "APRS", [("WIDE1-1", False)], b"!4903.50N/07201.75W>x")
    assert decode_ax25_ui(ax25) == "N0CALL>APRS,WIDE1-1:!4903.50N/07201.75W>x"


def test_decode_omits_ssid_zero() -> None:
    ax25 = build_ax25_ui("N0CALL", "APRS", [], b">no ssid")
    line = decode_ax25_ui(ax25)
    assert line is not None
    assert line.startswith("N0CALL>APRS:")  # no "-0" on either call


def test_decode_renders_high_ssid_via_shift_not_mask() -> None:
    # SSID 11 lives in bits 4..1; the 0x0F-without-shift bug would render '-5'.
    ax25 = build_ax25_ui("N0CALL-11", "APRS", [], b">x")
    line = decode_ax25_ui(ax25)
    assert line is not None
    assert line.startswith("N0CALL-11>APRS")


def test_decode_marks_only_h_set_digis_with_star() -> None:
    ax25 = build_ax25_ui("K7XYZ-7", "APRS", [("WIDE1", True), ("WIDE2-1", False)], b">x")
    assert decode_ax25_ui(ax25) == "K7XYZ-7>APRS,WIDE1*,WIDE2-1:>x"


def test_decode_cr_bit_on_src_dest_never_produces_star() -> None:
    # build_ax25_ui sets the C/R bit (0x80) on src+dest; it must not become a '*'.
    ax25 = build_ax25_ui("N0CALL", "APRS", [], b">x")
    line = decode_ax25_ui(ax25)
    assert line is not None
    assert "*" not in line


# --- decode_ax25_ui: rejections (all -> None, never raise) --------------------


def test_decode_rejects_too_short_frame() -> None:
    assert decode_ax25_ui(b"\x00\x01\x02\x03") is None


def test_decode_rejects_no_end_of_address_bit() -> None:
    # 10 address slots none with the end-of-address bit set: bounded -> None.
    bogus = bytes([0x82, 0x84, 0x86, 0x88, 0x8A, 0x8C, 0x60]) * 10
    assert decode_ax25_ui(bogus) is None


def test_decode_rejects_addresses_running_to_frame_end() -> None:
    # Address block (dest+source+digi, end-of-address bit set on the digi) consumes
    # every octet, with no control/PID after. Must be contained as None, not raise
    # IndexError out of the decoder (PRD §37 "no exception escapes").
    full = build_ax25_ui("N0CALL", "APRS", [("WIDE1-1", False)], b">x")
    assert decode_ax25_ui(full[:21]) is None  # 3 addresses = 21 octets; nothing after


def test_decode_rejects_non_ui_control() -> None:
    ax25 = bytearray(build_ax25_ui("N0CALL", "APRS", [], b">x"))
    ax25[14] = 0x00  # control field (after 2x7 address octets): not UI 0x03
    assert decode_ax25_ui(bytes(ax25)) is None


def test_decode_rejects_non_f0_pid() -> None:
    ax25 = bytearray(build_ax25_ui("N0CALL", "APRS", [], b">x"))
    ax25[15] = 0xCF  # PID: not 0xF0
    assert decode_ax25_ui(bytes(ax25)) is None


def test_decode_rejects_non_printable_callsign() -> None:
    ax25 = bytearray(build_ax25_ui("N0CALL", "APRS", [], b">x"))
    ax25[0] = 0x02  # decodes to 0x01, not printable -> Ax25DecodeError -> None
    assert decode_ax25_ui(bytes(ax25)) is None


# --- canned fixture frames decode to their documented lines ------------------


def test_fixture_frames_decode_to_documented_tnc2_lines() -> None:
    for kiss_frame, expected in (
        (kf.KISS_POSITION, kf.TNC2_POSITION),
        (kf.KISS_OBJECT, kf.TNC2_OBJECT),
        (kf.KISS_STATUS, kf.TNC2_STATUS),
        (kf.KISS_DIGIPEATED, kf.TNC2_DIGIPEATED),
    ):
        chunks, remainder = iter_kiss_frames(kiss_frame)
        assert remainder == b""
        assert kiss_frame_to_tnc2(chunks[0]) == expected


def test_fixture_malformed_short_frame_returns_none() -> None:
    assert decode_ax25_ui(kf.AX25_MALFORMED_SHORT) is None


# --- decoded line feeds the parser -------------------------------------------


def test_decoded_line_parses_to_expected_track() -> None:
    ax25 = build_ax25_ui("N0CALL", "APRS", [("WIDE1-1", False)], b"!4903.50N/07201.75W>Test")
    chunks, _ = iter_kiss_frames(build_kiss_data_frame(ax25))
    line = kiss_frame_to_tnc2(chunks[0])
    assert line is not None
    track = parse_aprs_packet(line, received_at=T0)
    assert track is not None
    assert track.id == "aprs:station:N0CALL"
    assert track.locally_received is True

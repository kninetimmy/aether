"""Canned KISS/AX.25 byte fixtures for the runtime tests (M2.2b).

Keeps the byte literals out of the test bodies and documents the exact TNC2 line
each frame must decode to. Built via :mod:`aether.adapters.aprs_fake_feeder`'s pure
builders, which are the inverse of :mod:`aether.adapters.aprs_kiss`'s decoder — so
these double as an encode/decode round-trip oracle.

The existing ``packets.txt`` (TNC2 text) stays the *parser's* fixture; these KISS
byte frames are the *runtime's* fixture (the socket + framing + AX.25 path).
"""

from aether.adapters.aprs_fake_feeder import build_ax25_ui, build_kiss_data_frame

#: One raw AX.25 UI frame: an uncompressed position from a transmitting station,
#: heard via WIDE1-1 (not yet repeated, so no '*').
AX25_POSITION = build_ax25_ui(
    "N0CALL",
    "APRS",
    [("WIDE1-1", False)],
    b"!4903.50N/07201.75W>Test position",
)
TNC2_POSITION = "N0CALL>APRS,WIDE1-1:!4903.50N/07201.75W>Test position"

#: An object report.
AX25_OBJECT = build_ax25_ui(
    "W1OBJ",
    "APRS",
    [("WIDE2-1", False)],
    b";LEADER   *092345z4903.50N/07201.75W>088/036Leading edge",
)
TNC2_OBJECT = "W1OBJ>APRS,WIDE2-1:;LEADER   *092345z4903.50N/07201.75W>088/036Leading edge"

#: A status (no position).
AX25_STATUS = build_ax25_ui("N0STAT", "APRS", [], b">Net control tonight 8PM")
TNC2_STATUS = "N0STAT>APRS:>Net control tonight 8PM"

#: A digipeated position: the first digi's H bit is set, so a '*' follows it; the
#: SSID-7 source exercises the SSID render. The second digi is not repeated.
AX25_DIGIPEATED = build_ax25_ui(
    "K7XYZ-7",
    "APRS",
    [("WIDE1", True), ("WIDE2-1", False)],
    b"=4903.50N/07201.75W-Digipeated",
)
TNC2_DIGIPEATED = "K7XYZ-7>APRS,WIDE1*,WIDE2-1:=4903.50N/07201.75W-Digipeated"

#: A frame the parser recognizes and skips (telemetry) — exercises records_rejected.
AX25_DEFERRED = build_ax25_ui("N0TLM", "APRS", [], b"T#005,199,000,255,073,123,01101001")

#: KISS-framed versions of the above for the de-framing tests.
KISS_POSITION = build_kiss_data_frame(AX25_POSITION)
KISS_OBJECT = build_kiss_data_frame(AX25_OBJECT)
KISS_STATUS = build_kiss_data_frame(AX25_STATUS)
KISS_DIGIPEATED = build_kiss_data_frame(AX25_DIGIPEATED)
KISS_DEFERRED = build_kiss_data_frame(AX25_DEFERRED)

#: A deliberately malformed AX.25 frame: too short to hold dest+source+control+pid.
AX25_MALFORMED_SHORT = b"\x00\x01\x02\x03"

#: A KISS command (non-DATA) frame — TYPE low-nibble != 0, must be gated out.
KISS_NON_DATA = bytes([0x06, 0x01, 0x02])  # SetHardware payload, no framing

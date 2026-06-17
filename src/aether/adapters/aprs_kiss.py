"""Pure KISS de-framing + AX.25 UI-frame decode to TNC2 monitor lines (PRD §18.3).

The byte-level edge between Dire Wolf's KISS TCP port (KISSPORT, default 8001) and
the pure :mod:`aether.adapters.aprs` TNC2 parser. Bytes in, ``"SRC>DEST,DIGI*:info"``
strings out — no sockets, no async, fully unit-testable against canned frames. The
runtime that owns the socket and reconnect is :mod:`aether.adapters.local_aprs`.

Receive-only by construction: this module only *decodes* received frames. It never
builds a frame to send and the KISS socket is read-only from aether's side — the
KISS protocol is bidirectional (an app that *writes* a KISS data frame asks Dire
Wolf to transmit), so the load-bearing guardrail is that nothing here, and nothing
in the runtime, ever writes to that socket (PRD §18.3 "Never send packets back to
KISS/AGW for transmission").

Byte layouts below are cited to the canonical sources they were verified against:

- KISS framing — KA9Q "The KISS TNC" spec (ka9q.net/papers/kiss.html): FEND/FESC
  escaping; type byte high-nibble = port, low-nibble = command; command 0 = data.
- AX.25 address/control/PID — Dire Wolf ``src/ax25_pad.h`` (the exact decoder this
  reads from) and AX.25 v2.2 (ax25.net): 7-byte addresses, callsign chars stored
  left-shifted by one, SSID octet ``H R R SSID 0``, UI control 0x03, PID 0xF0.

Failure isolation (PRD §37): a malformed frame yields ``None`` (logged at debug),
never an exception out of :func:`kiss_frame_to_tnc2`; one bad frame in a TCP read
never wedges the stream.
"""

import logging

log = logging.getLogger(__name__)

# --- KISS framing constants (KA9Q KISS spec) ---------------------------------
#: Frame delimiter; brackets every KISS frame on the wire.
FEND = 0xC0
#: Escape byte; the next byte is a translated FEND/FESC.
FESC = 0xDB
#: Transposed FEND — ``FESC TFEND`` un-escapes to ``FEND`` (0xC0) in the payload.
TFEND = 0xDC
#: Transposed FESC — ``FESC TFESC`` un-escapes to ``FESC`` (0xDB) in the payload.
TFESC = 0xDD
#: Type-byte low nibble for a data frame (the rest is raw AX.25). 0xFF = exit-KISS,
#: 1..6 are TX-timing/SetHardware commands we never originate and ignore on read.
KISS_DATA_CMD = 0x00

# --- AX.25 constants (Dire Wolf src/ax25_pad.h, AX.25 v2.2) -------------------
#: UI (unnumbered information) control field value; APRS frames are always UI.
AX25_UI_CONTROL = 0x03
#: PID "no layer 3 protocol" — the protocol id APRS uses.
AX25_PID_NO_LAYER_3 = 0xF0
#: Each AX.25 address is 6 callsign octets + 1 SSID octet.
ADDR_LEN = 7
#: Minimum UI frame: 7 (dest) + 7 (source) + 1 (control) + 1 (pid), before info.
MIN_AX25_LEN = 16
#: Up to 8 digipeaters in the address path (ax25_pad.h AX25_MAX_DIGIS).
MAX_DIGIS = 8
#: Destination + source + up to 8 digipeaters (ax25_pad.h AX25_MAX_ADDRS = 10).
#: Derived from MAX_DIGIS so the two can't drift; the address loop is bounded by it.
MAX_ADDRS = 2 + MAX_DIGIS

#: SSID octet bit masks (ax25_pad.h: "Bits: H R R SSID 0").
_SSID_LAST_MASK = 0x01  # bit 0 — address-extension / end-of-address
_SSID_SSID_MASK = 0x1E  # bits 4..1 — the SSID value (0..15), stored left-shifted
_SSID_H_CR_MASK = 0x80  # bit 7 — H (has-been-repeated) on digis / C-R on src+dest

#: A wedged or garbage stream with no FEND must not grow the carry-over buffer
#: without bound; drop a remainder past this before it can (PRD §37).
MAX_LINE_BYTES = 4096


class Ax25DecodeError(Exception):
    """A frame is not a well-formed AX.25 UI frame; the caller skips it."""


def kiss_unescape(payload: bytes) -> bytes:
    """Reverse KISS byte-stuffing (KA9Q spec).

    ``FESC TFEND`` -> ``FEND`` (0xC0); ``FESC TFESC`` -> ``FESC`` (0xDB). A
    trailing lone ``FESC`` or an unknown escape pair is emitted leniently (the
    raw following byte) rather than raising — a corrupt escape costs at most one
    byte, not a dropped read. This MUST run before the type byte is read or the
    AX.25 frame parsed, because a 0xC0 inside the payload arrives as ``0xDB
    0xDC`` and would otherwise read as a frame boundary.
    """
    out = bytearray()
    i = 0
    n = len(payload)
    while i < n:
        byte = payload[i]
        if byte == FESC and i + 1 < n:
            nxt = payload[i + 1]
            if nxt == TFEND:
                out.append(FEND)
            elif nxt == TFESC:
                out.append(FESC)
            else:
                out.append(nxt)  # lenient: unknown escape -> raw next byte
            i += 2
            continue
        out.append(byte)
        i += 1
    return bytes(out)


def iter_kiss_frames(buffer: bytes) -> tuple[list[bytes], bytes]:
    """Split a KISS byte stream on FEND into complete frames + a partial remainder.

    The socket reader appends each TCP read to a carry-over buffer and calls this.
    A KISS stream is ``[FEND] payload FEND payload FEND ...``; each chunk strictly
    between two FENDs is a candidate frame. Leading/trailing/back-to-back FENDs
    produce empty chunks, which the KA9Q spec allows — they are ignored. The bytes
    after the last FEND (no closing FEND yet) are returned as ``remainder`` so the
    next read can complete them; a buffer with no FEND at all is entirely
    remainder. Complete chunks are returned AS-IS — still escaped, still including
    the leading type byte; un-escaping is owned by :func:`kiss_frame_to_tnc2`.

    If the remainder grows past :data:`MAX_LINE_BYTES` with no FEND in sight, it is
    a wedged/garbage stream and is discarded so the buffer can't grow unbounded
    (PRD §37 failure isolation).
    """
    frames: list[bytes] = []
    start = 0
    remainder = b""
    n = len(buffer)
    i = 0
    while i < n:
        if buffer[i] == FEND:
            chunk = buffer[start:i]
            if chunk:  # ignore empty chunks between back-to-back/leading FENDs
                frames.append(chunk)
            start = i + 1
        i += 1
    remainder = buffer[start:]
    if len(remainder) > MAX_LINE_BYTES:
        log.debug("discarding %d-byte KISS remainder with no frame boundary", len(remainder))
        remainder = b""
    return frames, remainder


def _decode_callsign(six: bytes) -> str:
    """Decode 6 callsign octets to ASCII, validating and right-stripping padding.

    Each octet stores an ASCII char shifted LEFT by one bit on the wire (the low
    bit is the HDLC address-extension bit), so decode with a RIGHT shift:
    ``ch = (byte >> 1) & 0x7F`` (ax25_pad.h / AX.25 v2.2). Callsigns are
    uppercase, space-padded to 6; any non ``{A-Z, 0-9, space}`` char means a
    corrupt frame -> :class:`Ax25DecodeError` (never pass garbage to the parser).
    """
    chars: list[str] = []
    for octet in six:
        ch = (octet >> 1) & 0x7F
        if not (0x30 <= ch <= 0x39 or 0x41 <= ch <= 0x5A or ch == 0x20):
            raise Ax25DecodeError(f"non-callsign char {ch:#x}")
        chars.append(chr(ch))
    return "".join(chars).rstrip(" ")


def _decode_address(octets: bytes, *, is_digi: bool) -> tuple[str, bool]:
    """Decode one 7-byte AX.25 address to ``"CALL[-SSID]"`` plus its repeated flag.

    SSID value lives in bits 4..1, so ``ssid = (byte >> 1) & 0x0F`` — masking
    ``0x0F`` *without* the shift is the classic bug (the value is not in bits
    3..0). SSID-0 is omitted (never emit ``-0``); 1..15 render ``-1``..``-15``
    (ax25_pad.h SSID_SSID_MASK=0x1e/SHIFT=1).

    Bit 7 is the H "has-been-repeated" flag only on digipeater addresses; on the
    destination and source it is the command/response bit (normally 1 for APRS)
    and must NOT drive a TNC2 ``*``. So ``has_been_repeated`` is reported only when
    ``is_digi`` (ax25_pad.h: "H for digipeaters ... For source & destination it is
    called command/response").
    """
    ssid_octet = octets[6]
    call = _decode_callsign(octets[0:6])
    ssid = (ssid_octet & _SSID_SSID_MASK) >> 1
    rendered = f"{call}-{ssid}" if ssid != 0 else call
    has_been_repeated = is_digi and bool(ssid_octet & _SSID_H_CR_MASK)
    return rendered, has_been_repeated


def decode_ax25_ui(frame: bytes) -> str | None:
    """Decode a raw AX.25 UI frame to a TNC2 line, or ``None`` if it isn't one.

    Returns ``"SOURCE>DEST,DIGI1,DIGI2*:info"`` (the format
    :func:`aether.adapters.aprs.parse_aprs_packet` consumes) for a UI / PID-0xF0
    frame, else ``None`` (non-UI control, wrong PID, too short, no end-of-address
    bit, or a corrupt callsign). Address order on the wire is DEST then SOURCE, but
    the TNC2 line is ``SOURCE>DEST`` — do not transpose. A ``*`` is appended to
    each digipeater whose H bit is set (Dire-Wolf-matching, safe), never to
    source/dest. Any malformation is contained as ``None`` (PRD §37); no exception
    escapes.
    """
    try:
        if len(frame) < MIN_AX25_LEN:
            return None

        # --- addresses: dest, source, then 0..8 digipeaters --------------------
        addresses: list[tuple[str, bool]] = []
        addr_end = -1
        for slot in range(MAX_ADDRS):
            offset = slot * ADDR_LEN
            octets = frame[offset : offset + ADDR_LEN]
            if len(octets) < ADDR_LEN:
                return None  # truncated mid-address
            addresses.append(_decode_address(octets, is_digi=slot >= 2))
            if octets[6] & _SSID_LAST_MASK:  # end-of-address bit
                addr_end = offset + ADDR_LEN
                break
        if addr_end < 0:  # no end-of-address bit within 10 addresses -> malformed
            return None
        if len(addresses) < 2:  # need at least dest + source
            return None

        # --- control + PID: APRS is always UI / no-layer-3 ---------------------
        # A truncated frame whose addresses consume every octet (end-of-address bit
        # set on the last one) leaves no room for the control/PID pair. Bail rather
        # than index past the end — an IndexError here is not an Ax25DecodeError and
        # would escape this function's "contained as None" contract (PRD §37).
        if len(frame) < addr_end + 2:
            return None
        if frame[addr_end] != AX25_UI_CONTROL:
            return None
        if frame[addr_end + 1] != AX25_PID_NO_LAYER_3:
            return None

        # --- info: runs to end of frame; KISS already stripped the FCS ---------
        info = frame[addr_end + 2 :].decode("ascii", "replace")

        # --- render SOURCE>DEST,DIGIs:info ------------------------------------
        dest_str = addresses[0][0]
        source_str = addresses[1][0]
        line = f"{source_str}>{dest_str}"
        for digi_str, repeated in addresses[2:]:
            line += f",{digi_str}"
            if repeated:
                line += "*"  # this hop's H bit is set: heard via this digipeater
        return f"{line}:{info}"
    except Ax25DecodeError as exc:
        log.debug("skipping malformed AX.25 frame: %s", exc)
        return None


def kiss_frame_to_tnc2(kiss_frame: bytes) -> str | None:
    """Decode one extracted (still-escaped) KISS chunk to a TNC2 line, or ``None``.

    ``kiss_frame`` is a single chunk from :func:`iter_kiss_frames` including the
    leading type byte. Steps: un-escape first (so a payload 0xC0 isn't misread),
    read and drop exactly one type byte, gate on ``(type & 0x0F) == 0`` (DATA;
    ignore exit-KISS 0xFF and TX-timing/SetHardware command frames — those are
    never RF traffic), then decode the remaining raw AX.25 frame. The high nibble
    of the type byte is the KISS port and is ignored.
    """
    de_escaped = kiss_unescape(kiss_frame)
    if not de_escaped:
        return None
    type_byte = de_escaped[0]
    if (type_byte & 0x0F) != KISS_DATA_CMD:
        return None  # command / non-data frame: not RF traffic
    return decode_ax25_ui(de_escaped[1:])

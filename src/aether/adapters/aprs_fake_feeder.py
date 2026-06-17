"""No-hardware feeder: a fake TCP KISS server streaming canned APRS frames.

Stands in for a real Dire Wolf KISS port so the M2.2b local APRS path runs with no
SDR and no Dire Wolf (PRD §6 no-hardware gate, §34 "every source ships a
fake/replay feeder"). Unlike ADS-B (a file snapshot), APRS has no on-disk form, so
the stand-in is a *server* that emits real KISS-framed AX.25 frames over TCP —
which exercises the full socket + KISS framing + AX.25 decode path end to end.

The pure builders (:func:`build_ax25_ui`, :func:`build_kiss_data_frame`) are reused
by the unit tests as the exact inverse of the decoder; ``__main__`` runs the server
forever. Point the adapter at it::

    python -m aether.adapters.aprs_fake_feeder 127.0.0.1 8001 &
    AETHER_DEMO_SOURCE=0 AETHER_LOCAL_APRS=1 AETHER_LOCAL_APRS_PORT=8001 \
        uvicorn aether.backend.main:app --app-dir src

RECEIVE-ONLY / test-double: this only *writes received-direction DATA frames to a
connecting reader* — it imitates a TNC handing decoded frames to an app. It never
transmits on RF, never writes to a real TNC, and aether's reader never writes back
(reads from the client are ignored).
"""

import asyncio
import sys

from aether.adapters.aprs_kiss import (
    AX25_PID_NO_LAYER_3,
    AX25_UI_CONTROL,
    FEND,
    FESC,
    KISS_DATA_CMD,
    TFEND,
    TFESC,
)

#: RR reserved bits, set to 1 1 per AX.25 (Dire Wolf SSID_RR_MASK=0x60).
_SSID_RR_BITS = 0x60
#: Command/response (src+dest) bit — normally 1 for APRS (Dire Wolf SSID_H_MASK).
_SSID_CR_BIT = 0x80
#: H "has-been-repeated" bit — same bit position, meaningful on digipeaters.
_SSID_H_BIT = 0x80
#: End-of-address bit, set on the final address octet only.
_SSID_LAST_BIT = 0x01


def _encode_address(call: str, ssid: int, *, last: bool, flag_bit: int) -> bytes:
    """Encode one ``CALL`` + SSID into 7 AX.25 octets (inverse of the decoder).

    Callsign chars are space-padded to 6 and stored LEFT-shifted by one bit
    (``byte = ord(ch) << 1``); the SSID octet is ``H/CR | RR | (ssid << 1) | last``
    (Dire Wolf src/ax25_pad.h bit layout). ``flag_bit`` is the C/R bit on
    source/dest, or the H bit on a repeated digipeater, or 0.
    """
    padded = call.upper().ljust(6)[:6]
    octets = bytearray((ord(ch) << 1) & 0xFF for ch in padded)
    ssid_octet = flag_bit | _SSID_RR_BITS | ((ssid & 0x0F) << 1)
    if last:
        ssid_octet |= _SSID_LAST_BIT
    octets.append(ssid_octet)
    return bytes(octets)


def _split_call(addr: str) -> tuple[str, int]:
    """Split ``"CALL-SSID"`` into ``(call, ssid)``; a bare call is SSID 0."""
    if "-" in addr:
        call, _, ssid_s = addr.partition("-")
        return call, int(ssid_s)
    return addr, 0


def build_ax25_ui(
    source: str,
    dest: str,
    digis: list[tuple[str, bool]],
    info: bytes,
) -> bytes:
    """Build a raw AX.25 UI frame — the exact inverse of :func:`decode_ax25_ui`.

    Address order on the wire is DEST, SOURCE, then digipeaters. Source and dest
    carry the C/R bit (1); a digipeater carries the H bit when its ``(call, True)``
    flag says it has been repeated. The end-of-address bit is set on the last
    address only, then control 0x03 (UI) and PID 0xF0, then the info bytes.
    """
    addrs: list[bytes] = []
    last_index = 1 + len(digis)  # dest=0, source=1, digis=2..

    dcall, dssid = _split_call(dest)
    addrs.append(_encode_address(dcall, dssid, last=last_index == 0, flag_bit=_SSID_CR_BIT))
    scall, sssid = _split_call(source)
    addrs.append(_encode_address(scall, sssid, last=last_index == 1, flag_bit=_SSID_CR_BIT))
    for i, (digi, repeated) in enumerate(digis):
        gcall, gssid = _split_call(digi)
        addrs.append(
            _encode_address(
                gcall,
                gssid,
                last=(2 + i) == last_index,
                flag_bit=_SSID_H_BIT if repeated else 0,
            )
        )

    return b"".join(addrs) + bytes([AX25_UI_CONTROL, AX25_PID_NO_LAYER_3]) + info


def build_kiss_data_frame(ax25: bytes, *, port: int = 0) -> bytes:
    """Wrap a raw AX.25 frame in a KISS DATA frame: ``FEND TYPE escaped... FEND``.

    Type byte = ``(port << 4) | KISS_DATA_CMD``; the payload (type + AX.25) is
    byte-stuffed (FEND -> FESC TFEND, FESC -> FESC TFESC) so no payload byte can be
    mistaken for the frame boundary (KA9Q KISS spec).
    """
    type_byte = ((port & 0x0F) << 4) | KISS_DATA_CMD
    payload = bytes([type_byte]) + ax25
    escaped = bytearray()
    for byte in payload:
        if byte == FEND:
            escaped += bytes([FESC, TFEND])
        elif byte == FESC:
            escaped += bytes([FESC, TFESC])
        else:
            escaped.append(byte)
    return bytes([FEND]) + bytes(escaped) + bytes([FEND])


#: A handful of representative real APRS infos, one per common data type, built into
#: KISS DATA frames. The digipeated frame sets a digi's H bit to exercise the ``*``.
CANNED_FRAMES: list[bytes] = [
    # Uncompressed position via WIDE1-1 (a transmitting station).
    build_kiss_data_frame(
        build_ax25_ui(
            "N0CALL",
            "APRS",
            [("WIDE1-1", False)],
            b"!4903.50N/07201.75W>Test position",
        )
    ),
    # Object report.
    build_kiss_data_frame(
        build_ax25_ui(
            "W1OBJ",
            "APRS",
            [("WIDE2-1", False)],
            b";LEADER   *092345z4903.50N/07201.75W>088/036Leading edge",
        )
    ),
    # Status.
    build_kiss_data_frame(
        build_ax25_ui("N0STAT", "APRS", [], b">Net control tonight 8PM"),
    ),
    # Digipeated position: the digi's H bit is set, so the TNC2 line shows a '*'.
    build_kiss_data_frame(
        build_ax25_ui(
            "K7XYZ-7",
            "APRS",
            [("WIDE1", True), ("WIDE2-1", False)],
            b"=4903.50N/07201.75W-Digipeated",
        )
    ),
    # An empty back-to-back-FEND frame (ignored by the de-framer) to exercise the
    # framing path, immediately followed by a non-DATA TYPE frame (gated out).
    bytes([FEND, FEND]),
    bytes([FEND, 0x06, 0x01, 0x02, FEND]),  # TYPE 0x06 SetHardware: not DATA
]


async def serve_kiss(
    host: str,
    port: int,
    *,
    frames: list[bytes] | None = None,
    interval_s: float = 0.5,
    loop_forever: bool = True,
) -> asyncio.Server:
    """Start a fake KISS server that streams ``frames`` to each connecting reader.

    Returns the started :class:`asyncio.Server`; the caller awaits
    ``server.serve_forever()`` (the ``__main__`` path) or keeps it for the test's
    lifetime. Each client connection gets the frames written one per ``interval_s``,
    looping while ``loop_forever`` so a reader always has data to decode. Reads from
    the client are ignored — the server never expects the reader to write anything
    (aether is read-only).
    """
    payload = list(frames) if frames is not None else list(CANNED_FRAMES)

    async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                for frame in payload:
                    writer.write(frame)
                    await writer.drain()
                    await asyncio.sleep(interval_s)
                if not loop_forever:
                    break
        except (ConnectionResetError, BrokenPipeError):
            pass  # reader went away; nothing to clean up but the socket
        finally:
            writer.close()

    return await asyncio.start_server(_handle, host, port)


async def _serve_forever(host: str, port: int, interval_s: float) -> None:
    server = await serve_kiss(host, port, interval_s=interval_s)
    addrs = ", ".join(str(s.getsockname()) for s in (server.sockets or ()))
    print(f"fake KISS server -> {addrs} every {interval_s}s (Ctrl-C to stop)")
    async with server:
        await server.serve_forever()


def _main(argv: list[str]) -> int:
    if len(argv) < 3:
        print("usage: python -m aether.adapters.aprs_fake_feeder <host> <port> [interval_s]")
        return 2
    host = argv[1]
    port = int(argv[2])
    interval_s = float(argv[3]) if len(argv) > 3 else 0.5
    try:
        asyncio.run(_serve_forever(host, port, interval_s))
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))

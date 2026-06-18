"""No-hardware feeder: a fake APRS-IS TCP server streaming canned TNC2 lines.

Stands in for a real APRS-IS server so the M3.4 APRS-IS path — and the local↔APRS-IS
fusion path — runs with no live APRS-IS account and no network (PRD §6 no-hardware
gate, §34 "every source ships a fake/replay feeder"). It is the APRS-IS sibling of
:mod:`aether.adapters.aprs_fake_feeder` (the fake KISS server) and
:mod:`aether.adapters.network_adsb_fake_feeder` (the fake ADS-B provider): real,
production-wired code selected by config, never a live call.

On each connection it reads and *ignores* the client's login line (a stand-in for a
real server's filtered feed — aether only ever sends its login), emits a couple of
``#`` server-comment/keepalive lines (banner + ``logresp``, like a real Tier-2
server), then streams :data:`CANNED_LINES` in TNC2 format, CR/LF terminated, one per
``interval_s``, with a ``#`` keepalive between loops so the adapter's stall timer is
exercised against real keepalives.

The roster is built to demonstrate the M3 exit criterion end to end:

- ``N0CALL`` reuses the identity the LOCAL APRS fake emits
  (:mod:`aether.adapters.aprs_fake_feeder`), so a combined demo fuses the local-RF
  and APRS-IS observations into ONE ``aprs:station:N0CALL`` track with both
  provenance paths.
- A second ``N0CALL`` line is the *same* packet re-relayed via a different igate
  q-construct — the adapter's :class:`~aether.adapters.aprs_is.DuplicateFilter`
  must drop it (PRD §18.4).
- ``W9NET`` is APRS-IS-only (no local counterpart), so it stays a network-only
  track — the un-fused case alongside.

Run the combined no-hardware APRS fusion demo (broker first)::

    python -m aether.adapters.aprs_fake_feeder 127.0.0.1 8001 &
    python -m aether.adapters.aprs_is_fake_feeder 127.0.0.1 14580 &
    AETHER_DEMO_SOURCE=0 \
        AETHER_LOCAL_APRS=1 AETHER_LOCAL_APRS_PORT=8001 \
        AETHER_APRS_IS=1 AETHER_APRS_IS_HOST=127.0.0.1 AETHER_APRS_IS_PORT=14580 \
        AETHER_APRS_IS_CALLSIGN=N0CALL \
        uvicorn aether.backend.main:app --app-dir src

This generates data in-process/over loopback only; it never transmits, never touches
a radio, and never reaches the real APRS-IS network.
"""

import asyncio
import sys

#: Representative APRS-IS TNC2 lines, as a real server delivers them (with the
#: ``TCPIP*``/``qAC``/``qAR`` q-constructs APRS-IS injects into the path). See the
#: module docstring for why these specific callsigns.
CANNED_LINES: list[str] = [
    # Same station the LOCAL APRS fake emits → fuses into one aprs:station:N0CALL.
    "N0CALL>APU25N,TCPIP*,qAC,T2TEST:!4903.50N/07201.75W>Internet copy",
    # The exact same packet re-relayed via a different igate path: the adapter's
    # DuplicateFilter must drop it (same SRC>DEST:info, different q-construct).
    "N0CALL>APU25N,TCPIP*,qAR,IGATE2:!4903.50N/07201.75W>Internet copy",
    # An APRS-IS-only station with no local counterpart: stays a network-only track.
    "W9NET>APRS,TCPIP*,qAC,T2TEST:!4012.00N/08530.00W#Internet-only station",
    # A status frame from the network-only station (no geometry).
    "W9NET>APRS,TCPIP*,qAC,T2TEST:>APRS-IS test station",
]


async def serve_aprs_is(
    host: str,
    port: int,
    *,
    lines: list[str] | None = None,
    interval_s: float = 0.5,
    loop_forever: bool = True,
    banner: bool = True,
) -> asyncio.Server:
    """Start a fake APRS-IS server that streams ``lines`` to each connecting client.

    Returns the started :class:`asyncio.Server`; the caller awaits
    ``server.serve_forever()`` (the ``__main__`` path) or keeps it for a test's
    lifetime. Each client first has its login line read and discarded (we never act
    on what aether sends — aether is receive-only), then optionally receives a
    server banner + ``# logresp`` line, then the canned lines one per ``interval_s``,
    looping while ``loop_forever`` with a ``#`` keepalive between loops.
    """
    payload = list(lines) if lines is not None else list(CANNED_LINES)

    async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            # Read and ignore the client's login line. A real server would parse it
            # for the filter; the fake just proves aether sent one and never expects
            # anything else from the client (aether only ever writes its login).
            try:
                await asyncio.wait_for(reader.readline(), 5.0)
            except (TimeoutError, OSError):
                pass
            if banner:
                writer.write(b"# aether-fake-aprsis 0.1\r\n")
                writer.write(b"# logresp N0CALL unverified, server FAKE-1\r\n")
                await writer.drain()
            while True:
                for line in payload:
                    writer.write(line.encode("ascii") + b"\r\n")
                    await writer.drain()
                    await asyncio.sleep(interval_s)
                writer.write(b"# keepalive\r\n")  # server keepalive between loops
                await writer.drain()
                if not loop_forever:
                    break
        except (ConnectionResetError, BrokenPipeError):
            pass  # client went away; nothing to clean up but the socket
        finally:
            writer.close()

    return await asyncio.start_server(_handle, host, port)


async def _serve_forever(host: str, port: int, interval_s: float) -> None:
    server = await serve_aprs_is(host, port, interval_s=interval_s)
    addrs = ", ".join(str(s.getsockname()) for s in (server.sockets or ()))
    print(f"fake APRS-IS server -> {addrs} every {interval_s}s (Ctrl-C to stop)")
    async with server:
        await server.serve_forever()


def _main(argv: list[str]) -> int:
    if len(argv) < 3:
        print("usage: python -m aether.adapters.aprs_is_fake_feeder <host> <port> [interval_s]")
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

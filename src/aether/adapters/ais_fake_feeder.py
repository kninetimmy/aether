"""No-hardware feeder: a fake AISStream WebSocket server streaming canned frames.

Stands in for the real AISStream.io secure WebSocket so the M3.5 AIS path runs with
no API key and no network (PRD §6 no-hardware gate, §34 "every source ships a
fake/replay feeder"). It is the AIS sibling of
:mod:`aether.adapters.aprs_is_fake_feeder` (fake APRS-IS server) and
:mod:`aether.adapters.network_adsb_fake_feeder` (fake ADS-B provider): real,
production-wired code selected by config, never a live call. Plain ``ws`` (the
adapter runs against it with ``AETHER_AIS_TLS=0``); the real endpoint is ``wss``.

On each connection it reads and *ignores* the client's subscription message (aether
only ever sends that one message — receive-only), then streams :data:`CANNED_FRAMES`
as JSON text frames, one per ``interval_s``, looping while ``loop_forever``.

The roster is built to demonstrate AIS behaviour end to end:

- ``111111111`` sends a ``ShipStaticData`` (name/type/voyage) followed by a
  ``PositionReport``: the adapter's :class:`~aether.adapters.ais.VesselMerger` must
  fold the static fields into the position so the vessel is ONE track labeled
  ``DEMO CARGO`` carrying its type/destination alongside the live fix (PRD §18.5).
- ``222222222`` is position-only (no static), so it stays labeled by MMSI — the
  un-named case alongside.

Both vessels are network-only (``locally_received=False``) — AIS has no local-RF
leg. Frames omit ``time_utc`` so each observation is stamped at receipt (always
fresh); duplicate-relay collapsing is exercised in the unit tests, where broadcast
timestamps can be controlled deterministically.

Run the no-hardware AIS demo (broker first)::

    python -m aether.adapters.ais_fake_feeder 127.0.0.1 8765 &
    AETHER_DEMO_SOURCE=0 \
        AETHER_AIS=1 AETHER_AIS_TLS=0 AETHER_AIS_HOST=127.0.0.1 AETHER_AIS_PORT=8765 \
        AETHER_AIS_API_KEY=demo AETHER_AIS_LAT=38.5 AETHER_AIS_LON=-74.5 \
        uvicorn aether.backend.main:app --app-dir src

This generates data in-process/over loopback only; it never transmits, never touches
a radio, and never reaches the real AISStream service.
"""

import asyncio
import json
import sys
from typing import Any

import websockets
from websockets.exceptions import ConnectionClosed

#: Representative AISStream envelopes (``MessageType`` / ``MetaData`` / ``Message``),
#: as the real service delivers them. See the module docstring for the roster intent.
_FRAMES: list[dict[str, Any]] = [
    {
        "MessageType": "ShipStaticData",
        "MetaData": {
            "MMSI": 111111111,
            "ShipName": "DEMO CARGO",
            "latitude": 38.5,
            "longitude": -74.5,
        },
        "Message": {
            "ShipStaticData": {
                "UserID": 111111111,
                "Name": "DEMO CARGO",
                "CallSign": "DEMO1",
                "ImoNumber": 1000001,
                "Type": 70,  # cargo
                "Destination": "PORT DEMO",
                "MaximumStaticDraught": 5.5,
                "Dimension": {"A": 100, "B": 20, "C": 10, "D": 10},
            }
        },
    },
    {
        "MessageType": "PositionReport",
        "MetaData": {
            "MMSI": 111111111,
            "ShipName": "DEMO CARGO",
            "latitude": 38.5,
            "longitude": -74.5,
        },
        "Message": {
            "PositionReport": {
                "UserID": 111111111,
                "Latitude": 38.5,
                "Longitude": -74.5,
                "Sog": 12.5,
                "Cog": 270.0,
                "TrueHeading": 271,
                "NavigationalStatus": 0,  # under way using engine
            }
        },
    },
    {
        "MessageType": "PositionReport",
        "MetaData": {"MMSI": 222222222, "ShipName": "", "latitude": 39.0, "longitude": -74.0},
        "Message": {
            "PositionReport": {
                "UserID": 222222222,
                "Latitude": 39.0,
                "Longitude": -74.0,
                "Sog": 0.0,
                "Cog": 360.0,  # not available
                "TrueHeading": 511,  # not available
                "NavigationalStatus": 5,  # moored
            }
        },
    },
]

#: The roster as JSON text frames (what the WebSocket sends).
CANNED_FRAMES: list[str] = [json.dumps(frame) for frame in _FRAMES]


async def serve_ais(
    host: str,
    port: int,
    *,
    frames: list[str] | None = None,
    interval_s: float = 0.5,
    loop_forever: bool = True,
) -> Any:
    """Start a fake AISStream WebSocket server that streams ``frames`` to each client.

    Returns the started ``websockets`` server; the caller awaits ``serve_forever()``
    (the ``__main__`` path) or keeps it for a test's lifetime. Each client first has
    its subscription message read and discarded (aether only ever sends that one
    message — receive-only), then receives the canned frames one per ``interval_s``,
    looping while ``loop_forever``.
    """
    payload = list(frames) if frames is not None else list(CANNED_FRAMES)

    async def _handle(ws: Any) -> None:
        try:
            # Read and ignore the client's subscription (its bounding box/API key). A
            # real server filters on it; the fake just proves aether sent one and
            # never expects anything else from the client (it only ever subscribes).
            try:
                await asyncio.wait_for(ws.recv(), 5.0)
            except (TimeoutError, OSError, ConnectionClosed):
                pass
            while True:
                for frame in payload:
                    await ws.send(frame)
                    await asyncio.sleep(interval_s)
                if not loop_forever:
                    break
        except (ConnectionClosed, OSError):
            pass  # client went away; nothing to clean up but the socket

    return await websockets.serve(_handle, host, port)


async def _serve_forever(host: str, port: int, interval_s: float) -> None:
    server = await serve_ais(host, port, interval_s=interval_s)
    addrs = ", ".join(str(s.getsockname()) for s in server.sockets)
    print(f"fake AISStream server -> {addrs} every {interval_s}s (Ctrl-C to stop)")
    await server.serve_forever()


def _main(argv: list[str]) -> int:
    if len(argv) < 3:
        print("usage: python -m aether.adapters.ais_fake_feeder <host> <port> [interval_s]")
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

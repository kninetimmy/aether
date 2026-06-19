"""FastAPI app: REST live state + sequence-numbered websocket (PRD §21–22).

In-memory only for M1. Records now flow over the MQTT bus (PRD §23): the lifespan
runs a subscriber that ingests ``aether/v2/...`` topics into the hub, which serves
``/api/state`` and streams ``/ws/v2`` (snapshot then deltas). For the no-hardware
demo the lifespan also runs the demo publisher in-process; a real deployment sets
``AETHER_DEMO_SOURCE=0`` and runs source adapters instead.

Run the no-hardware demo (broker first):
    docker compose up -d
    uvicorn aether.backend.main:app --host 127.0.0.1 --port 8000
"""

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import aiomqtt
from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from aether.adapters.ais import run_ais
from aether.adapters.aprs_is import run_aprs_is
from aether.adapters.local_adsb import run_local_adsb
from aether.adapters.local_aprs import run_local_aprs
from aether.adapters.network_adsb import run_network_adsb
from aether.backend.hub import Connection, Hub
from aether.backend.protocol import snapshot_message
from aether.backend.subscription import default_filter, parse_subscribe
from aether.bus.client import DEFAULT_RECONNECT_S, connect, run_record_subscriber
from aether.bus.demo_publisher import run_demo_publisher
from aether.config import Settings

log = logging.getLogger(__name__)

#: Grace period for source tasks to stop on shutdown before they're abandoned.
SHUTDOWN_GRACE_S = 5.0

#: How often the backend ages out stale fused tracks (PRD §15.4). Independent of
#: the bus: it surfaces LOCAL→NET handoffs and removes tracks once every
#: contributor has gone silent, even with no new ingest.
EXPIRY_INTERVAL_S = 1.0

#: Minimum spacing between accepted ``subscribe`` frames on one connection. The
#: client debounces at ~300 ms; this is the SERVER guard so subscribe-spam can't
#: starve the send loop or the shared event loop (PRD §37 failure isolation). A
#: frame arriving inside the window is parsed for validity but does not re-snapshot.
SUBSCRIBE_MIN_INTERVAL_S = 0.25


def create_app(*, settings: Settings | None = None, demo_interval_s: float = 1.0) -> FastAPI:
    cfg = settings if settings is not None else Settings.from_env()
    hub = Hub()

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        ready = asyncio.Event()
        tasks = [
            asyncio.create_task(
                run_record_subscriber(cfg, hub.publish, ready=ready, identifier="aether-backend")
            ),
            asyncio.create_task(_run_expiry(hub)),
        ]
        if cfg.demo_source:
            tasks.append(asyncio.create_task(_run_demo(cfg, ready, demo_interval_s)))
        if cfg.local_adsb:
            tasks.append(asyncio.create_task(run_local_adsb(cfg, ready)))
        if cfg.local_aprs:
            tasks.append(asyncio.create_task(run_local_aprs(cfg, ready)))
        if cfg.network_adsb:
            tasks.append(asyncio.create_task(run_network_adsb(cfg, ready)))
        if cfg.aprs_is:
            tasks.append(asyncio.create_task(run_aprs_is(cfg, ready)))
        if cfg.ais:
            tasks.append(asyncio.create_task(run_ais(cfg, ready)))
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
            # Bound the shutdown wait: aiomqtt's graceful disconnect-on-cancel can
            # intermittently hang (observed flaky on Python 3.11), and an unbounded
            # await would wedge shutdown — and any TestClient/lifespan around it —
            # forever. Abandon a straggler after a short grace; the loop is tearing
            # down regardless, so a stuck client connection is moot (PRD §37).
            _done, pending = await asyncio.wait(tasks, timeout=SHUTDOWN_GRACE_S)
            if pending:
                log.warning(
                    "%d source task(s) did not stop within %.0fs; abandoning",
                    len(pending),
                    SHUTDOWN_GRACE_S,
                )

    app = FastAPI(title="aether COP", version="0.1.0", lifespan=lifespan)

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        return {"status": "ok", "seq": hub.state.seq, "clients": hub.client_count}

    @app.get("/api/state")
    async def state() -> dict[str, Any]:
        return snapshot_message(hub.state.snapshot())

    @app.get("/api/config")
    async def config() -> dict[str, Any]:
        """Runtime config for the frontend (PRD §5: coordinates are never committed).

        Feeds the M3.6a range-from-station filter origin. A 0,0 station is reported
        as ``null`` so the client degrades the range control to a disabled no-op
        rather than centering on null island.
        """
        configured = not (cfg.station_lat == 0.0 and cfg.station_lon == 0.0)
        return {
            "station": (
                {
                    "lat": cfg.station_lat,
                    "lon": cfg.station_lon,
                    "radius_nm": cfg.station_radius_nm,
                }
                if configured
                else None
            )
        }

    @app.websocket("/ws/v2")
    async def ws_v2(websocket: WebSocket) -> None:
        await websocket.accept()
        # Register before snapshotting so no delta is lost between the two (both are
        # synchronous — the bus cannot interleave until we await). The default
        # filter is the station-scoped one (PRD §16.3a) until the client subscribes.
        conn = hub.register(default_filter(cfg))
        try:
            await websocket.send_json(hub.snapshot_for(conn))
            sender = asyncio.create_task(_ws_send(websocket, conn))
            receiver = asyncio.create_task(_ws_receive(websocket, hub, conn, cfg))
            try:
                # Whichever finishes first (disconnect / error) tears the other down.
                _done, pending = await asyncio.wait(
                    [sender, receiver], return_when=asyncio.FIRST_COMPLETED
                )
                for task in pending:
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task
            finally:
                for task in (sender, receiver):
                    task.cancel()
        except WebSocketDisconnect:
            pass
        finally:
            hub.unregister(conn)

    return app


async def _ws_send(websocket: WebSocket, conn: Connection) -> None:
    """Drain this connection's queue to the socket — the sole owner of writes."""
    while True:
        await websocket.send_json(await conn.queue.get())


async def _ws_receive(websocket: WebSocket, hub: Hub, conn: Connection, cfg: Settings) -> None:
    """Handle ``subscribe`` frames: validate, rate-limit, re-snapshot (PRD §22.2).

    Every accepted subscribe is a resync point — :meth:`Hub.resubscribe` swaps the
    filter, resets ``cseq``, and rebuilds a filtered snapshot which we hand to the
    sender via the connection queue so all socket writes stay single-owner and
    ordered. A malformed frame is logged and the PRIOR filter kept; it never raises
    out of this loop (PRD §37). A server-side min-interval guard means subscribe
    spam can validate but cannot force re-snapshot churn that starves the sender.
    """
    last_accept = -SUBSCRIBE_MIN_INTERVAL_S
    while True:
        try:
            frame = await websocket.receive_json()
        except WebSocketDisconnect:
            return
        except Exception:
            # A non-JSON / oversized text frame must not kill the connection.
            log.debug("ignoring unparseable ws frame", exc_info=True)
            continue
        new_filter = parse_subscribe(frame, cfg)
        if new_filter is None:
            continue  # malformed → keep the prior filter (already logged)
        loop_now = asyncio.get_event_loop().time()
        if loop_now - last_accept < SUBSCRIBE_MIN_INTERVAL_S:
            # Valid but too soon: keep the filter (so the latest intent wins) but
            # skip the expensive re-snapshot to protect the send loop.
            conn.filter = new_filter
            continue
        last_accept = loop_now
        # Route through the hub's drop-oldest enqueue so a momentarily-full queue
        # can't raise out of the receive loop (PRD §37). The fresh snapshot is the
        # resync baseline regardless of which older frames get dropped.
        hub.enqueue(conn, hub.resubscribe(conn, new_filter))


async def _run_expiry(hub: Hub) -> None:
    """Periodically age out stale fused tracks until cancelled.

    Exception-isolated per tick: a transient fusion/expiry error logs and the loop
    continues rather than wedging the backend (PRD §37 failure isolation).
    """
    while True:
        await asyncio.sleep(EXPIRY_INTERVAL_S)
        try:
            hub.expire(datetime.now(UTC))
        except Exception:  # one bad sweep must not kill the expiry loop
            log.warning("track expiry sweep failed; continuing", exc_info=True)


async def _run_demo(cfg: Settings, ready: asyncio.Event, interval_s: float) -> None:
    """Publish the demo stream once the subscriber is live (avoids a startup race).

    Reconnects on broker errors with the same backoff as the subscriber, so a
    broker blip degrades the demo feed rather than crashing the lifespan.
    """
    await ready.wait()
    while True:
        try:
            async with connect(cfg, identifier="aether-demo-publisher") as bus:
                await run_demo_publisher(bus, interval_s=interval_s)
        except aiomqtt.MqttError:
            log.warning("demo publisher lost broker; reconnecting in %.0fs", DEFAULT_RECONNECT_S)
            await asyncio.sleep(DEFAULT_RECONNECT_S)


app = create_app()

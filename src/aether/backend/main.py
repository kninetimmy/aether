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

from aether.adapters.local_adsb import run_local_adsb
from aether.adapters.local_aprs import run_local_aprs
from aether.backend.hub import Hub
from aether.backend.protocol import snapshot_message
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

    @app.websocket("/ws/v2")
    async def ws_v2(websocket: WebSocket) -> None:
        await websocket.accept()
        # Register before snapshotting so no delta is lost between the two
        # (both are synchronous — the bus cannot interleave until we await).
        queue = hub.register()
        try:
            await websocket.send_json(snapshot_message(hub.state.snapshot()))
            while True:
                await websocket.send_json(await queue.get())
        except WebSocketDisconnect:
            pass
        finally:
            hub.unregister(queue)

    return app


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

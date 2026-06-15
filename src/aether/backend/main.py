"""FastAPI app: REST live state + sequence-numbered websocket (PRD §21–22).

In-memory only for M1 — the demo source feeds simulated records into the hub,
which serves ``/api/state`` and streams ``/ws/v2`` (snapshot then deltas). The
``create_app`` factory takes the demo interval so tests can run it fast.

Run the no-hardware demo with:
    uvicorn aether.backend.main:app --host 127.0.0.1 --port 8000
"""

import asyncio
import contextlib
from collections.abc import AsyncIterator
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from aether.backend.demo import run_demo_source
from aether.backend.hub import Hub
from aether.backend.protocol import snapshot_message


def create_app(*, demo_interval_s: float = 1.0) -> FastAPI:
    hub = Hub()

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        task = asyncio.create_task(run_demo_source(hub, interval_s=demo_interval_s))
        try:
            yield
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

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
        # (both are synchronous — the demo cannot interleave until we await).
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


app = create_app()

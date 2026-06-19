"""Lifespan runner that wires the persistence writer to the bus (PRD §19.2).

Runs an independent bus subscriber (its own client identity) so persistence is a
*sibling* consumer of live state, never a dependency of it: if the writer or disk
stalls, only this subscriber's queue backs up — the hub keeps serving snapshots
and deltas (PRD §5). Mirrors the source-adapter ``run_*`` lifecycle so the backend
lifespan starts it behind the ``AETHER_PERSIST`` toggle exactly like an adapter.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from aether.bus.client import run_record_subscriber
from aether.config import Settings
from aether.persist.database import Database
from aether.persist.writer import PersistenceWriter

log = logging.getLogger(__name__)

#: MQTT client identity for the persistence subscriber — distinct from the hub's
#: ``aether-backend`` so the two are independent broker sessions (PRD §5 isolation).
PERSIST_CLIENT_ID = "aether-persist"


async def run_persistence(settings: Settings, ready: asyncio.Event | None = None) -> None:
    """Open the DB, start the drain task, and persist bus records until cancelled.

    ``ready`` (if given) is set once the subscriber is live, for callers that need
    to publish without racing the subscribe (tests). On shutdown the drain task is
    cancelled and the connection closed.
    """
    database = Database(settings.db_path)
    await asyncio.to_thread(database.open)
    log.info("persistence open at %s", settings.db_path)
    writer = PersistenceWriter(database, queue_max=settings.persist_queue_max)
    drain = asyncio.create_task(writer.run())
    try:
        await run_record_subscriber(
            settings, writer.enqueue, ready=ready, identifier=PERSIST_CLIENT_ID
        )
    finally:
        drain.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await drain
        await asyncio.to_thread(database.close)

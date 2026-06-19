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
from aether.persist.retention import run_retention
from aether.persist.sampling import SampleGate
from aether.persist.writer import PersistenceWriter

log = logging.getLogger(__name__)

#: MQTT client identity for the persistence subscriber — distinct from the hub's
#: ``aether-backend`` so the two are independent broker sessions (PRD §5 isolation).
PERSIST_CLIENT_ID = "aether-persist"


def build_sample_gate(settings: Settings) -> SampleGate | None:
    """Build the per-source cadence gate from settings, or ``None`` if disabled.

    Maps the per-source cadence knobs (PRD §19.5) to source names the adapters
    actually stamp; both APRS feeds share one cadence. ``None`` (sampling off)
    makes the writer persist every record — the M4.1 full-fidelity behavior.
    """
    if not settings.persist_sample:
        return None
    cadence = {
        "local_adsb": settings.persist_sample_local_adsb_s,
        "network_adsb": settings.persist_sample_network_adsb_s,
        "ais": settings.persist_sample_ais_s,
        "local_aprs": settings.persist_sample_aprs_s,
        "aprs_is": settings.persist_sample_aprs_s,
    }
    return SampleGate(cadence, default_s=settings.persist_sample_default_s)


async def run_persistence(settings: Settings, ready: asyncio.Event | None = None) -> None:
    """Open the DB, start the drain task, and persist bus records until cancelled.

    ``ready`` (if given) is set once the subscriber is live, for callers that need
    to publish without racing the subscribe (tests). On shutdown the drain and
    retention tasks are cancelled and the connection closed.

    The retention manager (PRD §19.4) runs as a sibling task started *after* the
    schema is migrated here, so its own connection can open with migrations off and
    never races this opener. Both are siblings of live state (PRD §5): a slow disk
    or a VACUUM only backs up this writer's queue, never the hub.
    """
    database = Database(settings.db_path)
    await asyncio.to_thread(database.open)
    log.info("persistence open at %s", settings.db_path)
    writer = PersistenceWriter(
        database,
        queue_max=settings.persist_queue_max,
        sample_gate=build_sample_gate(settings),
    )
    drain = asyncio.create_task(writer.run())
    retention = asyncio.create_task(run_retention(settings))
    try:
        await run_record_subscriber(
            settings, writer.enqueue, ready=ready, identifier=PERSIST_CLIENT_ID
        )
    finally:
        for task in (drain, retention):
            task.cancel()
        for task in (drain, retention):
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await asyncio.to_thread(database.close)

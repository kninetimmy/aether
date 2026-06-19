"""End-to-end over the real bus: publish -> MQTT -> persistence subscriber -> SQLite.

Proves the M4 persistence path (PRD §31.3 flow #1, §19) with the writer running as
an independent bus consumer — no hub in this test, so it also shows persistence is a
*sibling* of live state, not wired through it (PRD §5). Skips when no broker is
reachable (see conftest); CI starts Mosquitto so it runs there.
"""

import asyncio
import contextlib
import dataclasses
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from aether.bus.client import connect
from aether.config import Settings
from aether.persist.runner import run_persistence
from aether.schema.geometry import Point
from aether.schema.provenance import Provenance
from aether.schema.records import TrackRecord

T0 = datetime(2026, 6, 19, 12, 0, 0, tzinfo=UTC)


def _track() -> TrackRecord:
    return TrackRecord(
        id="local_adsb:abc",
        source="local_adsb",
        observed_at=T0,
        received_at=T0,
        published_at=T0,
        correlation_key="aircraft:icao:abc",
        track_type="aircraft",
        geometry=Point(coordinates=[-95.0, 40.0, 3000.0]),
        altitude_m=3000.0,
        locally_received=True,
        provenance=[Provenance(source="local_adsb", observed_at=T0, received_at=T0, local_rf=True)],
    )


def _count(db_path: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        return int(conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0])
    finally:
        conn.close()


async def _publish_and_wait(settings: Settings, db_path: str) -> int:
    ready = asyncio.Event()
    task = asyncio.create_task(run_persistence(settings, ready))
    try:
        await asyncio.wait_for(ready.wait(), timeout=10.0)
        async with connect(settings, identifier="test-persist-pub") as bus:
            await bus.publish_record(_track())
        for _ in range(100):  # up to ~5s for the record to round-trip and flush
            if (count := await asyncio.to_thread(_count, db_path)) >= 1:
                return count
            await asyncio.sleep(0.05)
        return await asyncio.to_thread(_count, db_path)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


def test_record_published_to_bus_is_persisted(broker_settings: Settings, tmp_path: Path) -> None:
    db_path = str(tmp_path / "integration.db")
    settings = dataclasses.replace(broker_settings, persist=True, db_path=db_path)
    assert asyncio.run(_publish_and_wait(settings, db_path)) == 1

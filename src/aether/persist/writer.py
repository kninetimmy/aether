"""Persistence writer: bus records → bounded async queue → SQLite (PRD §19.2).

A parallel bus subscriber (see :func:`aether.persist.runner.run_persistence`)
feeds records into :meth:`PersistenceWriter.enqueue`, a cheap synchronous sink
that never blocks ingestion: a full queue drops the record and counts it rather
than back-pressuring the bus. A single drain task batches the queue and writes it
off-thread, so a slow or failing disk can only ever back up this writer's own
queue — never the hub, the websocket, or another adapter (PRD §5, §37).

Only ``track`` records are persisted in this slice (track history, PRD §19.3);
events/alerts/geofences land with the alert engine. The backend stays generic —
this is record-*kind* routing, not per-source branching.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime

from aether.persist.database import Database, ObservationRow
from aether.persist.sampling import SampleGate
from aether.schema.records import Record, TrackRecord
from aether.schema.validation import dump_record_json

log = logging.getLogger(__name__)

#: Default bounded-queue depth (PRD §19.2). Sized so a brief disk stall is absorbed
#: rather than dropping records, while still bounding memory under sustained backlog.
DEFAULT_QUEUE_MAX = 10_000
#: Max rows coalesced into one transaction by the drain loop (batch inserts, §19.2).
DEFAULT_BATCH_MAX = 256


def to_observation_row(record: Record, *, now: datetime) -> ObservationRow | None:
    """Map a record to its storage row, or ``None`` if this slice doesn't persist it.

    Only :class:`TrackRecord` is persisted for track history (PRD §19.3). Geometry
    is flattened to indexable ``lon``/``lat``/``alt_m`` columns; the full record is
    kept verbatim in ``payload`` so replay can reconstruct it losslessly.
    """
    if not isinstance(record, TrackRecord):
        return None
    lon: float | None = None
    lat: float | None = None
    alt: float | None = None
    if record.geometry is not None:
        coords = record.geometry.coordinates
        lon, lat = coords[0], coords[1]
        alt = coords[2] if len(coords) > 2 else None
    return ObservationRow(
        record_id=record.id,
        correlation_key=record.correlation_key,
        kind=record.kind,
        track_type=record.track_type,
        source=record.source,
        lon=lon,
        lat=lat,
        alt_m=alt,
        observed_at=record.observed_at.isoformat(),
        received_at=record.received_at.isoformat(),
        persisted_at=now.isoformat(),
        payload=dump_record_json(record).decode(),
    )


class PersistenceWriter:
    """Bounded queue + single drain loop that persists track observations."""

    def __init__(
        self,
        database: Database,
        *,
        queue_max: int = DEFAULT_QUEUE_MAX,
        batch_max: int = DEFAULT_BATCH_MAX,
        sample_gate: SampleGate | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._database = database
        self._batch_max = batch_max
        self._sample_gate = sample_gate
        self._now = now or (lambda: datetime.now(UTC))
        self._queue: asyncio.Queue[ObservationRow] = asyncio.Queue(maxsize=queue_max)
        #: Count of records dropped because the queue was full (visible to health).
        self.dropped = 0
        #: Count of records thinned by the cadence sampling gate (PRD §19.5).
        #: Distinct from :attr:`dropped`: this is intentional rate-limiting, not
        #: disk back-pressure loss.
        self.sampled = 0

    def enqueue(self, record: Record) -> None:
        """Synchronous, non-blocking sink for the bus subscriber (PRD §37).

        Applies the per-source cadence gate (PRD §19.5), then converts and
        enqueues the record; a full queue drops it and bumps :attr:`dropped`
        rather than awaiting, so ingestion is never back-pressured by the disk.
        """
        if not isinstance(record, TrackRecord):
            return  # this slice persists only tracks (see ``to_observation_row``)
        now = self._now()
        if self._sample_gate is not None and not self._sample_gate.admit(
            identity=record.correlation_key or record.id,
            source=record.source,
            now=now,
            high_fidelity="emergency" in record.tags,
        ):
            self.sampled += 1
            return
        row = to_observation_row(record, now=now)
        if row is None:  # unreachable for a TrackRecord; keeps the mapper authoritative
            return
        try:
            self._queue.put_nowait(row)
        except asyncio.QueueFull:
            self.dropped += 1
            if self.dropped == 1 or self.dropped % 1000 == 0:
                log.warning("persistence queue full; dropped %d record(s)", self.dropped)

    async def run(self) -> None:
        """Drain the queue forever, batching writes off-thread (PRD §19.2).

        A failed flush is logged and its batch dropped — the loop survives so a
        transient disk error can't end persistence (PRD §37). Cancellation
        propagates out for clean lifespan shutdown.
        """
        while True:
            batch = [await self._queue.get()]
            while len(batch) < self._batch_max:
                try:
                    batch.append(self._queue.get_nowait())
                except asyncio.QueueEmpty:
                    break
            try:
                await asyncio.to_thread(self._database.insert_observations, batch)
            except Exception:
                log.exception("persistence flush failed; dropping %d row(s)", len(batch))
            finally:
                for _ in batch:
                    self._queue.task_done()

    async def wait_drained(self) -> None:
        """Block until every queued row has been processed (test/shutdown helper)."""
        await self._queue.join()

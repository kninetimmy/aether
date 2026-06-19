"""Retention manager: enforce the 30-day window and disk limits (PRD §19.4).

Runs as a *sibling* of the persistence writer (only when ``persist`` is on) on its
own SQLite connection, so a sweep — including a VACUUM that briefly takes the write
lock — can never gate serving live state (PRD §5). The writer drops a batch rather
than blocking on contention (PRD §37), so the two coexist safely on one WAL store.

Each sweep:

1. Always enforces the time-retention window (delete observations older than
   ``retention_days``).
2. Under storage pressure, walks the PRD §19.4 ladder — downsample old high-rate
   observations, delete oldest ordinary observations, shorten effective retention —
   re-measuring with a VACUUM between escalations until back under the water marks
   or the attempt budget is spent, then emits a health warning + system event.

The ladder's earlier steps (delete expired raw/diagnostic payloads; delete
low-value source-health samples) name tables that arrive with later M4/M5 consumers
(``source_status_samples``, etc.); they slot in here as those tables land, rather
than being created empty ahead of a consumer (persistence-with-its-consumer
decision). Today the lever is the ``observations`` table.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from math import ceil
from typing import Literal

from aether.bus.client import connect
from aether.config import Settings
from aether.persist.database import Database
from aether.schema.records import EventRecord, Record, SourceStatusRecord

log = logging.getLogger(__name__)

#: MQTT client identity for the retention manager's best-effort health/event
#: publishes — distinct from the writer's subscriber so they're independent
#: broker sessions (PRD §5 isolation).
RETENTION_CLIENT_ID = "aether-retention"
#: ``source`` on the records the manager emits under pressure (its own identity in
#: the source-status panel, alongside the adapters').
RETENTION_SOURCE = "aether-persist"

_GIB = 1024**3
#: Rows per delete statement — bounds how long the write lock is held so writer
#: contention stays brief between batches (PRD §37).
_DELETE_BATCH = 5000
#: Observations older than this are eligible for downsampling; recent data keeps
#: full fidelity (PRD §19.4 step 2 "old high-rate" observations).
_DOWNSAMPLE_AFTER_DAYS = 1
#: Target spacing kept per identity when downsampling old observations.
_DOWNSAMPLE_GAP_S = 30.0
#: Effective retention is never shortened below this floor (PRD §19.4 step 5).
_MIN_RETENTION_DAYS = 1
#: Cap on VACUUM-and-re-measure escalations in one sweep, so an impossible budget
#: (or pressure from *other* files on the disk) can't spin the sweep forever.
_MAX_LADDER_ATTEMPTS = 4

Pressure = Literal["none", "high", "critical"]

#: Async sink for the records the manager emits under pressure (a bus publish in
#: production; a collector in tests). ``None`` disables emission.
EmitFn = Callable[[Record], Awaitable[None]]


@dataclass(frozen=True)
class SweepResult:
    """What one :meth:`RetentionManager.sweep` did — returned for logging/tests."""

    pressure: Pressure
    expired_deleted: int
    downsampled: int
    oldest_deleted: int
    effective_retention_days: int
    vacuumed: bool
    db_bytes_before: int
    db_bytes_after: int
    rows_before: int
    rows_after: int


class RetentionManager:
    """Applies the PRD §19.4 retention policy to one persistence store."""

    def __init__(
        self,
        database: Database,
        settings: Settings,
        *,
        emit: EmitFn | None = None,
        now: Callable[[], datetime] | None = None,
        free_disk_bytes: Callable[[], int] | None = None,
    ) -> None:
        self._db = database
        self._settings = settings
        self._emit = emit
        self._now = now or (lambda: datetime.now(UTC))
        self._free_disk_bytes = free_disk_bytes or self._default_free_disk_bytes

    def _default_free_disk_bytes(self) -> int:
        # Free space on the filesystem holding the store (blocking — called via
        # ``to_thread``). Empty dirname (relative path) means the current directory.
        return shutil.disk_usage(os.path.dirname(self._settings.db_path) or ".").free

    def _assess(self, db_bytes: int, free_bytes: int) -> Pressure:
        """Classify storage pressure from store size and free disk (PRD §19.4 marks)."""
        s = self._settings
        max_bytes = s.db_max_gb * _GIB
        min_free = s.min_free_disk_gb * _GIB
        # A free-disk floor breach is always critical: aether must shed what it can
        # even when the size budget itself is fine (or unset).
        if min_free > 0 and free_bytes <= min_free:
            return "critical"
        if max_bytes > 0:
            if db_bytes >= max_bytes * s.db_critical_water:
                return "critical"
            if db_bytes >= max_bytes * s.db_high_water:
                return "high"
        return "none"

    def _cutoff(self, now: datetime, days: float) -> str:
        return (now - timedelta(days=days)).isoformat()

    async def _delete_older_than(self, cutoff_iso: str) -> int:
        """Delete every observation older than ``cutoff``, in lock-releasing batches."""
        total = 0
        while True:
            n = await asyncio.to_thread(
                self._db.delete_observations_older_than, cutoff_iso, limit=_DELETE_BATCH
            )
            total += n
            if n < _DELETE_BATCH:
                return total

    async def _delete_oldest(self, count: int) -> int:
        """Delete the ``count`` oldest observations, in lock-releasing batches."""
        total = 0
        while total < count:
            want = min(_DELETE_BATCH, count - total)
            deleted = await asyncio.to_thread(self._db.delete_oldest_observations, want)
            total += deleted
            if deleted < want:
                break  # ran out of rows
        return total

    async def sweep(self) -> SweepResult:
        """Run one retention pass; never raises out for one bad measurement is the
        caller's loop's job (see :func:`run_retention`). Returns what it did."""
        now = self._now()
        rows_before = await asyncio.to_thread(self._db.count_observations)
        size_before = await asyncio.to_thread(self._db.file_size_bytes)
        free_before = await asyncio.to_thread(self._free_disk_bytes)

        # Step 0 (always): enforce the time-retention window.
        expired = await self._delete_older_than(self._cutoff(now, self._settings.retention_days))

        # Pressure is judged on the pre-sweep size: DELETE doesn't shrink the file,
        # only the VACUUM below does, so size_before is the honest trigger.
        pressure = self._assess(size_before, free_before)
        downsampled = 0
        oldest = 0
        eff_days = self._settings.retention_days
        vacuumed = False

        if pressure != "none":
            # Step 2: thin old high-rate observations (recent data untouched).
            downsampled = await asyncio.to_thread(
                self._db.downsample_observations_older_than,
                self._cutoff(now, _DOWNSAMPLE_AFTER_DAYS),
                _DOWNSAMPLE_GAP_S,
            )
            # Steps 4-5: reclaim with VACUUM feedback, escalating until back under
            # the marks or the attempt budget is spent.
            for _ in range(_MAX_LADDER_ATTEMPTS):
                await asyncio.to_thread(self._db.vacuum)
                vacuumed = True
                size = await asyncio.to_thread(self._db.file_size_bytes)
                free = await asyncio.to_thread(self._free_disk_bytes)
                if self._assess(size, free) == "none":
                    break
                rows = await asyncio.to_thread(self._db.count_observations)
                if rows == 0:
                    break  # nothing left to shed — pressure is structural/external
                max_bytes = self._settings.db_max_gb * _GIB
                if max_bytes > 0 and size > max_bytes * self._settings.db_high_water:
                    # Step 4: delete oldest ordinary observations toward the budget.
                    target = max_bytes * self._settings.db_high_water
                    per_row = size / rows
                    want = max(_DELETE_BATCH, ceil((size - target) / per_row))
                    oldest += await self._delete_oldest(min(rows, want))
                elif eff_days > _MIN_RETENTION_DAYS:
                    # Step 5: shorten effective retention (low disk / no size cap).
                    eff_days = max(_MIN_RETENTION_DAYS, eff_days // 2)
                    expired += await self._delete_older_than(self._cutoff(now, eff_days))
                else:
                    # At the floor but still pressured: shed the oldest as a last resort.
                    oldest += await self._delete_oldest(_DELETE_BATCH)
            else:
                log.warning(
                    "retention: still under %s pressure after %d attempts",
                    pressure,
                    _MAX_LADDER_ATTEMPTS,
                )

        # Bound the WAL sidecar every sweep, then measure the honest final size.
        await asyncio.to_thread(self._db.checkpoint_truncate)
        size_after = await asyncio.to_thread(self._db.file_size_bytes)
        rows_after = await asyncio.to_thread(self._db.count_observations)

        result = SweepResult(
            pressure=pressure,
            expired_deleted=expired,
            downsampled=downsampled,
            oldest_deleted=oldest,
            effective_retention_days=eff_days,
            vacuumed=vacuumed,
            db_bytes_before=size_before,
            db_bytes_after=size_after,
            rows_before=rows_before,
            rows_after=rows_after,
        )
        if pressure != "none":
            log.warning("retention sweep under pressure: %s", self._summary(result))
            await self._emit_pressure(now, result)
        return result

    def _summary(self, r: SweepResult) -> str:
        return (
            f"{r.pressure} storage pressure: store "
            f"{r.db_bytes_before / _GIB:.3f}->{r.db_bytes_after / _GIB:.3f} GiB; "
            f"deleted {r.expired_deleted} expired, downsampled {r.downsampled}, "
            f"removed {r.oldest_deleted} oldest; effective retention "
            f"{r.effective_retention_days}d of {self._settings.retention_days}d"
        )

    async def _emit_pressure(self, now: datetime, result: SweepResult) -> None:
        if self._emit is None:
            return
        for record in self._pressure_records(now, result):
            await self._emit(record)

    def _pressure_records(self, now: datetime, result: SweepResult) -> list[Record]:
        """The health warning + system event for PRD §19.4 step 6.

        A ``source_status`` (degraded) is the health warning the source panel shows;
        an ``event`` is the system alert/notification. The stateful ``alert`` record
        (open/ack/resolve lifecycle) is the alert engine's domain and arrives with
        it — emitting one here would presume that not-yet-built machinery.
        """
        summary = self._summary(result)
        severity = "critical" if result.pressure == "critical" else "warning"
        status = SourceStatusRecord(
            id=f"source_status:{RETENTION_SOURCE}",
            source=RETENTION_SOURCE,
            observed_at=now,
            received_at=now,
            published_at=now,
            status="degraded",
            error_code="storage_pressure",
            error_summary=summary,
        )
        event = EventRecord(
            id=f"event:retention:{now.isoformat()}",
            source=RETENTION_SOURCE,
            observed_at=now,
            received_at=now,
            published_at=now,
            event_type="storage_pressure",
            severity=severity,
            summary="Persistence store under storage pressure",
            message=summary,
        )
        return [status, event]


async def run_retention(settings: Settings, ready: asyncio.Event | None = None) -> None:
    """Open a sibling connection and run the retention sweep on an interval.

    Opens its OWN connection with ``run_migrations=False`` — :func:`run_persistence`
    creates the schema first, so this opener only needs to attach. Each sweep is
    exception-isolated (one bad pass logs and the loop continues, PRD §37); pressure
    records are published best-effort, so a downed broker degrades signalling but
    never stops the actual reclaim.
    """
    database = Database(settings.db_path)
    await asyncio.to_thread(database.open, run_migrations=False)
    log.info("retention manager active for %s", settings.db_path)

    async def emit(record: Record) -> None:
        try:
            async with connect(settings, identifier=RETENTION_CLIENT_ID) as bus:
                await bus.publish_record(record)
        except Exception:  # broker down → log and keep going; the reclaim still ran
            log.warning("retention could not publish %s; continuing", record.kind, exc_info=True)

    manager = RetentionManager(database, settings, emit=emit)
    if ready is not None:
        ready.set()
    try:
        while True:
            await asyncio.sleep(settings.retention_interval_s)
            try:
                await manager.sweep()
            except Exception:  # one bad sweep must not kill the loop
                log.warning("retention sweep failed; continuing", exc_info=True)
    finally:
        await asyncio.to_thread(database.close)

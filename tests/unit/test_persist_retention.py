"""Unit tests for the retention manager (PRD §19.4, §35 disk-limit acceptance).

Drive :class:`RetentionManager.sweep` against a real on-disk SQLite store with
injected clock/free-disk so the storage-pressure ladder is deterministic and needs
no broker. The size-pressure test doubles as the §35 criterion "disk limits
override time retention safely": non-expired observations are shed to honor the
size budget.
"""

import asyncio
import dataclasses
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from aether.config import Settings
from aether.persist.database import Database, ObservationRow
from aether.persist.retention import (
    RETENTION_SOURCE,
    RetentionManager,
    SweepResult,
    run_retention,
)
from aether.schema.records import EventRecord, Record, SourceStatusRecord

T0 = datetime(2026, 6, 19, 12, 0, 0, tzinfo=UTC)
_GIB = 1024**3


def _rows(n: int, *, observed_from: datetime, payload_bytes: int = 800) -> list[ObservationRow]:
    """n observations of one identity, 1 s apart, with a fat payload to grow the file."""
    iso = lambda dt: dt.isoformat()  # noqa: E731
    return [
        ObservationRow(
            record_id=f"t{i}",
            correlation_key="aircraft:icao:abc",
            kind="track",
            track_type="aircraft",
            source="local_adsb",
            lon=-95.0,
            lat=40.0,
            alt_m=3000.0,
            observed_at=iso(observed_from + timedelta(seconds=i)),
            received_at=iso(observed_from + timedelta(seconds=i)),
            persisted_at=iso(T0),
            payload="x" * payload_bytes,
        )
        for i in range(n)
    ]


def _db(tmp_path: Path, rows: Sequence[ObservationRow]) -> Database:
    db = Database(str(tmp_path / "r.db"))
    db.open()
    db.insert_observations(rows)
    db.checkpoint_truncate()  # fold the WAL into the main file so sizes are stable
    return db


def _settings(tmp_path: Path, **kw: object) -> Settings:
    return dataclasses.replace(Settings(), db_path=str(tmp_path / "r.db"), persist=True, **kw)


class _Collector:
    """Async emit sink that records what the manager publishes under pressure."""

    def __init__(self) -> None:
        self.records: list[Record] = []

    async def __call__(self, record: Record) -> None:
        self.records.append(record)


# -- time retention ----------------------------------------------------------


def test_time_retention_deletes_old_keeps_recent(tmp_path: Path) -> None:
    db = _db(
        tmp_path,
        [
            *_rows(3, observed_from=T0 - timedelta(days=40)),  # expired
            *_rows(2, observed_from=T0 - timedelta(hours=1)),  # within window
        ],
    )
    try:
        mgr = RetentionManager(db, _settings(tmp_path, retention_days=30), now=lambda: T0)
        result = asyncio.run(mgr.sweep())
        assert result.pressure == "none"
        assert result.expired_deleted == 3
        assert not result.vacuumed
        assert db.count_observations() == 2
    finally:
        db.close()


def test_no_pressure_does_not_emit_or_vacuum(tmp_path: Path) -> None:
    db = _db(tmp_path, _rows(5, observed_from=T0 - timedelta(hours=1)))
    emit = _Collector()
    try:
        mgr = RetentionManager(
            db, _settings(tmp_path, retention_days=30, db_max_gb=10.0), emit=emit, now=lambda: T0
        )
        result = asyncio.run(mgr.sweep())
        assert result.pressure == "none"
        assert not result.vacuumed
        assert emit.records == []
        assert db.count_observations() == 5
    finally:
        db.close()


# -- downsample (ladder step 2) ----------------------------------------------


def test_downsample_keeps_one_per_gap(tmp_path: Path) -> None:
    # 120 observations 1 s apart, all older than the cutoff → 30 s buckets keep 4.
    db = _db(tmp_path, _rows(120, observed_from=T0 - timedelta(days=10)))
    try:
        deleted = db.downsample_observations_older_than((T0 - timedelta(days=1)).isoformat(), 30.0)
        assert deleted == 116
        assert db.count_observations() == 4
    finally:
        db.close()


# -- size pressure: ladder reclaims AND overrides time retention (PRD §35) ----


def test_size_pressure_reclaims_and_emits(tmp_path: Path) -> None:
    # All observations are RECENT (inside the 30-day window) yet the store is over
    # its size budget → the ladder must shed non-expired data and shrink the file.
    db = _db(tmp_path, _rows(2000, observed_from=T0 - timedelta(hours=2)))
    size_before = db.file_size_bytes()
    emit = _Collector()
    try:
        # Budget at 60% of the current size → critical (>= 0.95 * budget).
        settings = _settings(tmp_path, retention_days=30, db_max_gb=size_before * 0.6 / _GIB)
        mgr = RetentionManager(db, settings, emit=emit, now=lambda: T0)
        result = asyncio.run(mgr.sweep())

        assert result.pressure == "critical"
        assert result.vacuumed
        assert result.db_bytes_after < result.db_bytes_before  # VACUUM actually shrank it
        assert result.rows_after < result.rows_before  # non-expired data was shed
        assert db.file_size_bytes() <= settings.db_max_gb * _GIB  # honored the budget

        kinds = [r.kind for r in emit.records]
        assert kinds == ["source_status", "event"]
        status, event = emit.records
        assert isinstance(status, SourceStatusRecord)
        assert status.source == RETENTION_SOURCE
        assert status.status == "degraded"
        assert status.error_code == "storage_pressure"
        assert isinstance(event, EventRecord)
        assert event.event_type == "storage_pressure"
        assert event.severity == "critical"
    finally:
        db.close()


# -- free-disk pressure (injected) -------------------------------------------


def test_low_free_disk_shortens_retention_and_emits(tmp_path: Path) -> None:
    # No size cap, but the free-disk floor is breached and stays breached (pressure
    # from OTHER files). aether sheds what it honestly can — shortening effective
    # retention toward the floor — then warns; it can't reclaim others' space.
    db = _db(tmp_path, _rows(5, observed_from=T0 - timedelta(hours=1)))
    emit = _Collector()
    try:
        settings = _settings(tmp_path, retention_days=30, min_free_disk_gb=1.0)
        mgr = RetentionManager(
            db,
            settings,
            emit=emit,
            now=lambda: T0,
            free_disk_bytes=lambda: 0,  # always below the 1 GiB floor
        )
        result = asyncio.run(mgr.sweep())
        assert result.pressure == "critical"
        assert result.effective_retention_days < 30  # step 5 engaged
        assert [r.kind for r in emit.records] == ["source_status", "event"]
    finally:
        db.close()


def test_impossible_budget_caps_without_hanging(tmp_path: Path) -> None:
    # A budget smaller than an empty database can ever be: the sweep must bound its
    # work (no infinite VACUUM loop) and still complete, having shed everything.
    db = _db(tmp_path, _rows(50, observed_from=T0 - timedelta(hours=1)))
    try:
        settings = _settings(tmp_path, retention_days=30, db_max_gb=1e-9)  # ~1 byte
        mgr = RetentionManager(db, settings, now=lambda: T0)
        result = asyncio.run(asyncio.wait_for(mgr.sweep(), timeout=10.0))
        assert isinstance(result, SweepResult)
        assert result.pressure == "critical"
        assert db.count_observations() == 0  # shed everything trying to comply
    finally:
        db.close()


# -- run_retention loop isolation --------------------------------------------


def test_run_retention_loop_survives_sweep_errors(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # Create the schema first (the writer's job in production); run_retention opens
    # a sibling connection with migrations off.
    seed = Database(str(tmp_path / "r.db"))
    seed.open()
    seed.close()

    calls = 0

    async def boom(self: RetentionManager) -> SweepResult:
        nonlocal calls
        calls += 1
        raise RuntimeError("sweep blew up")

    monkeypatch.setattr(RetentionManager, "sweep", boom)
    settings = _settings(tmp_path, retention_interval_s=0.01)

    async def scenario() -> None:
        task = asyncio.create_task(run_retention(settings))
        for _ in range(100):  # wait until the loop has retried a few times
            if calls >= 3:
                break
            await asyncio.sleep(0.01)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(asyncio.wait_for(scenario(), timeout=5.0))
    assert calls >= 3  # the loop kept calling sweep despite each raising


@pytest.mark.parametrize(
    ("db_bytes", "free_bytes", "expected"),
    [
        (10, 10 * _GIB, "none"),  # tiny store, plenty free
        (int(0.9 * _GIB), 10 * _GIB, "high"),  # >= 85% of 1 GiB budget
        (int(0.99 * _GIB), 10 * _GIB, "critical"),  # >= 95% of budget
        (10, _GIB // 2, "critical"),  # free disk below the 1 GiB floor
    ],
)
def test_assess_levels(tmp_path: Path, db_bytes: int, free_bytes: int, expected: str) -> None:
    settings = _settings(tmp_path, db_max_gb=1.0, min_free_disk_gb=1.0)
    mgr = RetentionManager(Database(str(tmp_path / "r.db")), settings)
    assert mgr._assess(db_bytes, free_bytes) == expected

"""Unit tests for the persistence writer (PRD §19.2, §37 failure isolation)."""

import asyncio
import contextlib
import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from aether.persist.database import Database, ObservationRow
from aether.persist.sampling import SampleGate
from aether.persist.writer import PersistenceWriter, to_observation_row
from aether.schema.geometry import Point
from aether.schema.provenance import Provenance
from aether.schema.records import EventRecord, TrackRecord

T0 = datetime(2026, 6, 19, 12, 0, 0, tzinfo=UTC)


def _track(
    record_id: str = "local_adsb:abc",
    correlation_key: str | None = "aircraft:icao:abc",
    with_geometry: bool = True,
    tags: list[str] | None = None,
) -> TrackRecord:
    return TrackRecord(
        id=record_id,
        source="local_adsb",
        observed_at=T0,
        received_at=T0,
        published_at=T0,
        correlation_key=correlation_key,
        track_type="aircraft",
        geometry=Point(coordinates=[-95.0, 40.0, 3000.0]) if with_geometry else None,
        altitude_m=3000.0,
        locally_received=True,
        tags=tags if tags is not None else [],
        provenance=[Provenance(source="local_adsb", observed_at=T0, received_at=T0, local_rf=True)],
    )


def _event() -> EventRecord:
    return EventRecord(
        id="evt:1",
        source="local_adsb",
        observed_at=T0,
        received_at=T0,
        published_at=T0,
        event_type="emergency_squawk",
        summary="7700",
    )


async def _drive(writer: PersistenceWriter, records: Sequence[object]) -> None:
    """Run the drain loop, enqueue records, wait for the queue to empty, then stop."""
    task = asyncio.create_task(writer.run())
    for record in records:
        writer.enqueue(record)  # type: ignore[arg-type]
    await asyncio.wait_for(writer.wait_drained(), timeout=5.0)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


# -- to_observation_row (pure mapping) ---------------------------------------


def test_to_observation_row_flattens_geometry() -> None:
    row = to_observation_row(_track(), now=T0)
    assert row is not None
    assert (row.lon, row.lat, row.alt_m) == (-95.0, 40.0, 3000.0)
    assert row.correlation_key == "aircraft:icao:abc"
    assert row.track_type == "aircraft"
    assert row.persisted_at == T0.isoformat()
    assert '"kind":"track"' in row.payload


def test_to_observation_row_skips_non_track() -> None:
    assert to_observation_row(_event(), now=T0) is None


# -- PersistenceWriter over a real SQLite store ------------------------------


def test_persists_track_observation(tmp_path: Path) -> None:
    db = Database(str(tmp_path / "p.db"))
    db.open()
    try:
        asyncio.run(_drive(PersistenceWriter(db, now=lambda: T0), [_track()]))
        assert db.count_observations() == 1
        conn = sqlite3.connect(tmp_path / "p.db")
        lon, lat, alt, corr = conn.execute(
            "SELECT lon, lat, alt_m, correlation_key FROM observations"
        ).fetchone()
        assert (lon, lat, alt, corr) == (-95.0, 40.0, 3000.0, "aircraft:icao:abc")
    finally:
        db.close()


def test_skips_non_track_records(tmp_path: Path) -> None:
    db = Database(str(tmp_path / "p.db"))
    db.open()
    try:
        asyncio.run(_drive(PersistenceWriter(db, now=lambda: T0), [_event()]))
        assert db.count_observations() == 0
    finally:
        db.close()


def test_track_without_geometry_persists_null_coords(tmp_path: Path) -> None:
    db = Database(str(tmp_path / "p.db"))
    db.open()
    try:
        asyncio.run(_drive(PersistenceWriter(db, now=lambda: T0), [_track(with_geometry=False)]))
        conn = sqlite3.connect(tmp_path / "p.db")
        lon, lat, alt = conn.execute("SELECT lon, lat, alt_m FROM observations").fetchone()
        assert (lon, lat, alt) == (None, None, None)
    finally:
        db.close()


def test_full_queue_drops_without_raising(tmp_path: Path) -> None:
    db = Database(str(tmp_path / "p.db"))
    db.open()
    try:
        # No drain loop running, so a maxsize-1 queue fills immediately: every put
        # past the first is dropped and counted, never raised (PRD §37).
        writer = PersistenceWriter(db, queue_max=1, now=lambda: T0)
        for i in range(5):
            writer.enqueue(_track(record_id=f"t{i}"))
        assert writer.dropped == 4
    finally:
        db.close()


# -- sampling gate composition (PRD §19.5) -----------------------------------


def test_sampling_gate_thins_sub_cadence_duplicates(tmp_path: Path) -> None:
    db = Database(str(tmp_path / "p.db"))
    db.open()
    try:
        # Three records for one identity at the same instant; cadence 5 s admits
        # only the first, the rest are sampled out (not queue-full drops).
        writer = PersistenceWriter(db, sample_gate=SampleGate({"local_adsb": 5.0}), now=lambda: T0)
        asyncio.run(_drive(writer, [_track(record_id=f"t{i}") for i in range(3)]))
        assert db.count_observations() == 1
        assert writer.sampled == 2
        assert writer.dropped == 0
    finally:
        db.close()


def test_sampling_gate_lets_emergency_bypass(tmp_path: Path) -> None:
    db = Database(str(tmp_path / "p.db"))
    db.open()
    try:
        writer = PersistenceWriter(db, sample_gate=SampleGate({"local_adsb": 5.0}), now=lambda: T0)
        # Ordinary point, then an emergency point 0 s later: the emergency persists
        # despite the cadence (PRD §19.5 higher-fidelity-while-alert-active).
        asyncio.run(
            _drive(writer, [_track(record_id="a"), _track(record_id="b", tags=["emergency"])])
        )
        assert db.count_observations() == 2
        assert writer.sampled == 0
    finally:
        db.close()


def test_no_gate_persists_every_record(tmp_path: Path) -> None:
    db = Database(str(tmp_path / "p.db"))
    db.open()
    try:
        # sample_gate defaults to None → M4.1 full-fidelity behavior: both points
        # of one identity at one instant persist.
        writer = PersistenceWriter(db, now=lambda: T0)
        asyncio.run(_drive(writer, [_track(record_id="a"), _track(record_id="b")]))
        assert db.count_observations() == 2
        assert writer.sampled == 0
    finally:
        db.close()


class _FlakyDatabase:
    """Database double whose first ``fail_times`` flushes raise (disk-error sim)."""

    def __init__(self, fail_times: int) -> None:
        self._fail_times = fail_times
        self.calls = 0
        self.inserted: list[ObservationRow] = []

    def insert_observations(self, rows: Sequence[ObservationRow]) -> None:
        self.calls += 1
        if self.calls <= self._fail_times:
            raise sqlite3.OperationalError("disk I/O error")
        self.inserted.extend(rows)


def test_flush_failure_does_not_kill_the_loop() -> None:
    db = _FlakyDatabase(fail_times=1)
    writer = PersistenceWriter(db, now=lambda: T0)  # type: ignore[arg-type]

    async def scenario() -> None:
        task = asyncio.create_task(writer.run())
        writer.enqueue(_track(record_id="a"))
        await asyncio.wait_for(writer.wait_drained(), timeout=5.0)  # first flush raises
        writer.enqueue(_track(record_id="b"))
        await asyncio.wait_for(writer.wait_drained(), timeout=5.0)  # loop survived
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert db.calls == 2
    assert [row.record_id for row in db.inserted] == ["b"]

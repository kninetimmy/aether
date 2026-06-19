"""SQLite WAL database handle for the persistence store (PRD §19.2).

A thin wrapper around a single ``sqlite3`` connection in WAL mode with foreign
keys on. Opened once at startup (migrations run here); every write goes through
the single-owner :class:`~aether.persist.writer.PersistenceWriter` drain loop, so
the connection — created ``check_same_thread=False`` so it can be driven from
``asyncio.to_thread`` — is only ever touched by one logical writer at a time
(PRD §19.2 "single bounded async write queue").
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from typing import cast

from aether.persist.migrations import apply_migrations


@dataclass(frozen=True)
class ObservationRow:
    """One persisted track observation — the row written to ``observations``."""

    record_id: str
    correlation_key: str | None
    kind: str
    track_type: str | None
    source: str
    lon: float | None
    lat: float | None
    alt_m: float | None
    observed_at: str
    received_at: str
    persisted_at: str
    payload: str


_INSERT_OBSERVATION = (
    "INSERT INTO observations "
    "(record_id, correlation_key, kind, track_type, source, lon, lat, alt_m, "
    "observed_at, received_at, persisted_at, payload) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)

#: Column list (in :class:`ObservationRow` field order) for history reads.
_OBSERVATION_COLUMNS = (
    "record_id, correlation_key, kind, track_type, source, "
    "lon, lat, alt_m, observed_at, received_at, persisted_at, payload"
)


def read_track_history(
    path: str,
    identity: str,
    *,
    start_iso: str | None = None,
    end_iso: str | None = None,
    limit: int,
    busy_timeout_ms: int = 5000,
) -> list[ObservationRow]:
    """Return one track's persisted observations, oldest-first (read-only, blocking).

    Opens a *fresh read-only* connection (``mode=ro``) so the read path never
    touches the writer's or retention's connection (PRD §5): a WAL reader neither
    blocks nor is blocked by the single writer, and this opener can never migrate or
    mutate the store. ``identity`` is matched the way the UI identifies a track — its
    ``correlation_key`` when fused, else its ``record_id`` (the same
    ``COALESCE(correlation_key, record_id)`` identity the downsampler groups on). The
    optional ``[start_iso, end_iso)`` window is half-open; bounds must already be in
    the store's canonical UTC-ISO form so the lexical compare is chronological. The
    most recent ``limit`` observations are selected (so a capped response keeps the
    newest history) and returned in ascending ``observed_at`` order.

    Raises ``sqlite3.OperationalError`` when the store file does not exist
    (persistence off, or nothing written yet); the caller maps that to an empty
    history. Blocking — call via ``asyncio.to_thread``.
    """
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)
    try:
        conn.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
        clauses = [
            f"SELECT {_OBSERVATION_COLUMNS} FROM observations "
            "WHERE (correlation_key = ? OR (correlation_key IS NULL AND record_id = ?))"
        ]
        params: list[object] = [identity, identity]
        if start_iso is not None:
            clauses.append("AND observed_at >= ?")
            params.append(start_iso)
        if end_iso is not None:
            clauses.append("AND observed_at < ?")
            params.append(end_iso)
        clauses.append("ORDER BY observed_at DESC, id DESC LIMIT ?")
        params.append(limit)
        rows = conn.execute(" ".join(clauses), params).fetchall()
    finally:
        conn.close()
    rows.reverse()  # newest-first select → return oldest-first
    return [
        ObservationRow(
            record_id=row[0],
            correlation_key=row[1],
            kind=row[2],
            track_type=row[3],
            source=row[4],
            lon=row[5],
            lat=row[6],
            alt_m=row[7],
            observed_at=row[8],
            received_at=row[9],
            persisted_at=row[10],
            payload=row[11],
        )
        for row in rows
    ]


class Database:
    """Owns the persistence connection. All methods are blocking — call from a thread."""

    def __init__(self, path: str, *, busy_timeout_ms: int = 5000) -> None:
        self._path = path
        self._busy_timeout_ms = busy_timeout_ms
        self._conn: sqlite3.Connection | None = None

    def open(self, *, run_migrations: bool = True) -> None:
        """Open the connection, set WAL pragmas, and (by default) run migrations.

        ``run_migrations=False`` is for a *second* connection to an already-migrated
        store — the retention manager opens its own WAL connection this way so it is
        isolated from the writer's connection (PRD §5) without two openers racing to
        apply the same migration. The owner that creates the schema opens with
        migrations on; siblings open after it with them off. Blocking — call in a
        thread.
        """
        conn = sqlite3.connect(self._path, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(f"PRAGMA busy_timeout={self._busy_timeout_ms}")
        if run_migrations:
            apply_migrations(conn)
        self._conn = conn

    def close(self) -> None:
        """Close the connection if open (blocking)."""
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def _require_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("Database is not open; call open() first")
        return self._conn

    def insert_observations(self, rows: Sequence[ObservationRow]) -> None:
        """Insert a batch of observations in one transaction (blocking)."""
        if not rows:
            return
        conn = self._require_conn()
        conn.executemany(
            _INSERT_OBSERVATION,
            [
                (
                    row.record_id,
                    row.correlation_key,
                    row.kind,
                    row.track_type,
                    row.source,
                    row.lon,
                    row.lat,
                    row.alt_m,
                    row.observed_at,
                    row.received_at,
                    row.persisted_at,
                    row.payload,
                )
                for row in rows
            ],
        )
        conn.commit()

    def count_observations(self) -> int:
        """Total persisted observations — a read helper for tests/health (blocking)."""
        conn = self._require_conn()
        row = conn.execute("SELECT COUNT(*) FROM observations").fetchone()
        return cast(int, row[0])

    # -- retention (PRD §19.4) -----------------------------------------------
    # All blocking; the retention manager drives them via ``asyncio.to_thread`` on
    # its own connection so they never touch the writer's connection or the loop.

    def file_size_bytes(self) -> int:
        """On-disk size of the store: main DB plus the ``-wal``/``-shm`` sidecars.

        The size the ``db_max_gb`` budget is measured against (PRD §19.4). Missing
        sidecars (e.g. no checkpoint yet) simply count as zero.
        """
        total = 0
        for suffix in ("", "-wal", "-shm"):
            try:
                total += os.path.getsize(self._path + suffix)
            except OSError:
                pass  # not yet created / mid-checkpoint — treat as absent
        return total

    def delete_observations_older_than(self, cutoff_iso: str, *, limit: int) -> int:
        """Delete up to ``limit`` oldest observations with ``observed_at < cutoff``.

        Timestamps are stored as UTC ISO-8601 with a fixed offset, so a lexical
        ``<`` is a chronological one. Bounded by ``limit`` so a large backlog is
        purged in lock-releasing batches (writer contention stays brief, PRD §37).
        Returns the number of rows deleted.
        """
        conn = self._require_conn()
        cur = conn.execute(
            "DELETE FROM observations WHERE id IN ("
            "SELECT id FROM observations WHERE observed_at < ? ORDER BY observed_at LIMIT ?)",
            (cutoff_iso, limit),
        )
        conn.commit()
        return cur.rowcount

    def delete_oldest_observations(self, limit: int) -> int:
        """Delete the ``limit`` oldest observations regardless of age (ladder step 4).

        The last-resort lever when the store is over its size budget but data is
        still inside the retention window: drop the oldest ordinary observations
        first (PRD §19.4 — before alerts or major events, which live elsewhere).
        Returns the number of rows deleted.
        """
        conn = self._require_conn()
        cur = conn.execute(
            "DELETE FROM observations WHERE id IN ("
            "SELECT id FROM observations ORDER BY observed_at LIMIT ?)",
            (limit,),
        )
        conn.commit()
        return cur.rowcount

    def downsample_observations_older_than(self, cutoff_iso: str, gap_s: float) -> int:
        """Thin old high-rate observations to ~one per identity per ``gap_s`` (step 2).

        For rows older than ``cutoff`` keeps the earliest observation in each
        ``gap_s`` time bucket per fused identity (``correlation_key``, falling back
        to ``record_id`` when unfused) and deletes the rest — recent data keeps full
        fidelity. Returns the number of rows deleted.
        """
        conn = self._require_conn()
        cur = conn.execute(
            "DELETE FROM observations WHERE observed_at < ? AND id NOT IN ("
            "  SELECT MIN(id) FROM observations WHERE observed_at < ? "
            "  GROUP BY COALESCE(correlation_key, record_id), "
            "           CAST(strftime('%s', observed_at) / ? AS INTEGER))",
            (cutoff_iso, cutoff_iso, gap_s),
        )
        conn.commit()
        return cur.rowcount

    def vacuum(self) -> None:
        """Rebuild the database file to reclaim space freed by deletes (PRD §19.2).

        DELETE alone leaves freed pages in the file; VACUUM is what actually shrinks
        it back under the size budget. Run only under storage pressure — it takes a
        brief write lock (the writer drops a batch rather than blocking, PRD §37).
        """
        self._require_conn().execute("VACUUM")

    def checkpoint_truncate(self) -> None:
        """Checkpoint and truncate the WAL so the ``-wal`` sidecar can't grow unbounded."""
        self._require_conn().execute("PRAGMA wal_checkpoint(TRUNCATE)")

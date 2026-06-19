"""SQLite WAL database handle for the persistence store (PRD §19.2).

A thin wrapper around a single ``sqlite3`` connection in WAL mode with foreign
keys on. Opened once at startup (migrations run here); every write goes through
the single-owner :class:`~aether.persist.writer.PersistenceWriter` drain loop, so
the connection — created ``check_same_thread=False`` so it can be driven from
``asyncio.to_thread`` — is only ever touched by one logical writer at a time
(PRD §19.2 "single bounded async write queue").
"""

from __future__ import annotations

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


class Database:
    """Owns the persistence connection. All methods are blocking — call from a thread."""

    def __init__(self, path: str, *, busy_timeout_ms: int = 5000) -> None:
        self._path = path
        self._busy_timeout_ms = busy_timeout_ms
        self._conn: sqlite3.Connection | None = None

    def open(self) -> None:
        """Open the connection, set WAL pragmas, and run migrations (blocking)."""
        conn = sqlite3.connect(self._path, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(f"PRAGMA busy_timeout={self._busy_timeout_ms}")
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

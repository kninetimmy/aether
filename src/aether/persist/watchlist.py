"""CRUD persistence for the operator watchlist (PRD §24.6, §21.5).

The watchlist is low-volume operator config, not the high-rate observation stream, so
it does **not** go through the single-writer drain loop. Each call opens its own
short-lived connection and closes it — reads on a fresh *read-only* handle (so they
never touch the writer's or retention's connection, PRD §5), writes on a short
read-write handle that WAL lets coexist with the observation writer. The full entry
is stored as JSON in ``payload`` and reconstructed losslessly on read; the flattened
columns exist only for ordering.

Schema ownership: the ``watchlist`` table is migration v4, applied by the persistence
writer when it opens the store at lifespan startup (PRD §19.2). These helpers open
with migrations *off* (siblings, like retention): reads tolerate a not-yet-created
store by returning empty; a write before the store is migrated raises
``sqlite3.OperationalError`` for the API to map to an honest 503. All blocking —
drive from ``asyncio.to_thread`` so they never block the event loop.

The PRIMARY KEY is the canonical watchlist_key string (e.g. ``aircraft:icao:abc123``,
``orbital:celestrak:25544``, ``aprs:N0CALL-9``) — client-minted, stable, and
deterministic, so PUT-upsert is the natural write semantic.
"""

from __future__ import annotations

import sqlite3

from aether.schema.watchlist import WatchlistEntry

#: Match the writer/retention busy-timeout so a brief write-lock overlap waits
#: rather than failing immediately (PRD §19.2).
_BUSY_TIMEOUT_MS = 5000


def _connect_ro(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)
    conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
    return conn


def _connect_rw(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def list_watchlist(path: str) -> list[WatchlistEntry]:
    """Return all stored watchlist entries, oldest-first (read-only, blocking).

    A missing store or not-yet-created table (persistence on but nothing written)
    yields an empty list rather than an error — the same honest degradation the
    track-history reader uses (PRD §37).
    """
    try:
        conn = _connect_ro(path)
    except sqlite3.OperationalError:
        return []  # store file does not exist yet
    try:
        rows = conn.execute("SELECT payload FROM watchlist ORDER BY created_at, key").fetchall()
    except sqlite3.OperationalError:
        return []  # table not created yet
    finally:
        conn.close()
    return [WatchlistEntry.model_validate_json(row[0]) for row in rows]


def get_watchlist_entry(path: str, key: str) -> WatchlistEntry | None:
    """Return one watchlist entry by key, or ``None`` if absent/uncreated (read-only)."""
    try:
        conn = _connect_ro(path)
    except sqlite3.OperationalError:
        return None
    try:
        row = conn.execute("SELECT payload FROM watchlist WHERE key = ?", (key,)).fetchone()
    except sqlite3.OperationalError:
        return None
    finally:
        conn.close()
    return WatchlistEntry.model_validate_json(row[0]) if row is not None else None


def upsert_watchlist_entry(path: str, entry: WatchlistEntry) -> None:
    """Insert or replace a watchlist entry (read-write, blocking).

    Uses ``INSERT OR REPLACE`` so toggle-on is idempotent and creates-or-updates
    without a pre-check. The caller computes the full entry (preserving ``created_at``
    on update); this replaces the row wholesale.

    Raises ``sqlite3.OperationalError`` if the store is not yet migrated (cold start
    before the writer opened it) — the API maps the latter to a 503.
    """
    conn = _connect_rw(path)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO watchlist "
            "(key, label, priority, notes, created_at, updated_at, payload) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                entry.key,
                entry.label,
                entry.priority,
                entry.notes,
                entry.created_at.isoformat(),
                entry.updated_at.isoformat(),
                entry.model_dump_json(),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def delete_watchlist_entry(path: str, key: str) -> bool:
    """Delete a watchlist entry by key; return whether a row was removed (read-write)."""
    conn = _connect_rw(path)
    try:
        cur = conn.execute("DELETE FROM watchlist WHERE key = ?", (key,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()

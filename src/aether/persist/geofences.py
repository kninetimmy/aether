"""CRUD persistence for operator geofences (PRD §19.3, §21.5).

Geofences are low-volume operator config, not the high-rate observation stream, so
they do **not** go through the single-writer drain loop. Each call opens its own
short-lived connection and closes it — reads on a fresh *read-only* handle (so they
never touch the writer's or retention's connection, PRD §5), writes on a short
read-write handle that WAL lets coexist with the observation writer. The full
geofence is stored as JSON in ``payload`` and reconstructed losslessly on read; the
flattened columns exist only for ordering.

Schema ownership: the ``geofences`` table is migration v2, applied by the
persistence writer when it opens the store at lifespan startup (PRD §19.2). These
helpers open with migrations *off* (siblings, like retention): reads tolerate a
not-yet-created store by returning empty; a write before the store is migrated
raises ``sqlite3.OperationalError`` for the API to map to an honest 503. All
blocking — drive from ``asyncio.to_thread`` so they never block the event loop.
"""

from __future__ import annotations

import sqlite3

from aether.schema.geofence import Geofence

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


def list_geofences(path: str) -> list[Geofence]:
    """Return all stored geofences, oldest-first (read-only, blocking).

    A missing store or not-yet-created table (persistence on but nothing written)
    yields an empty list rather than an error — the same honest degradation the
    track-history reader uses (PRD §37).
    """
    try:
        conn = _connect_ro(path)
    except sqlite3.OperationalError:
        return []  # store file does not exist yet
    try:
        rows = conn.execute("SELECT payload FROM geofences ORDER BY created_at, id").fetchall()
    except sqlite3.OperationalError:
        return []  # table not created yet
    finally:
        conn.close()
    return [Geofence.model_validate_json(row[0]) for row in rows]


def get_geofence(path: str, geofence_id: str) -> Geofence | None:
    """Return one geofence by id, or ``None`` if absent/uncreated (read-only)."""
    try:
        conn = _connect_ro(path)
    except sqlite3.OperationalError:
        return None
    try:
        row = conn.execute("SELECT payload FROM geofences WHERE id = ?", (geofence_id,)).fetchone()
    except sqlite3.OperationalError:
        return None
    finally:
        conn.close()
    return Geofence.model_validate_json(row[0]) if row is not None else None


def insert_geofence(path: str, geofence: Geofence) -> None:
    """Insert a new geofence (read-write, blocking).

    Raises ``sqlite3.IntegrityError`` if the id already exists, and
    ``sqlite3.OperationalError`` if the store is not yet migrated (cold start before
    the writer opened it) — the API maps the latter to a 503.
    """
    conn = _connect_rw(path)
    try:
        conn.execute(
            "INSERT INTO geofences (id, name, enabled, created_at, updated_at, payload) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                geofence.id,
                geofence.name,
                int(geofence.enabled),
                geofence.created_at.isoformat(),
                geofence.updated_at.isoformat(),
                geofence.model_dump_json(),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def update_geofence(path: str, geofence: Geofence) -> bool:
    """Overwrite an existing geofence's row; return whether a row was updated.

    The caller computes the new geofence (preserving ``created_at``); this replaces
    the mutable columns + payload. ``False`` means no row with that id existed.
    """
    conn = _connect_rw(path)
    try:
        cur = conn.execute(
            "UPDATE geofences SET name = ?, enabled = ?, updated_at = ?, payload = ? WHERE id = ?",
            (
                geofence.name,
                int(geofence.enabled),
                geofence.updated_at.isoformat(),
                geofence.model_dump_json(),
                geofence.id,
            ),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def delete_geofence(path: str, geofence_id: str) -> bool:
    """Delete a geofence by id; return whether a row was removed (read-write)."""
    conn = _connect_rw(path)
    try:
        cur = conn.execute("DELETE FROM geofences WHERE id = ?", (geofence_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()

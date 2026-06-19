"""Versioned SQLite migrations for the persistence store (PRD §19.2).

The schema is built forward-only by numbered :class:`Migration` steps. Each runs
exactly once; ``schema_migrations`` records which versions have been applied so a
re-open is a no-op. Introduced at M4 with its first consumer — track history in
the ``observations`` table (PRD §19.3). Later milestones *append* migrations for
events, alerts, geofences, watchlist, and the rest of §19.3; a released migration
is never edited or renumbered (PRD §37 schema guardrail).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime


@dataclass(frozen=True)
class Migration:
    """One forward-only schema step: a version, a name, and its DDL statements."""

    version: int
    name: str
    statements: tuple[str, ...]


#: The ordered migration set. Append new versions; never edit or renumber a
#: released one.
MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        version=1,
        name="observations",
        statements=(
            """
            CREATE TABLE observations (
                id              INTEGER PRIMARY KEY,
                record_id       TEXT    NOT NULL,
                correlation_key TEXT,
                kind            TEXT    NOT NULL,
                track_type      TEXT,
                source          TEXT    NOT NULL,
                lon             REAL,
                lat             REAL,
                alt_m           REAL,
                observed_at     TEXT    NOT NULL,
                received_at     TEXT    NOT NULL,
                persisted_at    TEXT    NOT NULL,
                payload         TEXT    NOT NULL
            )
            """,
            "CREATE INDEX ix_observations_corr_observed "
            "ON observations (correlation_key, observed_at)",
            "CREATE INDEX ix_observations_observed ON observations (observed_at)",
        ),
    ),
    Migration(
        version=2,
        name="geofences",
        statements=(
            # Operator-config geofences (PRD §19.3, §11.1 COP-FR-008). Low-volume,
            # CRUD-managed via the API — distinct from the high-rate observation
            # stream. ``payload`` holds the full geofence JSON (authoritative shape);
            # the flattened columns back listing/ordering without parsing every row.
            """
            CREATE TABLE geofences (
                id          TEXT    PRIMARY KEY,
                name        TEXT    NOT NULL,
                enabled     INTEGER NOT NULL,
                created_at  TEXT    NOT NULL,
                updated_at  TEXT    NOT NULL,
                payload     TEXT    NOT NULL
            )
            """,
            "CREATE INDEX ix_geofences_created ON geofences (created_at)",
        ),
    ),
    Migration(
        version=3,
        name="alert_rules",
        statements=(
            # Operator-config alert rules (PRD §20.1, §11.16 ALERT-FR-002). Like
            # geofences: low-volume, CRUD-managed via the API, distinct from the
            # high-rate observation stream. ``payload`` holds the full rule JSON
            # (authoritative shape); the flattened columns back listing/ordering and
            # cheap severity/enabled filtering without parsing every row.
            """
            CREATE TABLE alert_rules (
                id          TEXT    PRIMARY KEY,
                name        TEXT    NOT NULL,
                enabled     INTEGER NOT NULL,
                severity    TEXT    NOT NULL,
                created_at  TEXT    NOT NULL,
                updated_at  TEXT    NOT NULL,
                payload     TEXT    NOT NULL
            )
            """,
            "CREATE INDEX ix_alert_rules_created ON alert_rules (created_at)",
        ),
    ),
)


def _ensure_bookkeeping(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        "version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)"
    )
    conn.commit()


def applied_versions(conn: sqlite3.Connection) -> set[int]:
    """Return the set of migration versions already applied to ``conn``."""
    _ensure_bookkeeping(conn)
    return {int(row[0]) for row in conn.execute("SELECT version FROM schema_migrations")}


def apply_migrations(
    conn: sqlite3.Connection, *, now: Callable[[], datetime] | None = None
) -> list[int]:
    """Apply every unapplied migration in order; return the versions applied.

    Each migration's DDL and its ``schema_migrations`` bookkeeping commit together,
    so re-opening an up-to-date database re-runs nothing and returns ``[]``.
    """
    clock = now or (lambda: datetime.now(UTC))
    done = applied_versions(conn)
    applied: list[int] = []
    for migration in sorted(MIGRATIONS, key=lambda m: m.version):
        if migration.version in done:
            continue
        for statement in migration.statements:
            conn.execute(statement)
        conn.execute(
            "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
            (migration.version, migration.name, clock().isoformat()),
        )
        conn.commit()
        applied.append(migration.version)
    return applied

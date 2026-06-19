"""CRUD persistence for operator alert rules (PRD §19.3, §20.1, §21.4).

Alert rules are low-volume operator config, not the high-rate observation stream,
so — exactly like geofences — they do **not** go through the single-writer drain
loop. Each call opens its own short-lived connection and closes it: reads on a
fresh *read-only* handle (so they never touch the writer's or retention's
connection, PRD §5), writes on a short read-write handle that WAL lets coexist
with the observation writer. The full rule is stored as JSON in ``payload`` and
reconstructed losslessly on read; the flattened columns exist only for ordering
and cheap filtering.

Schema ownership: the ``alert_rules`` table is migration v3, applied by the
persistence writer when it opens the store at lifespan startup (PRD §19.2). These
helpers open with migrations *off* (siblings, like geofences/retention): reads
tolerate a not-yet-created store by returning empty; a write before the store is
migrated raises ``sqlite3.OperationalError`` for the API to map to an honest 503.
All blocking — drive from ``asyncio.to_thread`` so they never block the event loop.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable

from aether.schema.alert_rule import AlertRule

#: Match the writer/retention/geofence busy-timeout so a brief write-lock overlap
#: waits rather than failing immediately (PRD §19.2).
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


def list_alert_rules(path: str) -> list[AlertRule]:
    """Return all stored alert rules, oldest-first (read-only, blocking).

    A missing store or not-yet-created table (persistence on but nothing written)
    yields an empty list rather than an error — the same honest degradation the
    geofence/track-history readers use (PRD §37).
    """
    try:
        conn = _connect_ro(path)
    except sqlite3.OperationalError:
        return []  # store file does not exist yet
    try:
        rows = conn.execute("SELECT payload FROM alert_rules ORDER BY created_at, id").fetchall()
    except sqlite3.OperationalError:
        return []  # table not created yet
    finally:
        conn.close()
    return [AlertRule.model_validate_json(row[0]) for row in rows]


def get_alert_rule(path: str, rule_id: str) -> AlertRule | None:
    """Return one alert rule by id, or ``None`` if absent/uncreated (read-only)."""
    try:
        conn = _connect_ro(path)
    except sqlite3.OperationalError:
        return None
    try:
        row = conn.execute("SELECT payload FROM alert_rules WHERE id = ?", (rule_id,)).fetchone()
    except sqlite3.OperationalError:
        return None
    finally:
        conn.close()
    return AlertRule.model_validate_json(row[0]) if row is not None else None


def insert_alert_rule(path: str, rule: AlertRule) -> None:
    """Insert a new alert rule (read-write, blocking).

    Raises ``sqlite3.IntegrityError`` if the id already exists, and
    ``sqlite3.OperationalError`` if the store is not yet migrated (cold start before
    the writer opened it) — the API maps the latter to a 503.
    """
    conn = _connect_rw(path)
    try:
        conn.execute(*_insert_sql(rule))
        conn.commit()
    finally:
        conn.close()


def update_alert_rule(path: str, rule: AlertRule) -> bool:
    """Overwrite an existing rule's row; return whether a row was updated.

    The caller computes the new rule (preserving ``created_at``); this replaces the
    mutable columns + payload. ``False`` means no row with that id existed.
    """
    conn = _connect_rw(path)
    try:
        cur = conn.execute(
            "UPDATE alert_rules SET name = ?, enabled = ?, severity = ?, updated_at = ?, "
            "payload = ? WHERE id = ?",
            (
                rule.name,
                int(rule.enabled),
                rule.severity,
                rule.updated_at.isoformat(),
                rule.model_dump_json(),
                rule.id,
            ),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def delete_alert_rule(path: str, rule_id: str) -> bool:
    """Delete an alert rule by id; return whether a row was removed (read-write)."""
    conn = _connect_rw(path)
    try:
        cur = conn.execute("DELETE FROM alert_rules WHERE id = ?", (rule_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def seed_alert_rules(path: str, rules: Iterable[AlertRule]) -> int:
    """Insert any default rules whose id is absent; return how many were inserted.

    Idempotent (PRD §11.16 ALERT-FR-008): ``INSERT OR IGNORE`` keyed on the stable
    template id means a re-seed inserts nothing and an operator's edits to a seeded
    rule are never clobbered. One short read-write connection for the whole batch.
    Raises ``sqlite3.OperationalError`` if the store is not yet migrated; the caller
    (lifespan startup) isolates that so a cold store never wedges boot (PRD §5/§37).
    """
    conn = _connect_rw(path)
    try:
        inserted = 0
        for rule in rules:
            sql, params = _insert_sql(rule, ignore=True)
            inserted += conn.execute(sql, params).rowcount
        conn.commit()
        return inserted
    finally:
        conn.close()


def _insert_sql(rule: AlertRule, *, ignore: bool = False) -> tuple[str, tuple[object, ...]]:
    verb = "INSERT OR IGNORE INTO" if ignore else "INSERT INTO"
    return (
        f"{verb} alert_rules (id, name, enabled, severity, created_at, updated_at, payload) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            rule.id,
            rule.name,
            int(rule.enabled),
            rule.severity,
            rule.created_at.isoformat(),
            rule.updated_at.isoformat(),
            rule.model_dump_json(),
        ),
    )

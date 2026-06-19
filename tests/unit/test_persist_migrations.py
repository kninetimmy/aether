"""Unit tests for the SQLite migration framework (PRD §19.2)."""

import sqlite3
from pathlib import Path

from aether.persist.database import Database
from aether.persist.migrations import MIGRATIONS, applied_versions, apply_migrations


def _open(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(path)


def test_apply_migrations_creates_schema_and_records_versions(tmp_path: Path) -> None:
    conn = _open(tmp_path / "t.db")
    applied = apply_migrations(conn)

    assert applied == [m.version for m in MIGRATIONS]
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "observations" in tables
    assert "schema_migrations" in tables
    assert applied_versions(conn) == {m.version for m in MIGRATIONS}


def test_apply_migrations_is_idempotent(tmp_path: Path) -> None:
    conn = _open(tmp_path / "t.db")
    apply_migrations(conn)
    # A second pass over an up-to-date DB applies nothing and does not raise.
    assert apply_migrations(conn) == []


def test_observations_indexes_exist(tmp_path: Path) -> None:
    conn = _open(tmp_path / "t.db")
    apply_migrations(conn)
    indexes = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert "ix_observations_corr_observed" in indexes
    assert "ix_observations_observed" in indexes


def test_database_open_sets_wal_and_migrates(tmp_path: Path) -> None:
    db = Database(str(tmp_path / "wal.db"))
    db.open()
    try:
        assert db.count_observations() == 0
        # open() applied migrations and put the file in WAL mode.
        conn = sqlite3.connect(tmp_path / "wal.db")
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"
    finally:
        db.close()

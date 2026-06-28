"""Watchlist CRUD persistence round-trips + honest missing-store degradation (M6.6b)."""

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from aether.persist.database import Database
from aether.persist.watchlist import (
    delete_watchlist_entry,
    get_watchlist_entry,
    list_watchlist,
    upsert_watchlist_entry,
)
from aether.schema.watchlist import WatchlistEntry, WatchlistEntryCreate

T0 = datetime(2026, 6, 28, 12, 0, 0, tzinfo=UTC)
T1 = datetime(2026, 6, 28, 13, 0, 0, tzinfo=UTC)


def _migrated_db(tmp_path: Path) -> str:
    """A store with the schema applied (migration v4 creates ``watchlist``)."""
    path = str(tmp_path / "watchlist.db")
    db = Database(path)
    db.open()  # runs all migrations including v4
    db.close()
    return path


def _entry(
    key: str,
    *,
    label: str | None = None,
    priority: int | None = None,
    now: datetime = T0,
) -> WatchlistEntry:
    return WatchlistEntry.create(
        WatchlistEntryCreate(label=label, priority=priority),
        key=key,
        now=now,
    )


def test_insert_then_list_and_get_round_trip(tmp_path: Path) -> None:
    path = _migrated_db(tmp_path)
    upsert_watchlist_entry(path, _entry("aircraft:icao:abc123", label="Alpha"))
    upsert_watchlist_entry(path, _entry("mmsi:366000001", label="Bravo"))

    listed = list_watchlist(path)
    assert [e.key for e in listed] == ["aircraft:icao:abc123", "mmsi:366000001"]

    one = get_watchlist_entry(path, "aircraft:icao:abc123")
    assert one is not None
    assert one.label == "Alpha"
    assert one.key == "aircraft:icao:abc123"


def test_upsert_replaces_meta_while_preserving_created_at(tmp_path: Path) -> None:
    path = _migrated_db(tmp_path)
    entry = _entry("aircraft:icao:abc123", label="Old", priority=1)
    upsert_watchlist_entry(path, entry)

    # Build an updated entry (preserving created_at, changing label, bumping updated_at)
    updated = WatchlistEntry(
        key="aircraft:icao:abc123",
        label="New",
        priority=2,
        created_at=T0,
        updated_at=T1,
    )
    upsert_watchlist_entry(path, updated)

    fetched = get_watchlist_entry(path, "aircraft:icao:abc123")
    assert fetched is not None
    assert fetched.label == "New"
    assert fetched.priority == 2
    assert fetched.created_at == T0  # preserved
    assert fetched.updated_at == T1


def test_delete_reports_existence(tmp_path: Path) -> None:
    path = _migrated_db(tmp_path)
    upsert_watchlist_entry(path, _entry("aircraft:icao:abc123"))
    assert delete_watchlist_entry(path, "aircraft:icao:abc123") is True
    assert delete_watchlist_entry(path, "aircraft:icao:abc123") is False  # already gone
    assert get_watchlist_entry(path, "aircraft:icao:abc123") is None


def test_list_is_oldest_first(tmp_path: Path) -> None:
    path = _migrated_db(tmp_path)
    # Insert three entries with different created_at times
    upsert_watchlist_entry(path, _entry("b-key", now=T1))
    upsert_watchlist_entry(path, _entry("a-key", now=T0))
    listed = list_watchlist(path)
    # T0 < T1, so "a-key" comes first
    assert listed[0].key == "a-key"
    assert listed[1].key == "b-key"


def test_reads_tolerate_missing_store_file(tmp_path: Path) -> None:
    missing = str(tmp_path / "never.db")
    assert list_watchlist(missing) == []
    assert get_watchlist_entry(missing, "anything") is None


def test_reads_tolerate_uncreated_table(tmp_path: Path) -> None:
    # A store file with no watchlist table yet (only partially migrated) reads as empty.
    path = str(tmp_path / "empty.db")
    sqlite3.connect(path).close()  # create file, no schema
    assert list_watchlist(path) == []
    assert get_watchlist_entry(path, "anything") is None


def test_colon_bearing_key_survives_round_trip(tmp_path: Path) -> None:
    """Keys with colons (the common case) store and retrieve correctly."""
    path = _migrated_db(tmp_path)
    key = "orbital:celestrak:25544"
    upsert_watchlist_entry(path, _entry(key, label="ISS"))
    fetched = get_watchlist_entry(path, key)
    assert fetched is not None
    assert fetched.key == key
    assert fetched.label == "ISS"

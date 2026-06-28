"""Watchlist entry model: create/with_update round-trips and validation (M6.6b)."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from aether.schema.watchlist import (
    WatchlistEntry,
    WatchlistEntryCreate,
    WatchlistEntryUpdate,
)

T0 = datetime(2026, 6, 28, 12, 0, 0, tzinfo=UTC)
T1 = datetime(2026, 6, 28, 13, 0, 0, tzinfo=UTC)


def _entry(key: str = "aircraft:icao:abc123", **kwargs: object) -> WatchlistEntry:
    return WatchlistEntry.create(
        WatchlistEntryCreate(**kwargs),  # type: ignore[arg-type]
        key=key,
        now=T0,
    )


def test_create_sets_both_timestamps_to_now() -> None:
    entry = _entry(label="Test Aircraft", priority=3)
    assert entry.key == "aircraft:icao:abc123"
    assert entry.label == "Test Aircraft"
    assert entry.priority == 3
    assert entry.created_at == T0
    assert entry.updated_at == T0


def test_create_with_all_optional_fields_none() -> None:
    entry = _entry()
    assert entry.label is None
    assert entry.priority is None
    assert entry.notes is None


def test_with_update_changes_only_set_fields_and_bumps_updated_at() -> None:
    entry = _entry(label="Old", priority=1, notes="old note")
    updated = entry.with_update(WatchlistEntryUpdate(label="New"), now=T1)
    assert updated.label == "New"
    assert updated.priority == 1  # unchanged
    assert updated.notes == "old note"  # unchanged
    assert updated.created_at == T0  # preserved
    assert updated.updated_at == T1  # bumped
    assert updated.key == entry.key


def test_with_update_does_not_clear_with_none_omitted_field() -> None:
    """A field absent from the patch (not in model_fields_set) is left unchanged."""
    entry = _entry(label="Keep", priority=5)
    # patch only sets priority — label should stay
    updated = entry.with_update(WatchlistEntryUpdate(priority=7), now=T1)
    assert updated.label == "Keep"
    assert updated.priority == 7


def test_with_update_revalidates() -> None:
    """Re-validation catches a bad patch value (priority out of range)."""
    entry = _entry(priority=1)
    # Cannot patch to priority=10 (le=9 constraint)
    with pytest.raises(ValidationError):
        entry.with_update(WatchlistEntryUpdate(priority=10), now=T1)  # type: ignore[arg-type]


def test_extra_fields_forbidden_on_create() -> None:
    with pytest.raises(ValidationError):
        WatchlistEntryCreate(label="x", unknown_field="oops")  # type: ignore[call-arg]


def test_extra_fields_forbidden_on_update() -> None:
    with pytest.raises(ValidationError):
        WatchlistEntryUpdate(priority=1, unknown_field="oops")  # type: ignore[call-arg]


def test_extra_fields_forbidden_on_entry() -> None:
    with pytest.raises(ValidationError):
        WatchlistEntry(
            key="aircraft:icao:abc123",
            created_at=T0,
            updated_at=T0,
            unknown_field="oops",  # type: ignore[call-arg]
        )


def test_priority_bounds() -> None:
    # Valid bounds
    WatchlistEntry.create(WatchlistEntryCreate(priority=0), key="k", now=T0)
    WatchlistEntry.create(WatchlistEntryCreate(priority=9), key="k", now=T0)
    # Below 0
    with pytest.raises(ValidationError):
        WatchlistEntryCreate(priority=-1)  # type: ignore[arg-type]
    # Above 9
    with pytest.raises(ValidationError):
        WatchlistEntryCreate(priority=10)  # type: ignore[arg-type]


def test_label_max_length() -> None:
    WatchlistEntryCreate(label="x" * 200)  # exactly at max
    with pytest.raises(ValidationError):
        WatchlistEntryCreate(label="x" * 201)


def test_notes_max_length() -> None:
    WatchlistEntryCreate(notes="x" * 2000)
    with pytest.raises(ValidationError):
        WatchlistEntryCreate(notes="x" * 2001)


def test_key_min_length_enforced() -> None:
    with pytest.raises(ValidationError):
        WatchlistEntry(key="", created_at=T0, updated_at=T0)


def test_key_max_length_enforced() -> None:
    with pytest.raises(ValidationError):
        WatchlistEntry(key="k" * 257, created_at=T0, updated_at=T0)

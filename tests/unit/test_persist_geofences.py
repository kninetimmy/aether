"""Geofence CRUD persistence round-trips + honest missing-store degradation (M4.4)."""

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from aether.persist.database import Database
from aether.persist.geofences import (
    delete_geofence,
    get_geofence,
    insert_geofence,
    list_geofences,
    update_geofence,
)
from aether.schema.geofence import CircleShape, Geofence, GeofenceCreate, GeofenceUpdate

T0 = datetime(2026, 6, 19, 12, 0, 0, tzinfo=UTC)
T1 = datetime(2026, 6, 19, 13, 0, 0, tzinfo=UTC)


def _migrated_db(tmp_path: Path) -> str:
    """A store with the schema applied (migration v2 creates ``geofences``)."""
    path = str(tmp_path / "geofences.db")
    db = Database(path)
    db.open()  # runs all migrations
    db.close()
    return path


def _gf(id: str, *, name: str = "ring", now: datetime = T0) -> Geofence:
    return Geofence.create(
        GeofenceCreate(name=name, shape=CircleShape(center=[-95.0, 40.0], radius_m=1500.0)),
        id=id,
        now=now,
    )


def test_insert_then_list_and_get_round_trip(tmp_path: Path) -> None:
    path = _migrated_db(tmp_path)
    insert_geofence(path, _gf("gf-a", name="alpha"))
    insert_geofence(path, _gf("gf-b", name="bravo"))

    listed = list_geofences(path)
    assert [g.id for g in listed] == ["gf-a", "gf-b"]  # oldest-first by created_at, id

    one = get_geofence(path, "gf-a")
    assert one is not None
    assert one.name == "alpha"
    assert isinstance(one.shape, CircleShape)
    assert one.shape.radius_m == 1500.0


def test_update_replaces_mutable_fields(tmp_path: Path) -> None:
    path = _migrated_db(tmp_path)
    gf = _gf("gf-a", name="alpha")
    insert_geofence(path, gf)

    updated = gf.with_update(GeofenceUpdate(name="renamed", enabled=False), now=T1)
    assert update_geofence(path, updated) is True

    fetched = get_geofence(path, "gf-a")
    assert fetched is not None
    assert fetched.name == "renamed"
    assert fetched.enabled is False
    assert fetched.created_at == T0  # preserved
    assert fetched.updated_at == T1


def test_update_missing_row_returns_false(tmp_path: Path) -> None:
    path = _migrated_db(tmp_path)
    assert update_geofence(path, _gf("ghost")) is False


def test_delete_reports_existence(tmp_path: Path) -> None:
    path = _migrated_db(tmp_path)
    insert_geofence(path, _gf("gf-a"))
    assert delete_geofence(path, "gf-a") is True
    assert delete_geofence(path, "gf-a") is False  # already gone
    assert get_geofence(path, "gf-a") is None


def test_reads_tolerate_missing_store_file(tmp_path: Path) -> None:
    missing = str(tmp_path / "never.db")
    assert list_geofences(missing) == []
    assert get_geofence(missing, "anything") is None


def test_reads_tolerate_uncreated_table(tmp_path: Path) -> None:
    # A store file with no geofences table yet (nothing migrated) reads as empty.
    path = str(tmp_path / "empty.db")
    sqlite3.connect(path).close()  # create the file, no schema
    assert list_geofences(path) == []
    assert get_geofence(path, "anything") is None

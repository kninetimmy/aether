"""End-to-end: geofence CRUD → live overlay → ws snapshot/deltas (M4.4, PRD §21.5).

Demo OFF + persistence ON, so the bus is otherwise quiet and the geofence overlay
deltas are deterministic. Proves the three projection paths: a *persisted* geofence
is republished into the first snapshot at startup; a *created* one arrives as a
``feature_upsert`` delta; a *deleted* one as a ``remove``. Skips when no broker is
reachable (see conftest); CI starts Mosquitto so it runs there.
"""

import dataclasses
from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient

from aether.backend.main import create_app
from aether.config import Settings
from aether.persist.database import Database
from aether.persist.geofences import insert_geofence
from aether.schema.geofence import CircleShape, Geofence, GeofenceCreate

_CIRCLE = {"name": "ring", "shape": {"kind": "circle", "center": [-95.0, 40.0], "radius_m": 5000.0}}


def _seed_geofence(db_path: str, gid: str) -> None:
    db = Database(db_path)  # create schema (migration v2)
    db.open()
    db.close()
    insert_geofence(
        db_path,
        Geofence.create(
            GeofenceCreate(name="seeded", shape=CircleShape(center=[-95.0, 40.0], radius_m=2000.0)),
            id=gid,
            now=datetime(2026, 6, 19, 12, 0, 0, tzinfo=UTC),
        ),
    )


def _app(settings: Settings, db_path: str) -> TestClient:
    cfg = dataclasses.replace(settings, demo_source=False, persist=True, db_path=db_path)
    return TestClient(create_app(settings=cfg, demo_interval_s=0.05))


def _next_of_type(ws: object, wanted: str, *, tries: int = 50) -> dict:
    for _ in range(tries):
        msg = ws.receive_json()  # type: ignore[attr-defined]
        if msg["type"] == wanted:
            return msg  # type: ignore[no-any-return]
    raise AssertionError(f"no {wanted!r} frame arrived")


def test_persisted_geofence_republished_then_create_and_delete_stream(
    broker_settings: Settings, tmp_path: Path
) -> None:
    db_path = str(tmp_path / "geofences.db")
    _seed_geofence(db_path, "geofence-seeded01")

    with _app(broker_settings, db_path) as client, client.websocket_connect("/ws/v2") as ws:
        # 1) Startup republish: the seeded geofence is in the first snapshot.
        snapshot = ws.receive_json()
        assert snapshot["type"] == "snapshot"
        geofence_ids = {f["id"] for f in snapshot["features"] if f["feature_type"] == "geofence"}
        assert "geofence-seeded01" in geofence_ids

        # 2) Create: the new geofence arrives as a feature_upsert delta.
        created = client.post("/api/v2/geofences", json=_CIRCLE)
        assert created.status_code == 201
        gid = created.json()["id"]
        upsert = _next_of_type(ws, "feature_upsert")
        assert upsert["record"]["id"] == gid
        assert upsert["record"]["feature_type"] == "geofence"

        # 3) Delete: the overlay is removed from the stream.
        assert client.delete(f"/api/v2/geofences/{gid}").status_code == 204
        removed = _next_of_type(ws, "remove")
        assert removed["id"] == gid
        assert removed["kind"] == "feature"

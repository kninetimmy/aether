"""Endpoint tests for geofence CRUD (M4.4, PRD §21.5).

Hermetic like the history API tests: the :class:`TestClient` runs WITHOUT its
context manager so the app lifespan (MQTT subscriber) never starts. CRUD hits the
REST path against a pre-migrated temp store, and the create/delete projection into
live state is observed via ``GET /api/state`` (``hub.publish``/``hub.remove`` are
synchronous in the handler). The startup-republish + ws-delta path is covered in
the integration suite.
"""

from pathlib import Path

from fastapi.testclient import TestClient

from aether.backend.main import create_app
from aether.config import Settings
from aether.persist.database import Database

_CIRCLE = {"name": "ring", "shape": {"kind": "circle", "center": [-95.0, 40.0], "radius_m": 5000.0}}


def _client(tmp_path: Path, *, persist: bool = True) -> TestClient:
    path = str(tmp_path / "geofences.db")
    if persist:
        db = Database(path)  # pre-migrate so the geofences table exists for writes
        db.open()
        db.close()
    settings = Settings(demo_source=False, persist=persist, db_path=path)
    return TestClient(create_app(settings=settings))  # no `with` → lifespan not run


def _geofence_features(client: TestClient) -> list[dict]:
    feats = client.get("/api/state").json()["features"]
    return [f for f in feats if f["feature_type"] == "geofence"]


def test_create_lists_and_projects_to_live_state(tmp_path: Path) -> None:
    client = _client(tmp_path)
    created = client.post("/api/v2/geofences", json=_CIRCLE)
    assert created.status_code == 201
    gid = created.json()["id"]
    assert gid.startswith("geofence-")

    listing = client.get("/api/v2/geofences").json()
    assert listing["count"] == 1
    assert listing["geofences"][0]["id"] == gid

    # It is now a live overlay feature (projected via hub.publish on create).
    features = _geofence_features(client)
    assert [f["id"] for f in features] == [gid]
    assert features[0]["geometry"]["type"] == "Polygon"  # circle rendered as a polygon


def test_get_one_and_404(tmp_path: Path) -> None:
    client = _client(tmp_path)
    gid = client.post("/api/v2/geofences", json=_CIRCLE).json()["id"]
    assert client.get(f"/api/v2/geofences/{gid}").status_code == 200
    assert client.get("/api/v2/geofences/nope").status_code == 404


def test_patch_updates_fields(tmp_path: Path) -> None:
    client = _client(tmp_path)
    gid = client.post("/api/v2/geofences", json=_CIRCLE).json()["id"]
    resp = client.patch(f"/api/v2/geofences/{gid}", json={"name": "renamed", "enabled": False})
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "renamed"
    assert body["enabled"] is False
    assert client.get(f"/api/v2/geofences/{gid}").json()["name"] == "renamed"


def test_patch_404_for_unknown(tmp_path: Path) -> None:
    client = _client(tmp_path)
    assert client.patch("/api/v2/geofences/nope", json={"name": "x"}).status_code == 404


def test_delete_removes_from_store_and_state(tmp_path: Path) -> None:
    client = _client(tmp_path)
    gid = client.post("/api/v2/geofences", json=_CIRCLE).json()["id"]
    assert _geofence_features(client)  # present before delete

    assert client.delete(f"/api/v2/geofences/{gid}").status_code == 204
    assert client.get(f"/api/v2/geofences/{gid}").status_code == 404
    assert _geofence_features(client) == []  # overlay dropped from live state


def test_delete_404_for_unknown(tmp_path: Path) -> None:
    client = _client(tmp_path)
    assert client.delete("/api/v2/geofences/nope").status_code == 404


def test_503_when_persistence_disabled(tmp_path: Path) -> None:
    client = _client(tmp_path, persist=False)
    assert client.get("/api/v2/geofences").status_code == 503
    assert client.post("/api/v2/geofences", json=_CIRCLE).status_code == 503
    assert client.delete("/api/v2/geofences/whatever").status_code == 503


def test_create_422_on_invalid_shape(tmp_path: Path) -> None:
    client = _client(tmp_path)
    bad = {"name": "ring", "shape": {"kind": "circle", "center": [-95.0, 40.0], "radius_m": 0.0}}
    assert client.post("/api/v2/geofences", json=bad).status_code == 422

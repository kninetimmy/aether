"""Endpoint tests for alert-rule CRUD (M4.5, PRD §21.4).

Hermetic like the geofence/history API tests: the :class:`TestClient` runs WITHOUT
its context manager so the app lifespan (MQTT subscriber + template seeding) never
starts. CRUD hits the REST path against a pre-migrated temp store, so the store
holds only what each test creates. Startup template seeding is covered in the
integration suite.
"""

from pathlib import Path

from fastapi.testclient import TestClient

from aether.backend.main import create_app
from aether.config import Settings
from aether.persist.database import Database

_RULE = {
    "name": "Emergency squawk 7700",
    "severity": "high",
    "subject_types": ["aircraft"],
    "conditions": [{"field": "attributes.squawk", "operator": "equals", "value": "7700"}],
    "channels": ["dashboard", "browser"],
}


def _client(tmp_path: Path, *, persist: bool = True) -> TestClient:
    path = str(tmp_path / "alerts.db")
    if persist:
        db = Database(path)  # pre-migrate so the alert_rules table exists for writes
        db.open()
        db.close()
    settings = Settings(demo_source=False, persist=persist, db_path=path)
    return TestClient(create_app(settings=settings))  # no `with` → lifespan not run


def test_create_lists_and_returns_namespaced_id(tmp_path: Path) -> None:
    client = _client(tmp_path)
    created = client.post("/api/v2/alert-rules", json=_RULE)
    assert created.status_code == 201
    body = created.json()
    rid = body["id"]
    assert rid.startswith("rule-")
    assert body["enabled"] is True  # operator-created defaults on

    listing = client.get("/api/v2/alert-rules").json()
    assert listing["count"] == 1
    assert listing["alert_rules"][0]["id"] == rid


def test_get_one_and_404(tmp_path: Path) -> None:
    client = _client(tmp_path)
    rid = client.post("/api/v2/alert-rules", json=_RULE).json()["id"]
    assert client.get(f"/api/v2/alert-rules/{rid}").status_code == 200
    assert client.get("/api/v2/alert-rules/nope").status_code == 404


def test_patch_updates_fields(tmp_path: Path) -> None:
    client = _client(tmp_path)
    rid = client.post("/api/v2/alert-rules", json=_RULE).json()["id"]
    resp = client.patch(f"/api/v2/alert-rules/{rid}", json={"enabled": False, "severity": "low"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["enabled"] is False
    assert body["severity"] == "low"
    assert client.get(f"/api/v2/alert-rules/{rid}").json()["severity"] == "low"


def test_patch_404_for_unknown(tmp_path: Path) -> None:
    client = _client(tmp_path)
    assert client.patch("/api/v2/alert-rules/nope", json={"enabled": False}).status_code == 404


def test_delete_removes_rule(tmp_path: Path) -> None:
    client = _client(tmp_path)
    rid = client.post("/api/v2/alert-rules", json=_RULE).json()["id"]
    assert client.delete(f"/api/v2/alert-rules/{rid}").status_code == 204
    assert client.get(f"/api/v2/alert-rules/{rid}").status_code == 404


def test_delete_404_for_unknown(tmp_path: Path) -> None:
    client = _client(tmp_path)
    assert client.delete("/api/v2/alert-rules/nope").status_code == 404


def test_503_when_persistence_disabled(tmp_path: Path) -> None:
    client = _client(tmp_path, persist=False)
    assert client.get("/api/v2/alert-rules").status_code == 503
    assert client.post("/api/v2/alert-rules", json=_RULE).status_code == 503
    assert client.delete("/api/v2/alert-rules/whatever").status_code == 503


def test_create_422_on_bad_condition(tmp_path: Path) -> None:
    client = _client(tmp_path)
    # operator "in" needs a list value, not a scalar — rejected at validation.
    bad_cond = {"field": "attributes.squawk", "operator": "in", "value": "7700"}
    bad = {**_RULE, "conditions": [bad_cond]}
    assert client.post("/api/v2/alert-rules", json=bad).status_code == 422


def test_create_422_on_unknown_severity(tmp_path: Path) -> None:
    client = _client(tmp_path)
    bad = {**_RULE, "severity": "catastrophic"}
    assert client.post("/api/v2/alert-rules", json=bad).status_code == 422

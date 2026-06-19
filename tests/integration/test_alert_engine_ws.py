"""End-to-end: the alert engine fires from live demo data, then ack closes it (M4.6b).

Exercises the full wired path against a real broker (PRD §6 no-hardware gate): the
lifespan seeds the disabled §12 templates, loads them into the engine, and runs the
demo publisher. ``demo03`` is a locally-received, provider-classified *military*
aircraft (PRD §31.4), so once the "Locally received military aircraft" template is
enabled via the CRUD API, the engine should raise an alert into live state — visible
on ``/api/state`` — which we then acknowledge through the lifecycle endpoint. Skips
when no broker is reachable (see conftest); CI starts Mosquitto so it runs there.
"""

from __future__ import annotations

import dataclasses
import time
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from aether.backend.main import create_app
from aether.config import Settings

_MILITARY_RULE = "rule-aircraft-military-local"


def _app(settings: Settings, db_path: str) -> TestClient:
    cfg = dataclasses.replace(settings, demo_source=True, persist=True, db_path=db_path)
    return TestClient(create_app(settings=cfg, demo_interval_s=0.05))


def _alerts_for_rule(client: TestClient, rule_id: str) -> list[dict[str, Any]]:
    state = client.get("/api/state").json()
    return [a for a in state.get("alerts", []) if a["rule_id"] == rule_id]


def _wait_for_alert(client: TestClient, rule_id: str, *, timeout_s: float = 10.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        alerts = _alerts_for_rule(client, rule_id)
        if alerts:
            return alerts[0]
        time.sleep(0.1)
    raise AssertionError(f"no alert for {rule_id!r} within {timeout_s}s")


def test_engine_fires_military_alert_and_ack_closes_it(
    broker_settings: Settings, tmp_path: Path
) -> None:
    db_path = str(tmp_path / "engine.db")
    with _app(broker_settings, db_path) as client:
        # The template ships disabled; enabling it syncs the engine immediately.
        patched = client.patch(f"/api/v2/alert-rules/{_MILITARY_RULE}", json={"enabled": True})
        assert patched.status_code == 200 and patched.json()["enabled"] is True

        alert = _wait_for_alert(client, _MILITARY_RULE)
        assert alert["state"] == "open"
        assert alert["severity"] == "medium"
        assert alert["subject_id"]  # attributed to the demo03 aircraft

        acked = client.post(f"/api/v2/alerts/{alert['id']}/acknowledge")
        assert acked.status_code == 200
        assert acked.json()["state"] == "acknowledged"

        # The acknowledgement is reflected in live state.
        live = next(a for a in _alerts_for_rule(client, _MILITARY_RULE) if a["id"] == alert["id"])
        assert live["state"] == "acknowledged"


def test_geofence_enter_rule_fires_over_demo_stream(
    broker_settings: Settings, tmp_path: Path
) -> None:
    """A created geofence + entered_geofence rule fires on a demo track (M4.6c).

    Proves the geofence→engine sync path end to end: ``POST /api/v2/geofences``
    syncs the engine's contextual mirror, ``POST /api/v2/alert-rules`` registers an
    ``entered_geofence`` rule referencing it, and the demo's ``demo03`` orbit
    (centered (-95.4, 40.5)) is contained by a generous circle there — so containment
    math fires an open alert into live state with no hardware.
    """
    db_path = str(tmp_path / "geofence-enter.db")
    with _app(broker_settings, db_path) as client:
        gf = client.post(
            "/api/v2/geofences",
            json={
                "name": "demo03 ring",
                "shape": {
                    "kind": "circle",
                    "center": [-95.4, 40.5],
                    "radius_m": 50000.0,  # ~50 km covers the demo03 orbit
                },
            },
        )
        assert gf.status_code == 201
        geofence_id = gf.json()["id"]

        rule = client.post(
            "/api/v2/alert-rules",
            json={
                "name": "Aircraft entered demo03 ring",
                "severity": "medium",
                "subject_types": ["aircraft"],
                "conditions": [{"field": "geometry", "operator": "entered_geofence"}],
                "geofence_id": geofence_id,
                "transition": "enter",
                "enabled": True,
                "channels": ["dashboard"],
            },
        )
        assert rule.status_code == 201
        rule_id = rule.json()["id"]

        alert = _wait_for_alert(client, rule_id)
        assert alert["state"] == "open"
        assert alert["subject_id"]  # attributed to a demo aircraft inside the fence

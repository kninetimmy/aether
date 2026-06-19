"""End-to-end: the notification dispatcher settles a fired alert's delivery_status.

Exercises the full wired path against a real broker (PRD §6 no-hardware gate): the
engine fires an alert into live state, then the :class:`~aether.alerts.notify.
NotificationDispatcher` — registered as a hub observer with its ``run`` task started
by the lifespan — resolves the per-channel ``delivery_status`` off the hot path and
re-publishes the alert, so ``/api/state`` reflects the settled channels.

``demo03`` is a locally-received, provider-classified *military* aircraft
(PRD §31.4): a created rule matching it with a ``browser`` channel fires, and the
dispatcher resolves that channel — ``delivered`` when it clears the browser severity
threshold, ``suppressed`` when configured above it. Skips when no broker is reachable
(see conftest); CI starts Mosquitto so it runs there.
"""

from __future__ import annotations

import dataclasses
import time
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from aether.backend.main import create_app
from aether.config import Settings

_MILITARY_BROWSER_RULE = {
    "name": "Military aircraft (browser)",
    "severity": "medium",
    "subject_types": ["aircraft"],
    "conditions": [
        {"field": "classification.military", "operator": "equals", "value": True},
        {"field": "locally_received", "operator": "equals", "value": True},
    ],
    "transition": "enter",
    "enabled": True,
    "channels": ["dashboard", "browser"],
}


def _app(settings: Settings, db_path: str) -> TestClient:
    cfg = dataclasses.replace(settings, demo_source=True, persist=True, db_path=db_path)
    return TestClient(create_app(settings=cfg, demo_interval_s=0.05))


def _alerts_for_rule(client: TestClient, rule_id: str) -> list[dict[str, Any]]:
    state = client.get("/api/state").json()
    return [a for a in state.get("alerts", []) if a["rule_id"] == rule_id]


def _wait_for_settled(
    client: TestClient, rule_id: str, channel: str, *, timeout_s: float = 10.0
) -> dict[str, Any]:
    """Wait until an alert for ``rule_id`` exists and ``channel`` is no longer pending."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        for alert in _alerts_for_rule(client, rule_id):
            if alert["delivery_status"].get(channel, "pending") != "pending":
                return alert
        time.sleep(0.1)
    raise AssertionError(f"no settled {channel!r} alert for {rule_id!r} within {timeout_s}s")


def test_browser_channel_delivered_end_to_end(broker_settings: Settings, tmp_path: Path) -> None:
    # Default browser threshold is "info", so a medium alert clears it → delivered.
    with _app(broker_settings, str(tmp_path / "notify-deliver.db")) as client:
        created = client.post("/api/v2/alert-rules", json=_MILITARY_BROWSER_RULE)
        assert created.status_code == 201
        rule_id = created.json()["id"]

        alert = _wait_for_settled(client, rule_id, "browser")
        assert alert["state"] == "open"
        # dashboard is pre-delivered by the engine; the dispatcher delivers browser.
        assert alert["delivery_status"] == {"dashboard": "delivered", "browser": "delivered"}


def test_browser_channel_suppressed_below_threshold_end_to_end(
    broker_settings: Settings, tmp_path: Path
) -> None:
    # Raise the browser threshold above the rule's "medium" severity → suppressed.
    settings = dataclasses.replace(broker_settings, notify_browser_min_severity="critical")
    with _app(settings, str(tmp_path / "notify-suppress.db")) as client:
        created = client.post("/api/v2/alert-rules", json=_MILITARY_BROWSER_RULE)
        assert created.status_code == 201
        rule_id = created.json()["id"]

        alert = _wait_for_settled(client, rule_id, "browser")
        assert alert["delivery_status"] == {"dashboard": "delivered", "browser": "suppressed"}

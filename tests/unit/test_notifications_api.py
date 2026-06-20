"""Test-notification endpoint (M4.7b, PRD §21.4, §20.5).

``POST /api/v2/notifications/test`` fires a synthetic alert through the dispatcher's
real channel-resolution path. These tests mount the router on a bare app with a
dispatcher wired to fake drivers (no I/O), asserting: per-channel outcomes, that the
synthetic alert never publishes (isolation from live state), credential-free target
labels, threshold suppression short-circuiting a driver, and request validation. A
final hermetic ``create_app`` smoke confirms the route is wired and unconfigured
server channels degrade to ``unconfigured`` rather than crashing.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import FastAPI
from fastapi.testclient import TestClient

from aether.alerts.notify import (
    ChannelThresholds,
    DiscordDriver,
    NotificationDispatcher,
    NotificationDriver,
)
from aether.backend.main import create_app
from aether.backend.notifications_api import build_notifications_router
from aether.config import Settings
from aether.schema.records import AlertRecord

T0 = datetime(2026, 6, 19, 12, 0, 0, tzinfo=UTC)
_WEBHOOK = "https://discord.com/api/webhooks/123/SECRET-token-xyz"
_TOKEN = "SECRET-token-xyz"


class _RecordingDriver(NotificationDriver):
    """A server-driver double: records calls, returns a fixed result, no I/O."""

    def __init__(self, *, ok: bool) -> None:
        self.ok = ok
        self.calls: list[AlertRecord] = []

    async def deliver(self, alert: AlertRecord) -> bool:
        self.calls.append(alert)
        return self.ok

    @property
    def target(self) -> str:
        return "recording-driver"


class _PublishSink:
    def __init__(self) -> None:
        self.published: list[AlertRecord] = []

    def __call__(self, alert: AlertRecord) -> None:
        self.published.append(alert)


def _client(
    *,
    thresholds: ChannelThresholds | None = None,
    drivers: dict[str, NotificationDriver] | None = None,
) -> tuple[TestClient, _PublishSink]:
    sink = _PublishSink()
    dispatcher = NotificationDispatcher(
        publish=sink,
        clock=lambda: T0,
        thresholds=thresholds,
        drivers=drivers,
    )
    app = FastAPI()
    app.include_router(build_notifications_router(dispatcher, clock=lambda: T0))
    return TestClient(app), sink


def test_resolves_each_channel_and_never_publishes() -> None:
    client, sink = _client(drivers={"email": _RecordingDriver(ok=True)})
    resp = client.post(
        "/api/v2/notifications/test",
        json={"channels": ["browser", "email", "discord"], "severity": "high"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["severity"] == "high"
    assert body["alert_id"].startswith("test-")
    assert body["channels"] == {
        "browser": "delivered",  # client transport, meets threshold
        "email": "delivered",  # recording driver returned ok
        "discord": "unconfigured",  # no driver wired
    }
    # Isolation from live state (PRD §5): the synthetic alert is never published.
    assert sink.published == []


def test_targets_are_credential_free() -> None:
    client, _ = _client(
        thresholds=ChannelThresholds(discord="critical"),  # keep the driver from firing
        drivers={"discord": DiscordDriver(_WEBHOOK)},
    )
    resp = client.post(
        "/api/v2/notifications/test",
        json={"channels": ["discord"], "severity": "low"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["channels"]["discord"] == "suppressed"  # below the critical threshold
    assert _TOKEN not in resp.text  # webhook token never echoed
    assert "discord.com" in body["targets"]["discord"]  # redacted host shown


def test_threshold_suppresses_below_and_skips_driver() -> None:
    driver = _RecordingDriver(ok=True)
    client, _ = _client(
        thresholds=ChannelThresholds(email="critical"),
        drivers={"email": driver},
    )
    resp = client.post(
        "/api/v2/notifications/test",
        json={"channels": ["email"], "severity": "medium"},
    )
    assert resp.json()["channels"]["email"] == "suppressed"
    assert driver.calls == []  # threshold short-circuits before the driver


def test_default_channels_and_dedup() -> None:
    client, _ = _client()
    # Default body → browser/email/discord; a repeated channel is settled once.
    resp = client.post("/api/v2/notifications/test", json={"channels": ["browser", "browser"]})
    assert resp.status_code == 200
    assert resp.json()["channels"] == {"browser": "delivered"}


def test_unknown_channel_rejected() -> None:
    client, _ = _client()
    resp = client.post("/api/v2/notifications/test", json={"channels": ["pager"]})
    assert resp.status_code == 422


def test_empty_channels_rejected() -> None:
    client, _ = _client()
    resp = client.post("/api/v2/notifications/test", json={"channels": []})
    assert resp.status_code == 422


# == wiring smoke (hermetic create_app, no broker, no creds) =================


def test_create_app_wires_route_and_degrades_unconfigured() -> None:
    # No SMTP/webhook creds → server channels have no driver. The endpoint must still
    # answer (degrade visibly), and firing it must not enter any alert into live state.
    app = create_app(settings=Settings(demo_source=False))
    client = TestClient(app)  # no `with` → lifespan/broker never starts
    resp = client.post(
        "/api/v2/notifications/test",
        json={"channels": ["browser", "email", "discord"]},
    )
    assert resp.status_code == 200
    assert resp.json()["channels"] == {
        "browser": "delivered",
        "email": "unconfigured",
        "discord": "unconfigured",
    }
    # Live state is untouched — no test alert leaked into /api/state.
    assert client.get("/api/state").json()["alerts"] == []

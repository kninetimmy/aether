"""Shared helpers for broker-dependent integration tests.

These exercise the real MQTT path (PRD §6 no-hardware gate, §31.3 flow #1). They
need a reachable broker — ``docker compose up -d`` locally, or the Mosquitto step
in CI. When none is reachable the tests skip rather than fail, so the unit suite
still runs everywhere (e.g. dev boxes without Docker).
"""

import dataclasses
import socket

import pytest

from aether.config import Settings


def broker_reachable(settings: Settings, *, timeout_s: float = 0.5) -> bool:
    try:
        with socket.create_connection((settings.mqtt_host, settings.mqtt_port), timeout_s):
            return True
    except OSError:
        return False


@pytest.fixture
def broker_settings() -> Settings:
    """Demo-off settings pointed at the configured broker, or skip if unreachable.

    Security hardening (M7.1) is turned OFF here: these tests drive the app through
    Starlette's ``TestClient``, whose synthetic ``Host: testserver`` is not a
    representative browser request, so the Host/Origin guard — which ``from_env``
    enables by default — would 403 the data path we're actually exercising. The
    guard is unit-tested in isolation in :mod:`tests.unit.test_security`.
    """
    settings = dataclasses.replace(Settings.from_env(), security_enabled=False)
    if not broker_reachable(settings):
        pytest.skip(f"no MQTT broker at {settings.mqtt_host}:{settings.mqtt_port}")
    return settings

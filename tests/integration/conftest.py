"""Shared helpers for broker-dependent integration tests.

These exercise the real MQTT path (PRD §6 no-hardware gate, §31.3 flow #1). They
need a reachable broker — ``docker compose up -d`` locally, or the Mosquitto step
in CI. When none is reachable the tests skip rather than fail, so the unit suite
still runs everywhere (e.g. dev boxes without Docker).
"""

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
    """Demo-off settings pointed at the configured broker, or skip if unreachable."""
    settings = Settings.from_env()
    if not broker_reachable(settings):
        pytest.skip(f"no MQTT broker at {settings.mqtt_host}:{settings.mqtt_port}")
    return settings

"""End-to-end: local ADS-B adapter -> MQTT -> backend -> REST + ws (PRD §31.3).

The no-hardware gate for M2.1: the file poller reads a static ``aircraft.json``
fixture, normalizes to tracks, publishes onto the bus, and the backend streams
them to a websocket client tagged as locally received. Skips when no broker is
reachable (see conftest); CI starts Mosquitto so it runs there.
"""

import dataclasses
from pathlib import Path

from fastapi.testclient import TestClient

from aether.backend.main import create_app
from aether.config import Settings

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "readsb" / "aircraft.json"


def _app(settings: Settings) -> TestClient:
    # Demo off, local ADS-B on, pointed at the fixture file. throttle=0 so every
    # fast poll re-publishes, giving the ws client a steady stream to assert on.
    cfg = dataclasses.replace(
        settings,
        demo_source=False,
        local_adsb=True,
        local_adsb_source=str(FIXTURE),
        local_adsb_poll_s=0.05,
        local_adsb_throttle_s=0.0,
    )
    return TestClient(create_app(settings=cfg))


def test_local_adsb_streams_locally_received_tracks(broker_settings: Settings) -> None:
    with _app(broker_settings) as client, client.websocket_connect("/ws/v2") as ws:
        ws.receive_json()  # snapshot
        track = None
        for _ in range(80):
            msg = ws.receive_json()
            if msg["type"] == "track_upsert" and msg["record"]["source"] == "local_adsb":
                track = msg["record"]
                break
        assert track is not None
        assert track["id"].startswith("aircraft:icao:")
        assert track["locally_received"] is True
        assert track["provenance"][0]["local_rf"] is True


def test_local_adsb_publishes_source_status(broker_settings: Settings) -> None:
    with _app(broker_settings) as client, client.websocket_connect("/ws/v2") as ws:
        ws.receive_json()  # snapshot
        connected = None
        for _ in range(80):
            msg = ws.receive_json()
            if msg["type"] == "source_status" and msg["record"]["source"] == "local_adsb":
                if msg["record"]["status"] == "connected":
                    connected = msg["record"]
                    break
        assert connected is not None
        assert connected["attributes"]["aircraft_visible"] >= 1

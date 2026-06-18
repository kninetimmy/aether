"""End-to-end: local ADS-B adapter -> MQTT -> backend -> REST + ws (PRD §31.3).

The no-hardware gate for M2.1: the file poller reads a static ``aircraft.json``
fixture, normalizes to tracks, publishes onto the bus, and the backend streams
them to a websocket client tagged as locally received. Skips when no broker is
reachable (see conftest); CI starts Mosquitto so it runs there.
"""

import dataclasses
import json
import time
from pathlib import Path

from fastapi.testclient import TestClient

from aether.backend.main import create_app
from aether.config import Settings

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "readsb" / "aircraft.json"
EMERGENCY_FIXTURE = (
    Path(__file__).resolve().parents[1] / "fixtures" / "readsb" / "aircraft_emergency.json"
)


def _fresh_fixture(src: Path, tmp_path: Path) -> str:
    """Copy a readsb fixture with its ``now`` rebased to the present.

    The committed fixtures pin ``now`` to a fixed epoch so the parser *unit* tests
    can assert exact message ages. For the live end-to-end path that staleness now
    matters: M3.1 fusion keys freshness on ``observed_at`` (PRD §8.4), so a snapshot
    from days ago would expire immediately and the track would read as not-locally-
    received. Rebasing ``now`` to wall-clock time models a real, currently-heard
    aircraft without touching the shared fixture.
    """
    data = json.loads(src.read_text())
    data["now"] = time.time()
    dest = tmp_path / src.name
    dest.write_text(json.dumps(data))
    return str(dest)


def _app(settings: Settings, *, source: str) -> TestClient:
    # Demo off, local ADS-B on, pointed at a fixture file. throttle=0 so every
    # fast poll re-publishes, giving the ws client a steady stream to assert on.
    cfg = dataclasses.replace(
        settings,
        demo_source=False,
        local_adsb=True,
        local_adsb_source=source,
        local_adsb_poll_s=0.05,
        local_adsb_throttle_s=0.0,
    )
    return TestClient(create_app(settings=cfg))


def test_local_adsb_streams_locally_received_tracks(
    broker_settings: Settings, tmp_path: Path
) -> None:
    source = _fresh_fixture(FIXTURE, tmp_path)
    with _app(broker_settings, source=source) as client, client.websocket_connect("/ws/v2") as ws:
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


def test_local_adsb_publishes_source_status(broker_settings: Settings, tmp_path: Path) -> None:
    source = _fresh_fixture(FIXTURE, tmp_path)
    with _app(broker_settings, source=source) as client, client.websocket_connect("/ws/v2") as ws:
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


def test_local_adsb_emits_emergency_squawk_event(broker_settings: Settings) -> None:
    # The §32 M2 emergency-squawk template, end to end: an aircraft squawking 7700
    # on first sighting is an onset edge, so the adapter emits a critical event
    # that reaches the ws client as a discrete `event` frame with local provenance.
    with (
        _app(broker_settings, source=str(EMERGENCY_FIXTURE)) as client,
        client.websocket_connect("/ws/v2") as ws,
    ):
        ws.receive_json()  # snapshot
        event = None
        for _ in range(80):
            msg = ws.receive_json()
            if msg["type"] == "event" and msg["record"]["event_type"] == "emergency_squawk":
                event = msg["record"]
                break
        assert event is not None
        assert event["severity"] == "critical"
        assert event["subject_id"] == "aircraft:icao:e00001"
        assert "7700" in event["summary"]
        assert event["geometry"]["type"] == "Point"
        assert event["provenance"][0]["local_rf"] is True

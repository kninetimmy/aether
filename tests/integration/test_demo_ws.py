"""End-to-end over the real bus: demo publisher -> MQTT -> backend -> REST + ws.

PRD §31.3 flow #1 (fake source -> adapter -> MQTT -> backend -> WebSocket). Skips
when no broker is reachable (see conftest); CI starts Mosquitto so it runs there.
"""

import dataclasses

from fastapi.testclient import TestClient

from aether.backend.main import create_app
from aether.config import Settings

# Wire-frame types the client may receive (PRD §22.3-22.4).
_DELTA_TYPES = {
    "track_upsert",
    "feature_upsert",
    "event",
    "alert_upsert",
    "source_status",
    "remove",
}


def _app(settings: Settings) -> TestClient:
    # Fast demo ticks; the bus path is otherwise identical to production.
    fast = dataclasses.replace(settings, demo_source=True)
    return TestClient(create_app(settings=fast, demo_interval_s=0.05))


def test_health_and_state(broker_settings: Settings) -> None:
    with _app(broker_settings) as client:
        health = client.get("/api/health").json()
        assert health["status"] == "ok"

        state = client.get("/api/state").json()
        assert state["type"] == "snapshot"
        for bucket in ("tracks", "features", "events", "alerts", "source_status"):
            assert bucket in state


def test_ws_streams_snapshot_then_sequential_deltas(broker_settings: Settings) -> None:
    with _app(broker_settings) as client, client.websocket_connect("/ws/v2") as ws:
        snapshot = ws.receive_json()
        assert snapshot["type"] == "snapshot"

        seen_types: set[str] = set()
        last_seq = snapshot["seq"]
        for _ in range(20):
            delta = ws.receive_json()
            assert delta["type"] in _DELTA_TYPES
            assert delta["seq"] > last_seq  # monotonic, no gaps on a fresh client
            last_seq = delta["seq"]
            seen_types.add(delta["type"])

        # The demo moves aircraft every tick, so tracks must arrive over the bus.
        assert "track_upsert" in seen_types


def test_ws_track_delta_carries_provenance_and_locality(broker_settings: Settings) -> None:
    with _app(broker_settings) as client, client.websocket_connect("/ws/v2") as ws:
        ws.receive_json()  # snapshot
        track = None
        for _ in range(40):
            msg = ws.receive_json()
            if msg["type"] == "track_upsert":
                track = msg["record"]
                break
        assert track is not None
        assert track["kind"] == "track"
        assert "locally_received" in track
        assert track["geometry"]["type"] == "Point"

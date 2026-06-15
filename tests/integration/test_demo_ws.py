"""End-to-end: demo source -> live state -> REST + websocket (M1.2b)."""

from fastapi.testclient import TestClient

from aether.backend.main import create_app

# Wire-frame types the client may receive (PRD §22.3-22.4).
_DELTA_TYPES = {
    "track_upsert",
    "feature_upsert",
    "event",
    "alert_upsert",
    "source_status",
    "remove",
}


def test_health_and_state() -> None:
    app = create_app(demo_interval_s=0.02)
    with TestClient(app) as client:
        health = client.get("/api/health").json()
        assert health["status"] == "ok"

        state = client.get("/api/state").json()
        assert state["type"] == "snapshot"
        for bucket in ("tracks", "features", "events", "alerts", "source_status"):
            assert bucket in state


def test_ws_streams_snapshot_then_sequential_deltas() -> None:
    app = create_app(demo_interval_s=0.02)
    with TestClient(app) as client, client.websocket_connect("/ws/v2") as ws:
        snapshot = ws.receive_json()
        assert snapshot["type"] == "snapshot"

        seen_types: set[str] = set()
        last_seq = snapshot["seq"]
        for _ in range(10):
            delta = ws.receive_json()
            assert delta["type"] in _DELTA_TYPES
            assert delta["seq"] > last_seq  # monotonic, no gaps on a fresh client
            last_seq = delta["seq"]
            seen_types.add(delta["type"])

        # The demo moves aircraft every tick, so tracks must show up.
        assert "track_upsert" in seen_types


def test_ws_track_delta_carries_provenance_and_locality() -> None:
    app = create_app(demo_interval_s=0.02)
    with TestClient(app) as client, client.websocket_connect("/ws/v2") as ws:
        ws.receive_json()  # snapshot
        track = None
        for _ in range(20):
            msg = ws.receive_json()
            if msg["type"] == "track_upsert":
                track = msg["record"]
                break
        assert track is not None
        assert track["kind"] == "track"
        assert "locally_received" in track
        assert track["geometry"]["type"] == "Point"

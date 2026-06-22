"""End-to-end: FAA NOTAMs -> MQTT -> live state -> ws (PRD §31.3, §32 M6.4).

The M6.4 path with no network and no credentials: the adapter runs the in-process fake
FAA NOTAM provider, whose canned roster places NOTAMs relative to the AOI center. The
backend must carry NOTAMs with FAA-supplied geometry through the bus into live state and
out over /ws/v2 as ``feature_upsert`` frames — with FAA attribution and the honest "not a
flight-planning product" caveat — while a NOTAM with no usable geometry surfaces as a
textual event (the facility panel, AIRSPACE-FR-005) rather than an invented polygon, and a
cancelled NOTAM never appears.

Skips when no broker is reachable (see conftest); CI starts Mosquitto so it runs.
"""

import dataclasses

from fastapi.testclient import TestClient

from aether.backend.main import create_app
from aether.config import Settings

CENTER_LAT, CENTER_LON = 38.5, -122.5


def _app(settings: Settings) -> TestClient:
    # Demo off; only the FAA NOTAM adapter on, running the fake provider at a fast poll.
    # ``base_url=fake`` selects the no-hardware feeder, so no credentials are needed.
    cfg = dataclasses.replace(
        settings,
        demo_source=False,
        faa_notam=True,
        faa_notam_base_url="fake",
        faa_notam_center_lat=CENTER_LAT,
        faa_notam_center_lon=CENTER_LON,
        faa_notam_poll_s=0.05,
    )
    return TestClient(create_app(settings=cfg))


def test_notams_reach_the_ws_with_geometry_text_and_attribution(broker_settings: Settings) -> None:
    with _app(broker_settings) as client, client.websocket_connect("/ws/v2") as ws:
        ws.receive_json()  # snapshot
        notam = None
        for _ in range(400):
            msg = ws.receive_json()
            if msg["type"] == "feature_upsert" and msg["record"]["id"] == "notam:faa:NOTAM_FAKE_1":
                notam = msg["record"]
                break
        assert notam is not None, "geometry-bearing NOTAM never arrived over the ws"

        assert notam["feature_type"] == "notam_geometry"
        assert notam["geometry"]["type"] == "Polygon"
        assert "flight-planning" in notam["attributes"]["caveat"]
        assert notam["attributes"]["attribution"] == "FAA NOTAMs (external-api.faa.gov)"
        assert notam["attributes"]["text"]  # original NOTAM text retained (AIRSPACE-FR-006)
        assert notam["valid_from"] is not None and notam["valid_until"] is not None
        # Network-only feed: no local-RF leg.
        assert notam["provenance"][0]["local_rf"] is False

        state = client.get("/api/state").json()

        # Geometry-bearing NOTAMs are features; the cancelled NOTAM never appears.
        feature_ids = {f["id"] for f in state["features"] if f["feature_type"] == "notam_geometry"}
        assert "notam:faa:NOTAM_FAKE_1" in feature_ids  # single-area polygon
        assert "notam:faa:NOTAM_FAKE_2" in feature_ids  # two-area multipolygon
        assert "notam:faa:NOTAM_FAKE_5" not in feature_ids  # cancelled → dropped

        # The null-geometry NOTAM is a textual facility-panel event, not an invented shape.
        events = {e["id"]: e for e in state["events"]}
        textual = events.get("event:notam_text:NOTAM_FAKE_3")
        assert textual is not None, "null-geometry NOTAM should surface as a textual event"
        assert textual["event_type"] == "notam_textual"
        assert "RWY 12/30 CLSD" in textual["message"]
        assert "flight-planning" in textual["message"].lower()

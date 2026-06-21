"""End-to-end: NASA FIRMS detections -> MQTT -> live state -> ws (PRD §31.3, §32 M5).

The M5.3 path with no network and no map key: the adapter runs the in-process fake FIRMS
provider, whose canned roster places detections relative to the AOI center. The backend
must carry the in-AOI fire detections through the bus into live state and out over /ws/v2
as ``feature_upsert`` frames — with NASA FIRMS attribution and the not-a-confirmed-wildfire
caveat — while the far-away detection never appears (AOI filtering end to end).

Skips when no broker is reachable (see conftest); CI starts Mosquitto so it runs.
"""

import dataclasses

from fastapi.testclient import TestClient

from aether.backend.main import create_app
from aether.config import Settings

CENTER_LAT, CENTER_LON = 38.5, -122.5


def _app(settings: Settings) -> TestClient:
    # Demo off; only the FIRMS adapter on, running the fake provider at a fast poll.
    cfg = dataclasses.replace(
        settings,
        demo_source=False,
        firms=True,
        firms_api_base="fake",  # no-hardware feeder, no map key needed
        firms_center_lat=CENTER_LAT,
        firms_center_lon=CENTER_LON,
        firms_radius_nm=500.0,
        firms_min_confidence="",
        firms_poll_s=0.05,
    )
    return TestClient(create_app(settings=cfg))


def test_in_aoi_fire_detections_reach_the_ws_with_attribution(broker_settings: Settings) -> None:
    with _app(broker_settings) as client, client.websocket_connect("/ws/v2") as ws:
        ws.receive_json()  # snapshot
        fire = None
        for _ in range(400):
            msg = ws.receive_json()
            if (
                msg["type"] == "feature_upsert"
                and msg["record"]["feature_type"] == "fire_detection"
                and msg["record"]["attributes"].get("frp_mw") == 45.0  # the center hotspot
            ):
                fire = msg["record"]
                break
        assert fire is not None, "in-AOI fire detection never arrived over the ws"

        assert fire["attributes"]["confidence_class"] == "high"
        assert fire["attributes"]["attribution"] == "NASA FIRMS (LANCE/EOSDIS)"
        assert "not a confirmed wildfire" in fire["attributes"]["caveat"].lower()
        assert fire["severity"] is None  # thermal detection, not a graded hazard
        assert fire["provenance"][0]["local_rf"] is False  # network-only environmental feed

        # Live state holds the in-AOI detections; the ~15° away detection is filtered out.
        features = client.get("/api/state").json()["features"]
        fires = [f for f in features if f["feature_type"] == "fire_detection"]
        assert len(fires) == 3  # center (high) + short-hop (nominal) + low-conf at center
        for f in fires:
            lon, lat = f["geometry"]["coordinates"]
            assert abs(lat - CENTER_LAT) < 5.0 and abs(lon - CENTER_LON) < 5.0

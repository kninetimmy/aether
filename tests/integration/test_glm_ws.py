"""End-to-end: GLM lightning flashes -> MQTT -> live state -> ws (PRD §31.3, §32 M5).

The M5.6 path with no network and no ``netCDF4`` dependency: the adapter runs the in-process
fake GLM provider, whose canned roster places flashes relative to the AOI center. The backend
must carry the in-AOI flashes through the bus into live state and out over /ws/v2 as
``feature_upsert`` frames — with NOAA attribution and the not-a-confirmed-strike caveat — while
the far-away flash never appears (AOI filtering end to end).

Skips when no broker is reachable (see conftest); CI starts Mosquitto so it runs.
"""

import dataclasses

from fastapi.testclient import TestClient

from aether.backend.main import create_app
from aether.config import Settings

CENTER_LAT, CENTER_LON = 38.5, -122.5


def _app(settings: Settings) -> TestClient:
    # Demo off; only the GLM adapter on, running the fake provider at a fast poll.
    cfg = dataclasses.replace(
        settings,
        demo_source=False,
        glm=True,
        glm_satellite="fake",  # no-hardware feeder, no netCDF4 needed
        glm_center_lat=CENTER_LAT,
        glm_center_lon=CENTER_LON,
        glm_radius_nm=500.0,
        glm_poll_s=0.05,
    )
    return TestClient(create_app(settings=cfg))


def test_in_aoi_flashes_reach_the_ws_with_attribution(broker_settings: Settings) -> None:
    with _app(broker_settings) as client, client.websocket_connect("/ws/v2") as ws:
        ws.receive_json()  # snapshot
        flash = None
        for _ in range(400):
            msg = ws.receive_json()
            rec = msg["record"] if msg["type"] == "feature_upsert" else {}
            if rec.get("feature_type") == "lightning_flash":
                flash = rec
                break
        assert flash is not None, "in-AOI lightning flash never arrived over the ws"

        assert flash["attributes"]["attribution"].startswith("NOAA")
        assert "not a confirmed cloud-to-ground" in flash["attributes"]["caveat"].lower()
        assert flash["severity"] is None  # total-lightning detection, not a graded hazard
        assert flash["valid_until"] is not None  # carries a TTL so it ages off the map
        assert flash["provenance"][0]["local_rf"] is False  # network-only environmental feed

        # Live state holds the in-AOI flashes; the ~15° away flash is filtered out.
        features = client.get("/api/state").json()["features"]
        flashes = [f for f in features if f["feature_type"] == "lightning_flash"]
        assert len(flashes) >= 3  # center + short-hop + degraded-at-center (a window may add more)
        for f in flashes:
            lon, lat = f["geometry"]["coordinates"]
            assert abs(lat - CENTER_LAT) < 5.0 and abs(lon - CENTER_LON) < 5.0

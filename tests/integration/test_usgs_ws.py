"""End-to-end: USGS earthquakes -> MQTT -> live state -> ws (PRD §31.3, §32 M5).

The M5.1 path with no network: the adapter runs the in-process fake USGS provider,
whose canned roster places quakes relative to the AOI center. The backend must carry
the in-AOI earthquakes through the bus into live state and out over /ws/v2 as
``feature_upsert`` frames — with USGS attribution and honest caveats — while the
far-away quake and the quarry blast never appear (AOI + earthquake-only filtering
end to end).

Skips when no broker is reachable (see conftest); CI starts Mosquitto so it runs.
"""

import dataclasses

from fastapi.testclient import TestClient

from aether.backend.main import create_app
from aether.config import Settings

CENTER_LAT, CENTER_LON = 38.5, -122.5


def _app(settings: Settings) -> TestClient:
    # Demo off; only the USGS adapter on, running the fake provider at a fast poll.
    cfg = dataclasses.replace(
        settings,
        demo_source=False,
        usgs=True,
        usgs_feed_url="fake",
        usgs_center_lat=CENTER_LAT,
        usgs_center_lon=CENTER_LON,
        usgs_radius_nm=500.0,
        usgs_min_magnitude=0.0,
        usgs_poll_s=0.05,
    )
    return TestClient(create_app(settings=cfg))


def test_in_aoi_earthquakes_reach_the_ws_with_attribution(broker_settings: Settings) -> None:
    with _app(broker_settings) as client, client.websocket_connect("/ws/v2") as ws:
        ws.receive_json()  # snapshot
        quake = None
        for _ in range(400):
            msg = ws.receive_json()
            if (
                msg["type"] == "feature_upsert"
                and msg["record"]["id"] == "earthquake:usgs:ak_fake_001"
            ):
                quake = msg["record"]
                break
        assert quake is not None, "in-AOI earthquake never arrived over the ws"

        assert quake["feature_type"] == "earthquake"
        assert quake["attributes"]["magnitude"] == 4.6
        assert quake["attributes"]["attribution"] == "USGS Earthquake Hazards Program"
        assert quake["severity"] == "green"  # PAGER alert carried as the honest severity
        # Network-only environmental feed: no local-RF leg.
        assert quake["provenance"][0]["local_rf"] is False

        # Live state holds the in-AOI quakes once; the far quake and quarry blast are
        # filtered out end to end (AOI + earthquake-only).
        features = client.get("/api/state").json()["features"]
        ids = {f["id"] for f in features if f["feature_type"] == "earthquake"}
        assert "earthquake:usgs:ak_fake_001" in ids
        assert "earthquake:usgs:ak_fake_002" in ids
        assert "earthquake:usgs:ak_fake_003" not in ids  # outside the 500 NM AOI
        assert "earthquake:usgs:ak_fake_005" not in ids  # quarry blast, not an earthquake

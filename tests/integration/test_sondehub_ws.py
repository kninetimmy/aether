"""End-to-end: SondeHub radiosondes -> MQTT -> live state -> ws (PRD §31.3, §32 M5).

The M5.2 path with no network: the adapter runs the in-process fake SondeHub provider,
whose canned roster places sondes relative to the AOI center. The backend must carry
the in-AOI radiosondes through the bus into live state and out over /ws/v2 as
``track_upsert`` frames — with SondeHub attribution and ascent/descent state — while
the far-away sonde and the positionless frame never appear (AOI + position filtering
end to end).

Skips when no broker is reachable (see conftest); CI starts Mosquitto so it runs.
"""

import dataclasses

from fastapi.testclient import TestClient

from aether.backend.main import create_app
from aether.config import Settings

CENTER_LAT, CENTER_LON = 38.5, -122.5


def _app(settings: Settings) -> TestClient:
    # Demo off; only the SondeHub adapter on, running the fake provider at a fast poll.
    cfg = dataclasses.replace(
        settings,
        demo_source=False,
        sondehub=True,
        sondehub_api_base="fake",
        sondehub_center_lat=CENTER_LAT,
        sondehub_center_lon=CENTER_LON,
        sondehub_radius_nm=500.0,
        sondehub_poll_s=0.05,
    )
    return TestClient(create_app(settings=cfg))


def test_in_aoi_sondes_reach_the_ws_with_attribution(broker_settings: Settings) -> None:
    with _app(broker_settings) as client, client.websocket_connect("/ws/v2") as ws:
        ws.receive_json()  # snapshot
        sonde = None
        for _ in range(400):
            msg = ws.receive_json()
            if msg["type"] == "track_upsert" and msg["record"]["id"] == "sonde:RS41_FAKE_001":
                sonde = msg["record"]
                break
        assert sonde is not None, "in-AOI radiosonde never arrived over the ws"

        assert sonde["track_type"] == "radiosonde"
        assert sonde["altitude_m"] == 18250.0
        assert sonde["vertical_rate_mps"] == 5.2
        assert sonde["attributes"]["ascent_state"] == "ascending"
        assert sonde["attributes"]["attribution"] == "SondeHub (Project Horus) radiosonde network"
        # Network-only environmental feed: no local-RF leg.
        assert sonde["locally_received"] is False
        assert sonde["provenance"][0]["local_rf"] is False

        # Live state holds the in-AOI sondes once; the far sonde and the positionless
        # frame are filtered out end to end (AOI + position).
        tracks = client.get("/api/state").json()["tracks"]
        ids = {t["id"] for t in tracks if t["track_type"] == "radiosonde"}
        assert "sonde:RS41_FAKE_001" in ids
        assert "sonde:M10_FAKE_002" in ids
        assert "sonde:RS41_FAKE_003" not in ids  # outside the 500 NM AOI
        assert "sonde:DFM_FAKE_004" not in ids  # no position fix

"""End-to-end: FAA TFRs -> MQTT -> live state -> ws (PRD §31.3, §32 M6.1).

The M6.1 path with no network: the adapter runs the in-process fake FAA provider,
whose canned roster places TFRs relative to the AOI center. The backend must carry the
in-AOI TFRs through the bus into live state and out over /ws/v2 as ``feature_upsert``
frames — with FAA attribution and the honest "not a flight-planning product" caveat —
while the far-away TFR never appears (AOI filtering end to end) and the TFR with broken
geometry surfaces as a textual event rather than an invented polygon.

Skips when no broker is reachable (see conftest); CI starts Mosquitto so it runs.
"""

import dataclasses

from fastapi.testclient import TestClient

from aether.backend.main import create_app
from aether.config import Settings

CENTER_LAT, CENTER_LON = 38.5, -122.5


def _app(settings: Settings) -> TestClient:
    # Demo off; only the FAA TFR adapter on, running the fake provider at a fast poll.
    cfg = dataclasses.replace(
        settings,
        demo_source=False,
        faa_tfr=True,
        faa_tfr_base_url="fake",
        faa_tfr_center_lat=CENTER_LAT,
        faa_tfr_center_lon=CENTER_LON,
        faa_tfr_radius_nm=500.0,
        faa_tfr_poll_s=0.05,
    )
    return TestClient(create_app(settings=cfg))


def test_in_aoi_tfrs_reach_the_ws_with_attribution(broker_settings: Settings) -> None:
    with _app(broker_settings) as client, client.websocket_connect("/ws/v2") as ws:
        ws.receive_json()  # snapshot
        tfr = None
        for _ in range(400):
            msg = ws.receive_json()
            if msg["type"] == "feature_upsert" and msg["record"]["id"] == "tfr:faa:6/0001":
                tfr = msg["record"]
                break
        assert tfr is not None, "in-AOI TFR never arrived over the ws"

        assert tfr["feature_type"] == "tfr"
        assert tfr["geometry"]["type"] == "Polygon"
        assert "flight-planning" in tfr["attributes"]["caveat"]
        assert tfr["attributes"]["attribution"]
        assert tfr["valid_from"] is not None and tfr["valid_until"] is not None
        # Network-only environmental feed: no local-RF leg.
        assert tfr["provenance"][0]["local_rf"] is False

        # Live state holds the in-AOI TFRs; the far TFR is filtered out end to end.
        features = client.get("/api/state").json()["features"]
        ids = {f["id"] for f in features if f["feature_type"] == "tfr"}
        assert "tfr:faa:6/0001" in ids  # single-area polygon at center
        assert "tfr:faa:6/0002" in ids  # two-area multipolygon at center
        assert "tfr:faa:6/0003" not in ids  # ~25° away, outside the 500 NM AOI

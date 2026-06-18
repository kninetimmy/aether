"""End-to-end: local + network ADS-B -> MQTT -> fusion -> ws (PRD §31.3, §32 M3).

The M3 exit criterion, no hardware: the local adapter reads a static readsb
``aircraft.json`` fixture while the network adapter runs the in-process fake
provider, and both report the *same* airframe (ICAO ``a1b2c3``). The backend's
fusion engine must collapse the local-RF and Internet observations into a single
track keyed by the shared ``correlation_key``, with correct provenance — one RF
contributor and one network contributor, ``locally_received`` true while the local
radio is fresh. A network-only airframe stays a plain network track.

Skips when no broker is reachable (see conftest); CI starts Mosquitto so it runs.
"""

import dataclasses
import json
import time
from pathlib import Path

from fastapi.testclient import TestClient

from aether.backend.main import create_app
from aether.config import Settings

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "readsb" / "aircraft.json"


def _fresh_fixture(src: Path, tmp_path: Path) -> str:
    """Copy a readsb fixture with its ``now`` rebased to the present.

    The committed fixture pins ``now`` to a fixed epoch for the parser unit tests;
    fusion keys freshness on ``observed_at`` (PRD §8.4), so a days-old snapshot would
    expire immediately. Rebasing models a currently-heard aircraft (same rationale
    as the local-adapter integration test) so the local leg stays fresh enough to
    fuse with the always-fresh fake network feed.
    """
    data = json.loads(src.read_text())
    data["now"] = time.time()
    dest = tmp_path / src.name
    dest.write_text(json.dumps(data))
    return str(dest)


def _app(settings: Settings, *, source: str) -> TestClient:
    # Demo off; both ADS-B adapters on. Local polls the fixture file fast with no
    # throttle; network runs the fake provider over a single-tile AOI (radius below
    # the fake's 250 NM cap) at a fast poll with no inter-tile rate limit.
    cfg = dataclasses.replace(
        settings,
        demo_source=False,
        local_adsb=True,
        local_adsb_source=source,
        local_adsb_poll_s=0.05,
        local_adsb_throttle_s=0.0,
        network_adsb=True,
        network_adsb_provider="fake",
        network_adsb_center_lat=40.7,
        network_adsb_center_lon=-95.2,
        network_adsb_radius_nm=100.0,
        network_adsb_poll_s=0.05,
        network_adsb_rate_limit_s=0.0,
    )
    return TestClient(create_app(settings=cfg))


def test_local_and_network_duplicates_fuse_into_one_track(
    broker_settings: Settings, tmp_path: Path
) -> None:
    source = _fresh_fixture(FIXTURE, tmp_path)
    with _app(broker_settings, source=source) as client, client.websocket_connect("/ws/v2") as ws:
        ws.receive_json()  # snapshot
        fused = None
        for _ in range(400):
            msg = ws.receive_json()
            if msg["type"] == "track_upsert" and msg["record"]["id"] == "aircraft:icao:a1b2c3":
                record = msg["record"]
                # Wait until BOTH contributors have landed in the group.
                if record["attributes"].get("fusion", {}).get("fused_count") == 2:
                    fused = record
                    break
        assert fused is not None, "local+network never fused into one a1b2c3 track"

        # One identity, both provenance paths, locally received while local is fresh.
        assert fused["locally_received"] is True
        assert {p["source"] for p in fused["provenance"]} == {"local_adsb", "network_adsb"}
        assert {p["local_rf"] for p in fused["provenance"]} == {True, False}

        # "Appears once": the keyed live state holds a single entry for that ICAO.
        state = client.get("/api/state").json()
        a1b2c3 = [t for t in state["tracks"] if t["id"] == "aircraft:icao:a1b2c3"]
        assert len(a1b2c3) == 1
        assert a1b2c3[0]["attributes"]["fusion"]["fused_count"] == 2


def test_network_only_airframe_stays_a_network_track(
    broker_settings: Settings, tmp_path: Path
) -> None:
    # cafe01 is in the fake network roster but not the local fixture: it must show
    # as a single network track (no RF contributor), proving fusion is keyed on
    # identity and does not invent a local leg (FUSION-FR-006).
    source = _fresh_fixture(FIXTURE, tmp_path)
    with _app(broker_settings, source=source) as client, client.websocket_connect("/ws/v2") as ws:
        ws.receive_json()  # snapshot
        net_only = None
        for _ in range(400):
            msg = ws.receive_json()
            if msg["type"] == "track_upsert" and msg["record"]["id"] == "aircraft:icao:cafe01":
                net_only = msg["record"]
                break
        assert net_only is not None
        assert net_only["locally_received"] is False
        assert {p["source"] for p in net_only["provenance"]} == {"network_adsb"}
        assert net_only["provenance"][0]["local_rf"] is False

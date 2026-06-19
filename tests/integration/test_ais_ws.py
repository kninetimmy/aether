"""End-to-end: AIS (AISStream) -> MQTT -> live state -> ws (PRD §31.3, §32 M3).

No hardware, no API key: a fake AISStream WebSocket server feeds the AIS adapter,
which publishes vessel ``TrackRecord``\\s onto the bus that surface on ``/ws/v2`` and
``/api/state``. AIS is a network-only feed (no local-RF leg), so the properties under
test are: a vessel renders with correct network provenance and appears exactly once
per identity, and the adapter folds AISStream's separate static (name/type/voyage)
and dynamic (position) messages into ONE track by MMSI (PRD §18.5).

The fake runs as a subprocess so its event loop is independent of the TestClient's
(same rationale as ``test_aprs_is_ws.py``). Skips when no broker is reachable (see
conftest); CI starts Mosquitto so it runs there.
"""

import dataclasses
import socket
import subprocess
import sys
import time

from fastapi.testclient import TestClient

from aether.backend.main import create_app
from aether.config import Settings


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _start_fake_server(module: str, port: int) -> subprocess.Popen[bytes]:
    proc = subprocess.Popen(
        [sys.executable, "-m", module, "127.0.0.1", str(port), "0.05"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.25)
            try:
                s.connect(("127.0.0.1", port))
                return proc
            except OSError:
                time.sleep(0.05)
    proc.terminate()
    raise RuntimeError(f"fake server {module} did not start")


def _app(settings: Settings, *, ais_port: int) -> TestClient:
    # Demo off; AIS on against the fake WS server (plain ws, dummy key so the
    # subscription builds). Throttle off so every observation publishes.
    cfg = dataclasses.replace(
        settings,
        demo_source=False,
        ais=True,
        ais_tls=False,
        ais_host="127.0.0.1",
        ais_port=ais_port,
        ais_api_key="demo-key",
        ais_center_lat=38.5,
        ais_center_lon=-74.5,
        ais_throttle_s=0.0,
    )
    return TestClient(create_app(settings=cfg))


def test_ais_vessel_renders_with_network_provenance(broker_settings: Settings) -> None:
    # The position-only vessel (222222222) must show as a single network-only track:
    # no local-RF contributor, labeled by MMSI.
    ais_port = _free_port()
    feeder = _start_fake_server("aether.adapters.ais_fake_feeder", ais_port)
    try:
        with (
            _app(broker_settings, ais_port=ais_port) as client,
            client.websocket_connect("/ws/v2") as ws,
        ):
            ws.receive_json()  # snapshot
            vessel = None
            for _ in range(600):
                msg = ws.receive_json()
                if msg["type"] == "track_upsert" and msg["record"]["id"] == "ais:vessel:222222222":
                    vessel = msg["record"]
                    break
            assert vessel is not None, "AIS vessel never arrived over the websocket"
            assert vessel["track_type"] == "vessel"
            assert vessel["locally_received"] is False
            assert {p["source"] for p in vessel["provenance"]} == {"ais"}
            assert vessel["provenance"][0]["local_rf"] is False
            assert vessel["geometry"]["coordinates"] == [-74.0, 39.0]  # [lon, lat]
    finally:
        feeder.terminate()
        feeder.wait(timeout=5)


def test_ais_static_and_dynamic_fuse_into_one_track(broker_settings: Settings) -> None:
    # 111111111 sends ShipStaticData (name/type) then PositionReport: the merger folds
    # the static into the position, so one track carries both (PRD §18.5).
    ais_port = _free_port()
    feeder = _start_fake_server("aether.adapters.ais_fake_feeder", ais_port)
    try:
        with (
            _app(broker_settings, ais_port=ais_port) as client,
            client.websocket_connect("/ws/v2") as ws,
        ):
            ws.receive_json()  # snapshot
            # Wait until BOTH vessels have arrived before snapshotting state — the
            # second vessel is published just after the first, so checking state the
            # instant the merged track appears would race its ingestion.
            merged = None
            seen_other = False
            for _ in range(600):
                msg = ws.receive_json()
                if msg["type"] != "track_upsert":
                    continue
                record = msg["record"]
                if record["id"] == "ais:vessel:111111111":
                    if record["attributes"].get("vessel_name") and record["geometry"]:
                        merged = record
                elif record["id"] == "ais:vessel:222222222":
                    seen_other = True
                if merged is not None and seen_other:
                    break
            assert merged is not None, "static+dynamic never merged into one vessel track"
            assert seen_other, "the second vessel never arrived"
            assert merged["label"] == "DEMO CARGO"
            assert merged["attributes"]["vessel_name"] == "DEMO CARGO"
            assert merged["attributes"]["ship_type_text"] == "cargo"
            assert merged["attributes"]["destination"] == "PORT DEMO"
            assert merged["geometry"]["coordinates"] == [-74.5, 38.5]

            # "Appears once": the keyed live state holds a single entry per vessel
            # despite the feeder re-sending each on every loop.
            state = client.get("/api/state").json()
            ids = [t["id"] for t in state["tracks"]]
            assert ids.count("ais:vessel:111111111") == 1
            assert ids.count("ais:vessel:222222222") == 1
    finally:
        feeder.terminate()
        feeder.wait(timeout=5)

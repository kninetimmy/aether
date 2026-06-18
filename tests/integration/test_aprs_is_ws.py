"""End-to-end: local APRS + APRS-IS -> MQTT -> fusion -> ws (PRD §31.3, §32 M3).

The APRS twin of ``test_network_adsb_ws.py``, no hardware: a fake KISS server feeds
the local APRS adapter while a fake APRS-IS server feeds the APRS-IS adapter, and
both report the SAME station (``N0CALL`` at 4903.50N/07201.75W). The backend's
fusion engine must collapse the local-RF and Internet observations into a single
``aprs:station:N0CALL`` track keyed by the shared ``correlation_key``, with one RF
contributor and one network contributor and ``locally_received`` true while the
local radio is fresh. An APRS-IS-only station (``W9NET``) stays a network-only
track.

Both fakes run as subprocesses so their event loops are independent of the
TestClient's (same rationale as ``test_local_aprs_ws.py``). Skips when no broker is
reachable (see conftest); CI starts Mosquitto so it runs there.
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


def _app(settings: Settings, *, kiss_port: int, aprs_is_port: int) -> TestClient:
    # Demo off; both APRS adapters on. Local reads the fake KISS server; APRS-IS
    # reads the fake APRS-IS server with a non-empty callsign so the login builds.
    cfg = dataclasses.replace(
        settings,
        demo_source=False,
        local_aprs=True,
        local_aprs_host="127.0.0.1",
        local_aprs_port=kiss_port,
        local_aprs_throttle_s=0.0,
        aprs_is=True,
        aprs_is_host="127.0.0.1",
        aprs_is_port=aprs_is_port,
        aprs_is_callsign="N0CALL",
        aprs_is_throttle_s=0.0,
        aprs_is_stall_s=5.0,
    )
    return TestClient(create_app(settings=cfg))


def test_local_and_aprs_is_duplicates_fuse_into_one_track(broker_settings: Settings) -> None:
    kiss_port, aprs_is_port = _free_port(), _free_port()
    kiss = _start_fake_server("aether.adapters.aprs_fake_feeder", kiss_port)
    aprsis = _start_fake_server("aether.adapters.aprs_is_fake_feeder", aprs_is_port)
    try:
        with (
            _app(broker_settings, kiss_port=kiss_port, aprs_is_port=aprs_is_port) as client,
            client.websocket_connect("/ws/v2") as ws,
        ):
            ws.receive_json()  # snapshot
            fused = None
            for _ in range(600):
                msg = ws.receive_json()
                if msg["type"] == "track_upsert" and msg["record"]["id"] == "aprs:station:N0CALL":
                    record = msg["record"]
                    if record["attributes"].get("fusion", {}).get("fused_count") == 2:
                        fused = record
                        break
            assert fused is not None, "local+APRS-IS never fused into one N0CALL track"

            # One identity, both provenance paths, locally received while local is fresh.
            assert fused["locally_received"] is True
            assert {p["source"] for p in fused["provenance"]} == {"local_aprs", "aprs_is"}
            assert {p["local_rf"] for p in fused["provenance"]} == {True, False}

            # "Appears once": the keyed live state holds a single entry for that station.
            state = client.get("/api/state").json()
            n0call = [t for t in state["tracks"] if t["id"] == "aprs:station:N0CALL"]
            assert len(n0call) == 1
            assert n0call[0]["attributes"]["fusion"]["fused_count"] == 2
    finally:
        kiss.terminate()
        aprsis.terminate()
        kiss.wait(timeout=5)
        aprsis.wait(timeout=5)


def test_aprs_is_only_station_stays_a_network_track(broker_settings: Settings) -> None:
    # W9NET is in the APRS-IS fake roster but has no local counterpart: it must show
    # as a single network-only track (no RF contributor), proving fusion is keyed on
    # identity and does not invent a local leg (FUSION-FR-006).
    kiss_port, aprs_is_port = _free_port(), _free_port()
    kiss = _start_fake_server("aether.adapters.aprs_fake_feeder", kiss_port)
    aprsis = _start_fake_server("aether.adapters.aprs_is_fake_feeder", aprs_is_port)
    try:
        with (
            _app(broker_settings, kiss_port=kiss_port, aprs_is_port=aprs_is_port) as client,
            client.websocket_connect("/ws/v2") as ws,
        ):
            ws.receive_json()  # snapshot
            net_only = None
            for _ in range(600):
                msg = ws.receive_json()
                if msg["type"] == "track_upsert" and msg["record"]["id"] == "aprs:station:W9NET":
                    net_only = msg["record"]
                    break
            assert net_only is not None, "APRS-IS-only W9NET never arrived"
            assert net_only["locally_received"] is False
            assert {p["source"] for p in net_only["provenance"]} == {"aprs_is"}
            assert net_only["provenance"][0]["local_rf"] is False
    finally:
        kiss.terminate()
        aprsis.terminate()
        kiss.wait(timeout=5)
        aprsis.wait(timeout=5)

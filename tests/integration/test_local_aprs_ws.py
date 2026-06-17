"""End-to-end: fake KISS server -> AprsSource -> KISS/AX.25 -> parser -> bus ->
backend -> ws (PRD §31, §34 no-hardware gate for M2.2b).

A subprocess runs the fake KISS server (so its event loop is independent of the
TestClient's), streaming canned AX.25 frames on an ephemeral 127.0.0.1 port. The
adapter reads them, decodes, normalizes to local tracks, publishes onto the bus,
and the backend streams them to a websocket client tagged as locally received.
Skips when no broker is reachable (see conftest); CI starts Mosquitto so it runs
there. The fake server only WRITES frames to the reader; aether only READS —
neither side ever sends a KISS frame toward a TNC for transmission (receive-only).
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


def _start_fake_server(port: int) -> subprocess.Popen[bytes]:
    proc = subprocess.Popen(
        [sys.executable, "-m", "aether.adapters.aprs_fake_feeder", "127.0.0.1", str(port), "0.05"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    # Wait until the server is accepting connections (bounded).
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
    raise RuntimeError("fake KISS server did not start")


def _app(settings: Settings, port: int) -> TestClient:
    cfg = dataclasses.replace(
        settings,
        demo_source=False,
        local_aprs=True,
        local_aprs_host="127.0.0.1",
        local_aprs_port=port,
        local_aprs_throttle_s=0.0,
    )
    return TestClient(create_app(settings=cfg))


def test_local_aprs_streams_locally_received_tracks(broker_settings: Settings) -> None:
    port = _free_port()
    proc = _start_fake_server(port)
    try:
        with _app(broker_settings, port) as client, client.websocket_connect("/ws/v2") as ws:
            ws.receive_json()  # snapshot
            track = None
            for _ in range(120):
                msg = ws.receive_json()
                if msg["type"] == "track_upsert" and msg["record"]["source"] == "local_aprs":
                    track = msg["record"]
                    break
            assert track is not None
            assert track["id"].startswith("aprs:")
            assert track["locally_received"] is True
            assert track["provenance"][0]["local_rf"] is True
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def test_local_aprs_publishes_source_status(broker_settings: Settings) -> None:
    port = _free_port()
    proc = _start_fake_server(port)
    try:
        with _app(broker_settings, port) as client, client.websocket_connect("/ws/v2") as ws:
            ws.receive_json()  # snapshot
            connected = None
            for _ in range(120):
                msg = ws.receive_json()
                if (
                    msg["type"] == "source_status"
                    and msg["record"]["source"] == "local_aprs"
                    and msg["record"]["status"] == "connected"
                ):
                    connected = msg["record"]
                    break
            assert connected is not None
            assert connected["attributes"]["connection"] == "kiss"
    finally:
        proc.terminate()
        proc.wait(timeout=5)

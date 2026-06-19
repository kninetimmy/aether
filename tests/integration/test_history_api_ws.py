"""End-to-end over the real bus: demo → MQTT → live detail + persisted history.

Proves the M4.3 read API on the full path (PRD §21.3/§11.15, §33.6 "selected tracks
show history"): the same demo track is served live by ``GET /api/v2/tracks/{id}``
(from the hub) and its persisted points by ``GET /api/v2/tracks/{id}/history`` (from
the SQLite store the sibling persistence subscriber fills). Skips when no broker is
reachable (see conftest); CI starts Mosquitto so it runs there.
"""

import dataclasses
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
from fastapi.testclient import TestClient

from aether.backend.main import create_app
from aether.config import Settings

# A demo aircraft that is local-only (single source) — clean single-identity history.
_TRACK_ID = "aircraft:icao:demo03"


def _app(settings: Settings, db_path: str) -> TestClient:
    # Demo on (publishes over the bus), persistence on, sampling off so every demo
    # tick is persisted — deterministic history without cadence thinning.
    cfg = dataclasses.replace(
        settings, demo_source=True, persist=True, persist_sample=False, db_path=db_path
    )
    return TestClient(create_app(settings=cfg, demo_interval_s=0.05))


def _poll(
    get: Callable[[], httpx.Response], ok: Callable[[httpx.Response], bool]
) -> httpx.Response:
    """Poll ``get`` until ``ok`` (the demo + persist round-trip takes a moment)."""
    resp = get()
    for _ in range(200):  # up to ~10s
        if ok(resp):
            return resp
        time.sleep(0.05)
        resp = get()
    return resp


def test_selected_track_detail_and_history(broker_settings: Settings, tmp_path: Path) -> None:
    db_path = str(tmp_path / "history.db")
    with _app(broker_settings, db_path) as client:
        # Detail is served live from the hub once the demo track has been ingested.
        detail = _poll(
            lambda: client.get(f"/api/v2/tracks/{_TRACK_ID}"),
            lambda r: r.status_code == 200,
        )
        assert detail.status_code == 200
        body: dict[str, Any] = detail.json()
        assert body["id"] == _TRACK_ID
        assert body["kind"] == "track"
        assert body["locally_received"] is True  # demo03 is local-only

        # History is served from the persistence store the sibling subscriber fills.
        hist = _poll(
            lambda: client.get(f"/api/v2/tracks/{_TRACK_ID}/history"),
            lambda r: r.status_code == 200 and r.json()["count"] >= 1,
        )
        assert hist.status_code == 200
        hbody = hist.json()
        assert hbody["track_id"] == _TRACK_ID
        assert hbody["count"] >= 1
        observed = [p["observed_at"] for p in hbody["points"]]
        assert observed == sorted(observed)  # oldest-first
        assert all(p["source"] == "demo" for p in hbody["points"])


def test_unknown_track_history_is_empty_not_error(
    broker_settings: Settings, tmp_path: Path
) -> None:
    # A live, persisting backend still returns a clean empty history for an id with
    # no stored observations — never a 500 (PRD §37).
    with _app(broker_settings, str(tmp_path / "history.db")) as client:
        resp = client.get("/api/v2/tracks/aircraft:icao:nope/history")
        assert resp.status_code == 200
        assert resp.json()["count"] == 0

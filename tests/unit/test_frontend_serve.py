"""The backend serves the built SPA single-origin (one URL / one process on :8000).

Hermetic like the other API tests: the :class:`TestClient` runs WITHOUT its context
manager, so the app lifespan (MQTT subscriber) never starts. A temp directory stands
in for ``npm run build``; ``AETHER_FRONTEND_DIST`` points the mount at it. This guards
the two load-bearing properties of the single-origin serve: the SPA + its hashed
assets are served, and the static catch-all does NOT shadow the API.
"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from aether.backend.main import create_app
from aether.config import Settings


def _client(monkeypatch: pytest.MonkeyPatch, dist: Path | None) -> TestClient:
    if dist is None:
        monkeypatch.delenv("AETHER_FRONTEND_DIST", raising=False)
    else:
        monkeypatch.setenv("AETHER_FRONTEND_DIST", str(dist))
    # no `with` → lifespan (broker subscriber) never runs; routes still serve.
    return TestClient(create_app(settings=Settings(demo_source=False, persist=False)))


def _build(tmp_path: Path) -> Path:
    """Minimal stand-in for a vite production build."""
    (tmp_path / "index.html").write_text(
        "<!doctype html><title>aether COP</title>", encoding="utf-8"
    )
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "app-abc123.js").write_text("console.log('aether')", encoding="utf-8")
    return tmp_path


def test_serves_spa_and_assets_when_build_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _client(monkeypatch, _build(tmp_path))

    root = client.get("/")
    assert root.status_code == 200
    assert "aether COP" in root.text

    asset = client.get("/assets/app-abc123.js")
    assert asset.status_code == 200
    assert "aether" in asset.text


def test_api_routes_not_shadowed_by_static_mount(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _client(monkeypatch, _build(tmp_path))

    # The catch-all mount is registered AFTER the API routes, so these still win.
    assert client.get("/api/health").json()["status"] == "ok"
    assert client.get("/api/state").status_code == 200


def test_api_only_when_no_build(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Point at an empty dir: no index.html → nothing mounted → app stays API-only.
    client = _client(monkeypatch, tmp_path)

    assert client.get("/api/health").json()["status"] == "ok"
    assert client.get("/").status_code == 404

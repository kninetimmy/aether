"""THE M4 EXIT INVARIANT: replay cannot fire live alerts/notifications (PRD §19.6/§32).

End-to-end against a real broker (PRD §6 no-hardware gate): the lifespan runs the
demo publisher and the sibling persistence writer, an alert rule that WOULD match the
demo data is enabled, and we then run a replay session over the persisted window. The
replay must return reconstructed records but must NOT cause any new alert to enter
live state — because the replay path (``aether.backend.replay_api`` →
``aether.persist.database.read_observations_window`` → ``aether.replay.player``) is
REST + a read-only connection + pure reconstruction, physically decoupled from the
hub, the alert engine, and the notification dispatcher.

Two complementary guarantees:

* **Structural** (``test_replay_module_is_decoupled_from_live_path``): the replay
  module's source references none of Hub / AlertEngine / NotificationDispatcher / a
  publish call — so it is *incapable* of firing an alert, by construction. This runs
  with no broker.
* **Behavioral** (``test_replay_over_persisted_window_fires_no_live_alert``): replaying
  a window full of persisted observations adds no alert to live state beyond what the
  live demo stream already produced — the replay POST itself fires nothing.

Skips the behavioral test when no broker is reachable (see conftest); CI starts
Mosquitto so it runs there.
"""

from __future__ import annotations

import dataclasses
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

import aether.backend.replay_api as replay_api
from aether.backend.main import create_app
from aether.config import Settings

# A locally-received, provider-classified military demo aircraft (PRD §31.4); the
# seeded "Locally received military aircraft" template matches it once enabled.
_MILITARY_RULE = "rule-aircraft-military-local"


def _app(settings: Settings, db_path: str) -> TestClient:
    cfg = dataclasses.replace(
        settings, demo_source=True, persist=True, persist_sample=False, db_path=db_path
    )
    return TestClient(create_app(settings=cfg, demo_interval_s=0.05))


def _alert_ids(client: TestClient) -> set[str]:
    return {a["id"] for a in client.get("/api/state").json().get("alerts", [])}


def _poll_count(client: TestClient, path: str, *, timeout_s: float = 10.0) -> int:
    """Poll a replay POST window until it reconstructs at least one record."""
    deadline = time.monotonic() + timeout_s
    body: dict[str, Any] = {}
    while time.monotonic() < deadline:
        window = {
            "start": (datetime.now(UTC) - timedelta(hours=1)).isoformat(),
            "end": (datetime.now(UTC) + timedelta(minutes=1)).isoformat(),
        }
        body = client.post(path, json=window).json()
        if body.get("count", 0) >= 1:
            return int(body["count"])
        time.sleep(0.1)
    return int(body.get("count", 0))


def test_replay_module_is_decoupled_from_live_path() -> None:
    """Structural invariant: the replay module cannot reach the alert/notify path.

    Replay firing an alert is impossible *by construction* if the module never imports
    or references the hub, the alert engine, the notification dispatcher, or a publish
    call. Assert that against the module's *code* (its compiled symbol names, not its
    prose docstring) — the cheapest, most durable enforcement of the M4 exit criterion
    (no broker needed).
    """
    import inspect

    # No forbidden type is bound in the module namespace (would have to be imported to
    # be used) — the strongest, false-positive-free check.
    for forbidden in ("Hub", "AlertEngine", "NotificationDispatcher"):
        assert not hasattr(replay_api, forbidden), f"replay_api must not import {forbidden!r}"

    # And no ``.publish(...)`` call appears anywhere in the module's code (recursing
    # into every nested function/handler), scanning compiled bytecode constants — not
    # the docstring, which legitimately *names* these to document the decoupling.
    def _names(code: Any) -> set[str]:
        found = set(code.co_names)
        for const in code.co_consts:
            if inspect.iscode(const):
                found |= _names(const)
        return found

    referenced = _names(replay_api.build_replay_router.__code__)
    assert "publish" not in referenced, "replay_api must not call .publish(...)"

    # The build function takes ONLY cfg — no hub/engine/dispatcher can be injected.
    params = list(inspect.signature(replay_api.build_replay_router).parameters)
    assert params == ["cfg"]


def test_replay_over_persisted_window_fires_no_live_alert(
    broker_settings: Settings, tmp_path: Path
) -> None:
    db_path = str(tmp_path / "replay-invariant.db")
    with _app(broker_settings, db_path) as client:
        # Enable a rule that WOULD match the demo data, so the live engine is actively
        # firing alerts into state — the strongest setting in which to prove replay
        # adds none of its own.
        patched = client.patch(f"/api/v2/alert-rules/{_MILITARY_RULE}", json={"enabled": True})
        assert patched.status_code == 200 and patched.json()["enabled"] is True

        # Let the demo stream persist a window worth of matching observations (the rule
        # was enabled so the live engine is provably active and the persisted tracks are
        # ones that WOULD fire).
        replayed = _poll_count(client, "/api/v2/replay/sessions")
        assert replayed >= 1  # replay reconstructed real persisted records

        # Now QUIESCE the live engine so the assertion is actually falsifiable: with the
        # rule disabled, the live stream fires no NEW alerts, so any alert id that
        # appears during the replay burst could only have come from replay — which the
        # structural test proves is impossible. (The earlier triggered_at heuristic
        # could never fail; this set-delta can.)
        disabled = client.patch(f"/api/v2/alert-rules/{_MILITARY_RULE}", json={"enabled": False})
        assert disabled.status_code == 200 and disabled.json()["enabled"] is False
        time.sleep(0.3)  # let any in-flight live evaluation settle

        before = _alert_ids(client)
        window = {
            "start": (datetime.now(UTC) - timedelta(hours=1)).isoformat(),
            "end": (datetime.now(UTC) + timedelta(minutes=1)).isoformat(),
        }
        session_ids: list[str] = []
        for _ in range(20):
            resp = client.post("/api/v2/replay/sessions", json=window)
            assert resp.status_code == 200
            body = resp.json()
            session_ids.append(body["session_id"])
            # Every record replay returns is a reconstructed observation (a track),
            # never an alert — replay does not synthesize or re-emit alerts.
            assert all(r["kind"] != "alert" for r in body["records"])

        # The engine is quiesced, so NO new alert id may appear. A replay-fired alert
        # would show up here as a fresh id; the set delta must be empty. (Removals from
        # alert lifecycle transitions are ignored — only ADDITIONS would indict replay.)
        new_ids = _alert_ids(client) - before
        assert new_ids == set(), f"replay introduced alert(s) into live state: {new_ids}"

        # Replay sessions are tracked as read-only metadata, decoupled from alerts:
        # the last session is retrievable and reports only its window metadata.
        meta = client.get(f"/api/v2/replay/sessions/{session_ids[-1]}").json()
        assert "records" not in meta
        assert meta["count"] >= 1

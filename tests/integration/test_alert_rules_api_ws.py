"""End-to-end: lifespan seeds default alert-rule templates on startup (M4.5).

Unlike geofences, alert rules don't project into live state — there is no ws
delta to observe. What the lifespan *does* do (and only does with persistence on)
is migrate the store and seed the disabled §12 templates idempotently. This proves
that path against a real broker: starting the app twice over the same store seeds
once, and the seeded rules are readable via the CRUD API and disabled. Skips when
no broker is reachable (see conftest); CI starts Mosquitto so it runs there.
"""

import dataclasses
from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient

from aether.alerts.templates import default_rule_templates
from aether.backend.main import create_app
from aether.config import Settings


def _app(settings: Settings, db_path: str) -> TestClient:
    cfg = dataclasses.replace(settings, demo_source=False, persist=True, db_path=db_path)
    return TestClient(create_app(settings=cfg, demo_interval_s=0.05))


def test_startup_seeds_disabled_templates_idempotently(
    broker_settings: Settings, tmp_path: Path
) -> None:
    db_path = str(tmp_path / "alerts.db")
    expected_ids = {r.id for r in default_rule_templates(datetime.now(UTC))}

    # First boot: lifespan migrates the store and seeds the templates.
    with _app(broker_settings, db_path) as client:
        listing = client.get("/api/v2/alert-rules").json()
        seeded_ids = {r["id"] for r in listing["alert_rules"]}
        assert expected_ids <= seeded_ids
        assert all(r["enabled"] is False for r in listing["alert_rules"])
        first_count = listing["count"]

    # Second boot over the same store: seeding is idempotent (no duplicates).
    with _app(broker_settings, db_path) as client:
        again = client.get("/api/v2/alert-rules").json()
        assert again["count"] == first_count

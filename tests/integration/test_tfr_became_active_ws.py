"""End-to-end: a TFR crosses valid_from -> the clock (not an ingest) fires an alert.

The novelty of PRD §32 #16 is that the alert fires with **no new observation**: a TFR
ingested while still pending must raise an alert the instant its ``valid_from`` passes,
even though the feed publishes nothing at that moment (a re-poll would dedupe the
unchanged revision). This proves the wired clock path end to end against a real broker:
the lifespan seeds the disabled ``rule-tfr-became-active`` template and runs the 1 s
live-state sweep; we enable the rule, publish one TFR feature with a near-future
``valid_from`` onto the bus (pending → no alert), and the periodic sweep re-drives it
across ``valid_from`` so the engine raises an open alert into live state.

Skips when no broker is reachable (see conftest); CI starts Mosquitto so it runs.
"""

from __future__ import annotations

import asyncio
import dataclasses
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from aether.backend.main import create_app
from aether.bus.client import connect
from aether.config import Settings
from aether.schema.geometry import Polygon
from aether.schema.records import GeoFeatureRecord

_RULE = "rule-tfr-became-active"


def _app(settings: Settings, db_path: str) -> TestClient:
    cfg = dataclasses.replace(settings, demo_source=False, persist=True, db_path=db_path)
    return TestClient(create_app(settings=cfg))


def _timed_tfr(valid_from: datetime, valid_until: datetime) -> GeoFeatureRecord:
    now = datetime.now(UTC)
    return GeoFeatureRecord(
        id="tfr:faa:test-activation",
        source="faa_tfr",
        observed_at=now,
        received_at=now,
        published_at=now,
        correlation_key="tfr:faa:test-activation",
        feature_type="tfr",
        geometry=Polygon(
            coordinates=[
                [[-95.2, 39.9], [-94.8, 39.9], [-94.8, 40.1], [-95.2, 40.1], [-95.2, 39.9]]
            ]
        ),
        valid_from=valid_from,
        valid_until=valid_until,
        label="Activation test TFR",
    )


def _publish(settings: Settings, record: GeoFeatureRecord) -> None:
    async def _go() -> None:
        async with connect(settings, identifier="test-tfr-activation") as bus:
            await bus.publish_record(record)

    asyncio.run(_go())


def _alerts_for_rule(client: TestClient, rule_id: str) -> list[dict[str, Any]]:
    return [a for a in client.get("/api/state").json().get("alerts", []) if a["rule_id"] == rule_id]


def _features(client: TestClient) -> list[dict[str, Any]]:
    features: list[dict[str, Any]] = client.get("/api/state").json().get("features", [])
    return features


def test_tfr_becomes_active_fires_from_the_clock(broker_settings: Settings, tmp_path: Path) -> None:
    db_path = str(tmp_path / "tfr-active.db")
    with _app(broker_settings, db_path) as client:
        # The template ships disabled; enabling it syncs the engine immediately.
        patched = client.patch(f"/api/v2/alert-rules/{_RULE}", json={"enabled": True})
        assert patched.status_code == 200 and patched.json()["enabled"] is True

        # Publish a TFR that is still PENDING (valid_from a few seconds out) and lasts an
        # hour. The lead time must clear the feature's propagation to live state.
        valid_from = datetime.now(UTC) + timedelta(seconds=4)
        _publish(broker_settings, _timed_tfr(valid_from, valid_from + timedelta(hours=1)))

        # Wait for the feature to land — and assert it has NOT fired yet (still pending),
        # so the alert that follows can only come from the clock, not the ingest.
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if any(f["id"] == "tfr:faa:test-activation" for f in _features(client)):
                break
            time.sleep(0.1)
        else:  # pragma: no cover - feature must arrive on a working broker
            raise AssertionError("the pending TFR never reached live state")
        assert _alerts_for_rule(client, _RULE) == []  # pending → not active yet → no alert

        # The 1 s sweep crosses valid_from with no new ingest → the engine fires.
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            alerts = _alerts_for_rule(client, _RULE)
            if alerts:
                break
            time.sleep(0.2)
        else:  # pragma: no cover
            raise AssertionError(f"no became_active alert for {_RULE!r} after valid_from")

        assert alerts[0]["state"] == "open"
        assert alerts[0]["subject_id"] == "tfr:faa:test-activation"

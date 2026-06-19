"""Alert lifecycle + rule-preview endpoints (M4.6b, PRD §21.4).

Hermetic: each test mounts just the router under test on a bare FastAPI app over a
hand-built :class:`~aether.backend.hub.Hub` (and, for ``/test``, a pre-migrated temp
store). No app lifespan, no broker — ack/resolve are pure in-memory live-state
transitions, and ``/test`` is a read-only preview against the hub snapshot.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from aether.alerts.engine import AlertEngine
from aether.backend.alert_rules_api import build_alert_rules_router
from aether.backend.alerts_api import build_alerts_router
from aether.backend.hub import Hub
from aether.config import Settings
from aether.persist.alert_rules import insert_alert_rule
from aether.persist.database import Database
from aether.schema.alert_rule import AlertCondition, AlertRule, AlertRuleCreate
from aether.schema.records import AlertRecord, TrackRecord

T0 = datetime(2026, 6, 19, 12, 0, 0, tzinfo=UTC)


def _alert(alert_id: str, *, state: str = "open") -> AlertRecord:
    return AlertRecord(
        id=alert_id,
        source="alert-engine",
        observed_at=T0,
        received_at=T0,
        published_at=T0,
        rule_id="rule-x",
        subject_id="aircraft:icao:abc",
        state=state,  # type: ignore[arg-type]
        severity="high",
        title="Emergency squawk",
        summary="Emergency squawk — aircraft:icao:abc",
        triggered_at=T0,
    )


def _aircraft(*, squawk: str, id: str) -> TrackRecord:
    return TrackRecord(
        id=id,
        source="local_adsb",
        observed_at=T0,
        received_at=T0,
        published_at=T0,
        correlation_key=id,
        track_type="aircraft",
        locally_received=True,
        attributes={"squawk": squawk},
    )


def _alerts_client(hub: Hub) -> TestClient:
    app = FastAPI()
    app.include_router(build_alerts_router(hub))
    return TestClient(app)


def test_acknowledge_transitions_live_alert() -> None:
    hub = Hub()
    hub.publish(_alert("alert-1"))
    client = _alerts_client(hub)

    resp = client.post("/api/v2/alerts/alert-1/acknowledge")
    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "acknowledged"
    assert body["acknowledged_at"] is not None
    live = hub.state.get_alert("alert-1")
    assert live is not None and live.state == "acknowledged"


def test_resolve_transitions_live_alert() -> None:
    hub = Hub()
    hub.publish(_alert("alert-1"))
    client = _alerts_client(hub)

    resp = client.post("/api/v2/alerts/alert-1/resolve")
    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "resolved"
    assert body["resolved_at"] is not None


def test_ack_resolve_404_for_unknown_alert() -> None:
    client = _alerts_client(Hub())
    assert client.post("/api/v2/alerts/nope/acknowledge").status_code == 404
    assert client.post("/api/v2/alerts/nope/resolve").status_code == 404


def _rules_client(
    tmp_path: Path, *, persist: bool = True, with_tracks: bool = True
) -> tuple[TestClient, str, Hub]:
    path = str(tmp_path / "alerts.db")
    db = Database(path)  # pre-migrate so the alert_rules table exists
    db.open()
    db.close()
    rule = AlertRule.create(
        AlertRuleCreate(
            name="Emergency squawk 7700",
            severity="high",
            subject_types=["aircraft"],
            conditions=[AlertCondition(field="attributes.squawk", operator="equals", value="7700")],
            channels=["dashboard"],
        ),
        id="rule-test",
        now=T0,
    )
    insert_alert_rule(path, rule)
    hub = Hub()
    if with_tracks:
        hub.publish(_aircraft(squawk="7700", id="a1"))
        hub.publish(_aircraft(squawk="1200", id="a2"))
    cfg = Settings(demo_source=False, persist=persist, db_path=path)
    engine = AlertEngine(clock=lambda: datetime.now(UTC))
    app = FastAPI()
    app.include_router(build_alert_rules_router(cfg, hub, engine))
    return TestClient(app), rule.id, hub


def test_test_endpoint_previews_matches_against_live_state(tmp_path: Path) -> None:
    client, rid, _hub = _rules_client(tmp_path)
    resp = client.post(f"/api/v2/alert-rules/{rid}/test")
    assert resp.status_code == 200
    body = resp.json()
    assert body["evaluable"] is True
    assert body["evaluated"] == 2  # both aircraft are candidates
    assert body["matched"] == 1  # only the 7700 one matches


def test_test_endpoint_404_for_unknown_rule(tmp_path: Path) -> None:
    client, _rid, _hub = _rules_client(tmp_path)
    assert client.post("/api/v2/alert-rules/nope/test").status_code == 404


def test_test_endpoint_503_when_persistence_disabled(tmp_path: Path) -> None:
    client, rid, _hub = _rules_client(tmp_path, persist=False, with_tracks=False)
    assert client.post(f"/api/v2/alert-rules/{rid}/test").status_code == 503

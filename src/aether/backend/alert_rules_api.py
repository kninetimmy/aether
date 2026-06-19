"""REST CRUD for operator alert rules (PRD §21.4, §11.16 ALERT-FR-001..003).

``GET/POST/PATCH/DELETE /api/v2/alert-rules`` over the SQLite store (PRD §19.3).
Alert rules are operator config: created/edited here, persisted by
:mod:`aether.persist.alert_rules`. Unlike geofences they do **not** project into
live state (a rule isn't a map feature), so there is no hub interaction — just
store I/O. That I/O runs in a worker thread (``asyncio.to_thread``) so a
slow/locked store never blocks the event loop, and CRUD is gated behind
``AETHER_PERSIST`` (503 when off) — exactly like the geofence/history endpoints,
since rules live in the same store and live state never depends on it (PRD §5).

This slice is CRUD only. ``POST /api/v2/alert-rules/{id}/test`` (rule preview /
test fire, PRD §21.4) needs the evaluation engine and lands with it.
"""

from __future__ import annotations

import asyncio
import sqlite3
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Response

from aether.config import Settings
from aether.persist.alert_rules import (
    delete_alert_rule,
    get_alert_rule,
    insert_alert_rule,
    list_alert_rules,
    update_alert_rule,
)
from aether.schema.alert_rule import AlertRule, AlertRuleCreate, AlertRuleUpdate

_INITIALIZING = "persistence initializing; retry shortly"


def _new_id() -> str:
    return f"rule-{uuid.uuid4().hex[:12]}"


def build_alert_rules_router(cfg: Settings) -> APIRouter:
    """Build the alert-rule CRUD router bound to this app's config."""
    router = APIRouter(prefix="/api/v2/alert-rules", tags=["alert-rules"])

    def _require_persist() -> None:
        # Rules live in the persistence store; with it off there is nowhere to read
        # or write them. 503 (not "empty") so the client sees a categorical
        # unavailability, mirroring the geofence/history endpoints (PRD §37).
        if not cfg.persist:
            raise HTTPException(
                status_code=503, detail="persistence disabled; alert rules unavailable"
            )

    @router.get("")
    async def list_all() -> dict[str, object]:
        _require_persist()
        rules = await asyncio.to_thread(list_alert_rules, cfg.db_path)
        return {"count": len(rules), "alert_rules": rules}

    @router.post("", status_code=201)
    async def create(body: AlertRuleCreate) -> AlertRule:
        _require_persist()
        rule = AlertRule.create(body, id=_new_id(), now=datetime.now(UTC))
        try:
            await asyncio.to_thread(insert_alert_rule, cfg.db_path, rule)
        except sqlite3.OperationalError as exc:  # store not migrated yet (cold start)
            raise HTTPException(status_code=503, detail=_INITIALIZING) from exc
        return rule

    @router.get("/{rule_id}")
    async def get_one(rule_id: str) -> AlertRule:
        _require_persist()
        rule = await asyncio.to_thread(get_alert_rule, cfg.db_path, rule_id)
        if rule is None:
            raise HTTPException(status_code=404, detail=f"no alert rule {rule_id!r}")
        return rule

    @router.patch("/{rule_id}")
    async def patch(rule_id: str, body: AlertRuleUpdate) -> AlertRule:
        _require_persist()
        existing = await asyncio.to_thread(get_alert_rule, cfg.db_path, rule_id)
        if existing is None:
            raise HTTPException(status_code=404, detail=f"no alert rule {rule_id!r}")
        updated = existing.with_update(body, now=datetime.now(UTC))
        try:
            await asyncio.to_thread(update_alert_rule, cfg.db_path, updated)
        except sqlite3.OperationalError as exc:
            raise HTTPException(status_code=503, detail=_INITIALIZING) from exc
        return updated

    @router.delete("/{rule_id}", status_code=204)
    async def remove(rule_id: str) -> Response:
        _require_persist()
        try:
            existed = await asyncio.to_thread(delete_alert_rule, cfg.db_path, rule_id)
        except sqlite3.OperationalError as exc:
            raise HTTPException(status_code=503, detail=_INITIALIZING) from exc
        if not existed:
            raise HTTPException(status_code=404, detail=f"no alert rule {rule_id!r}")
        return Response(status_code=204)

    return router

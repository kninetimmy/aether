"""REST CRUD for operator alert rules (PRD §21.4, §11.16 ALERT-FR-001..003).

``GET/POST/PATCH/DELETE /api/v2/alert-rules`` over the SQLite store (PRD §19.3),
plus ``POST /api/v2/alert-rules/{id}/test`` (rule preview, PRD §21.4). Alert rules
are operator config: created/edited here, persisted by
:mod:`aether.persist.alert_rules`. The SQLite store is authoritative; the
in-memory :class:`~aether.alerts.engine.AlertEngine` holds the *live* ruleset it
evaluates, so every successful write here also syncs the engine
(``upsert_rule``/``remove_rule``) — that keeps evaluation off the hot-path disk
read while making an enable/edit take effect immediately, with no polling.

Store I/O runs in a worker thread (``asyncio.to_thread``) so a slow/locked store
never blocks the event loop, and CRUD is gated behind ``AETHER_PERSIST`` (503 when
off) — exactly like the geofence/history endpoints, since rules live in the same
store and live state never depends on it (PRD §5). ``/test`` previews a rule
against *current live state* (a dry run — no firing, no state change); preview
against recorded sample data / replay is a later slice.
"""

from __future__ import annotations

import asyncio
import sqlite3
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Response

from aether.alerts.engine import AlertEngine, preview_rule
from aether.backend.hub import Hub
from aether.config import Settings
from aether.persist.alert_rules import (
    delete_alert_rule,
    get_alert_rule,
    insert_alert_rule,
    list_alert_rules,
    update_alert_rule,
)
from aether.schema.alert_rule import AlertRule, AlertRuleCreate, AlertRuleUpdate
from aether.schema.records import Record

_INITIALIZING = "persistence initializing; retry shortly"


def _new_id() -> str:
    return f"rule-{uuid.uuid4().hex[:12]}"


def build_alert_rules_router(cfg: Settings, hub: Hub, engine: AlertEngine) -> APIRouter:
    """Build the alert-rule CRUD + preview router bound to this app's config.

    ``hub`` supplies live-state records for ``/test``; ``engine`` is the in-memory
    ruleset kept in sync with each successful write so evaluation reflects edits at
    once without re-reading the store.
    """
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
        engine.upsert_rule(rule)  # sync the live ruleset (store is authoritative)
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
        engine.upsert_rule(updated)  # an enable/edit takes effect on the next change
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
        engine.remove_rule(rule_id)  # stop evaluating a deleted rule immediately
        return Response(status_code=204)

    @router.post("/{rule_id}/test")
    async def test_rule(rule_id: str) -> dict[str, Any]:
        """Preview which current subjects a rule matches, without firing (PRD §21.4).

        A dry run against the live snapshot: no alert is emitted and no firing state
        changes. Reports each matching-subject-typed record's current match (or
        ``None`` per subject when the rule uses a contextual operator the level core
        can't preview yet — honest "unknown", not a misleading False).
        """
        _require_persist()
        rule = await asyncio.to_thread(get_alert_rule, cfg.db_path, rule_id)
        if rule is None:
            raise HTTPException(status_code=404, detail=f"no alert rule {rule_id!r}")
        snapshot = hub.state.snapshot()
        candidates: list[Record] = [*snapshot.tracks, *snapshot.source_status, *snapshot.events]
        return preview_rule(rule, candidates)

    return router

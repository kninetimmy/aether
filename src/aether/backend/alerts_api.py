"""Alert lifecycle endpoints — acknowledge / resolve (PRD §21.4, §20.5).

``POST /api/v2/alerts/{id}/acknowledge`` and ``/resolve`` transition a *live* alert
(the ones the engine raised into in-memory live state) and rebroadcast it to every
connected client. Unlike rules, alerts are not stored in the SQLite config tables in
this slice — they live in live state alongside tracks/features — so these endpoints
need no persistence and never touch the disk store (alert history/persistence is a
later M4 slice). The transition itself is in-memory and synchronous, so there is no
``to_thread`` hop.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException

from aether.backend.hub import Hub
from aether.schema.validation import dump_record


def build_alerts_router(hub: Hub) -> APIRouter:
    """Build the alert ack/resolve router bound to this app's hub."""
    router = APIRouter(prefix="/api/v2/alerts", tags=["alerts"])

    @router.post("/{alert_id}/acknowledge")
    async def acknowledge(alert_id: str) -> dict[str, Any]:
        updated = hub.transition_alert(alert_id, "acknowledged", datetime.now(UTC))
        if updated is None:
            raise HTTPException(status_code=404, detail=f"no live alert {alert_id!r}")
        return dump_record(updated)

    @router.post("/{alert_id}/resolve")
    async def resolve(alert_id: str) -> dict[str, Any]:
        updated = hub.transition_alert(alert_id, "resolved", datetime.now(UTC))
        if updated is None:
            raise HTTPException(status_code=404, detail=f"no live alert {alert_id!r}")
        return dump_record(updated)

    return router

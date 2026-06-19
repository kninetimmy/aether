"""REST CRUD for operator geofences (PRD §21.5, §11.1 COP-FR-008).

``GET/POST/PATCH/DELETE /api/v2/geofences`` over the SQLite store (PRD §19.3).
Geofences are operator config: created/edited here, persisted by
:mod:`aether.persist.geofences`, and *projected* into live state as
``feature_type="geofence"`` features so they render on the map and reach connected
clients as deltas. Store I/O runs in a worker thread (``asyncio.to_thread``) so a
slow/locked store never blocks the event loop, and CRUD is gated behind
``AETHER_PERSIST`` (503 when off) — exactly like the M4.3 history read, since
geofences live in the same store and live state never depends on it (PRD §5).
"""

from __future__ import annotations

import asyncio
import sqlite3
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Response

from aether.backend.hub import Hub
from aether.config import Settings
from aether.persist.geofences import (
    delete_geofence,
    get_geofence,
    insert_geofence,
    list_geofences,
    update_geofence,
)
from aether.schema.geofence import Geofence, GeofenceCreate, GeofenceUpdate


def _new_id() -> str:
    return f"geofence-{uuid.uuid4().hex[:12]}"


def build_geofence_router(cfg: Settings, hub: Hub) -> APIRouter:
    """Build the geofence CRUD router bound to this app's config + hub."""
    router = APIRouter(prefix="/api/v2/geofences", tags=["geofences"])

    def _require_persist() -> None:
        # Geofences live in the persistence store; with it off there is nowhere to
        # read or write them. 503 (not "empty") so the client sees a categorical
        # unavailability, mirroring the track-history read (PRD §37).
        if not cfg.persist:
            raise HTTPException(
                status_code=503, detail="persistence disabled; geofences unavailable"
            )

    @router.get("")
    async def list_all() -> dict[str, object]:
        _require_persist()
        geofences = await asyncio.to_thread(list_geofences, cfg.db_path)
        return {"count": len(geofences), "geofences": geofences}

    @router.post("", status_code=201)
    async def create(body: GeofenceCreate) -> Geofence:
        _require_persist()
        now = datetime.now(UTC)
        geofence = Geofence.create(body, id=_new_id(), now=now)
        try:
            await asyncio.to_thread(insert_geofence, cfg.db_path, geofence)
        except sqlite3.OperationalError as exc:  # store not migrated yet (cold start)
            raise HTTPException(
                status_code=503, detail="persistence initializing; retry shortly"
            ) from exc
        hub.publish(geofence.to_feature_record())
        return geofence

    @router.get("/{geofence_id}")
    async def get_one(geofence_id: str) -> Geofence:
        _require_persist()
        geofence = await asyncio.to_thread(get_geofence, cfg.db_path, geofence_id)
        if geofence is None:
            raise HTTPException(status_code=404, detail=f"no geofence {geofence_id!r}")
        return geofence

    @router.patch("/{geofence_id}")
    async def patch(geofence_id: str, body: GeofenceUpdate) -> Geofence:
        _require_persist()
        existing = await asyncio.to_thread(get_geofence, cfg.db_path, geofence_id)
        if existing is None:
            raise HTTPException(status_code=404, detail=f"no geofence {geofence_id!r}")
        updated = existing.with_update(body, now=datetime.now(UTC))
        try:
            await asyncio.to_thread(update_geofence, cfg.db_path, updated)
        except sqlite3.OperationalError as exc:
            raise HTTPException(
                status_code=503, detail="persistence initializing; retry shortly"
            ) from exc
        hub.publish(updated.to_feature_record())  # re-project the edited overlay
        return updated

    @router.delete("/{geofence_id}", status_code=204)
    async def remove(geofence_id: str) -> Response:
        _require_persist()
        try:
            existed = await asyncio.to_thread(delete_geofence, cfg.db_path, geofence_id)
        except sqlite3.OperationalError as exc:
            raise HTTPException(
                status_code=503, detail="persistence initializing; retry shortly"
            ) from exc
        if not existed:
            raise HTTPException(status_code=404, detail=f"no geofence {geofence_id!r}")
        hub.remove("feature", geofence_id)  # drop the overlay from every client
        return Response(status_code=204)

    return router

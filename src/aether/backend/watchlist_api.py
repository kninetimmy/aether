"""REST CRUD for the operator tracks-of-interest watchlist (PRD §21.5, §24.6).

``GET/PUT/PATCH/DELETE /api/v2/watchlist`` over the SQLite store (PRD §19.3).
The watchlist is operator config: entries are PUT/deleted here, persisted by
:mod:`aether.persist.watchlist`, and their identity keys are synced into the alert
engine's contextual evaluator so ``watchlist`` condition leaves fire correctly.

Unlike geofences this has **no live-map projection** — a watchlist entry is not an
overlay, so no ``hub`` parameter here (the factory signature deliberately diverges
from :func:`~aether.backend.geofence_api.build_geofence_router`). Membership state
lives only in the engine's in-memory set, synced here after every successful write.

The watchlist key is CLIENT-MINTED and DETERMINISTIC (``watchlistKey(track)``), so
PUT-to-a-known-URI with upsert semantics is correct REST and makes the toggle
idempotent. Colons in keys (``aircraft:icao:abc123``, ``orbital:celestrak:25544``)
are legal path-segment chars; Starlette's ``{key:path}`` converter decodes the whole
tail including any encoded colons transparently.

Store I/O runs in a worker thread (``asyncio.to_thread``) so a slow/locked store
never blocks the event loop, and CRUD is gated behind ``AETHER_PERSIST`` (503 when
off) — exactly like geofences and track history.
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Response

from aether.alerts.engine import AlertEngine
from aether.config import Settings
from aether.persist.watchlist import (
    delete_watchlist_entry,
    get_watchlist_entry,
    list_watchlist,
    upsert_watchlist_entry,
)
from aether.schema.watchlist import WatchlistEntry, WatchlistEntryCreate, WatchlistEntryUpdate

#: Maximum byte length for a watchlist key in the path (server-side guard to bound the PK).
_MAX_KEY_LEN = 256


def build_watchlist_router(cfg: Settings, engine: AlertEngine) -> APIRouter:
    """Build the watchlist CRUD router bound to this app's config and engine.

    ``engine`` is the in-memory membership set kept in sync with each successful
    write so the ``watchlist`` condition operator reflects edits immediately without
    re-reading the store on the evaluation hot path.
    """
    router = APIRouter(prefix="/api/v2/watchlist", tags=["watchlist"])

    def _require_persist() -> None:
        # The watchlist lives in the persistence store; with it off there is nowhere
        # to read or write entries. 503 (not "empty") so the client sees a categorical
        # unavailability, mirroring the track-history read (PRD §37).
        if not cfg.persist:
            raise HTTPException(
                status_code=503, detail="persistence disabled; watchlist unavailable"
            )

    def _validate_key(key: str) -> None:
        if len(key) > _MAX_KEY_LEN:
            raise HTTPException(
                status_code=422,
                detail=f"watchlist key too long (max {_MAX_KEY_LEN} chars)",
            )

    @router.get("")
    async def list_all() -> dict[str, object]:
        _require_persist()
        entries = await asyncio.to_thread(list_watchlist, cfg.db_path)
        return {"count": len(entries), "entries": entries}

    @router.get("/{key:path}")
    async def get_one(key: str) -> WatchlistEntry:
        _require_persist()
        _validate_key(key)
        entry = await asyncio.to_thread(get_watchlist_entry, cfg.db_path, key)
        if entry is None:
            raise HTTPException(status_code=404, detail=f"no watchlist entry {key!r}")
        return entry

    @router.put("/{key:path}")
    async def upsert(key: str, body: WatchlistEntryCreate) -> WatchlistEntry:
        """Upsert a watchlist entry (idempotent PUT).

        On create: ``created_at=updated_at=now``.
        On replace: ``created_at`` is preserved, meta updated, ``updated_at=now``.
        Always returns 200 so the toggle client treats create and replace identically.
        """
        _require_persist()
        _validate_key(key)
        now = datetime.now(UTC)
        existing = await asyncio.to_thread(get_watchlist_entry, cfg.db_path, key)
        if existing is not None:
            # Preserve created_at; apply new meta from body.
            # Explicitly set every field from body (not just model_fields_set) so a
            # PUT with label=None *clears* the label rather than leaving the old one.
            entry = WatchlistEntry(
                key=key,
                label=body.label,
                priority=body.priority,
                notes=body.notes,
                created_at=existing.created_at,
                updated_at=now,
            )
        else:
            entry = WatchlistEntry.create(body, key=key, now=now)
        try:
            await asyncio.to_thread(upsert_watchlist_entry, cfg.db_path, entry)
        except sqlite3.OperationalError as exc:
            raise HTTPException(
                status_code=503, detail="persistence initializing; retry shortly"
            ) from exc
        engine.upsert_watchlist(key)  # sync the membership set immediately
        return entry

    @router.patch("/{key:path}")
    async def patch(key: str, body: WatchlistEntryUpdate) -> WatchlistEntry:
        """Partial meta edit only; membership is unchanged (key still in the set)."""
        _require_persist()
        _validate_key(key)
        existing = await asyncio.to_thread(get_watchlist_entry, cfg.db_path, key)
        if existing is None:
            raise HTTPException(status_code=404, detail=f"no watchlist entry {key!r}")
        updated = existing.with_update(body, now=datetime.now(UTC))
        try:
            await asyncio.to_thread(upsert_watchlist_entry, cfg.db_path, updated)
        except sqlite3.OperationalError as exc:
            raise HTTPException(
                status_code=503, detail="persistence initializing; retry shortly"
            ) from exc
        engine.upsert_watchlist(key)  # no-op add; keeps engine authoritative
        return updated

    @router.delete("/{key:path}", status_code=204)
    async def remove(key: str) -> Response:
        _require_persist()
        _validate_key(key)
        try:
            existed = await asyncio.to_thread(delete_watchlist_entry, cfg.db_path, key)
        except sqlite3.OperationalError as exc:
            raise HTTPException(
                status_code=503, detail="persistence initializing; retry shortly"
            ) from exc
        if not existed:
            raise HTTPException(status_code=404, detail=f"no watchlist entry {key!r}")
        engine.remove_watchlist(key)  # stop the rule matching a deleted entry immediately
        return Response(status_code=204)

    return router

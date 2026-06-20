"""REST record/replay over persisted history (M4.8, PRD §19.6/§21.6).

``POST/GET/DELETE /api/v2/replay/sessions`` — the final M4 exit slice. A replay
session reconstructs a *bounded, server-side buffer* for a ``[start, end)`` window
from the SQLite store and returns it once over plain REST; the browser holds the
buffer and runs the timeline locally (PRD §19.6). The session metadata is kept in a
small bounded in-memory registry so a client can re-read what a session was and tear
it down; the records are not re-served (they live in the browser).

THE HARD M4 INVARIANT (PRD §19.6/§32): **replay cannot fire live alerts or
notifications.** This is guaranteed *structurally* — the module is REST + a read-only
window read (:func:`aether.persist.database.read_observations_window`) + pure record
reconstruction (:func:`aether.replay.player.reconstruct_records`). It does NOT import
or reference the hub, ``hub.publish``, the alert engine, or the notification
dispatcher, and never mutates live state. Verify by inspection of the import block.

Both the store read AND the CPU-bound record reconstruction run in one worker thread
(``asyncio.to_thread``) on a fresh read-only connection, so neither a slow/locked store
nor a large window's pydantic reconstruction blocks the event loop or gates serving
live state (PRD §5) — the endpoints are gated behind ``AETHER_PERSIST`` (503 when off),
exactly like the geofence/history/alert-rule endpoints. The window is bounded by time,
optional source filter, and a ``max_records`` cap clamped to ``cfg.replay_max_records``
(PRD §21.6); ``truncated`` flags a capped buffer so the earliest ``max_records`` are
never mistaken for the complete window.

Scope (M4): the persistence writer stores only ``TrackRecord`` observations, so a
replay buffer reconstructs **track history** — geo-features, events, and alerts are not
yet persisted (a later slice), and §19.6's optional "show which historical alerts
occurred" is deferred. ``read_observations_window`` additionally guards ``kind='track'``
so the buffer stays track-only by construction even if a future migration persists other
kinds. §21.6 also specifies ``GET /api/v2/export`` (GeoJSON/CSV/JSON); export is a
separate later slice — this module implements only the replay-sessions trio.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field

from aether.config import Settings
from aether.persist.database import read_observations_window
from aether.replay.player import reconstruct_records
from aether.replay.session import ReplaySession, SessionRegistry

log = logging.getLogger(__name__)

#: Cap on the optional contributing-source filter list — only a handful of sources
#: exist, so a request can never ask for an unbounded set (PRD §37).
_MAX_SOURCES = 32


class ReplaySessionRequest(BaseModel):
    """Request body for ``POST /api/v2/replay/sessions`` (PRD §21.6 bounded export).

    ``start``/``end`` are ISO-8601 instants defining the half-open ``[start, end)``
    window (validated server-side: parseable and ``end`` > ``start``, within
    ``cfg.replay_max_window_h``). ``sources`` optionally restricts to those
    contributing sources (count- and per-item-length-bounded so a request can never
    carry an unbounded payload, PRD §37); ``max_records`` optionally lowers the
    per-window cap (it is always also clamped to ``cfg.replay_max_records``). The
    ``sources`` filter is API-only by design — the UI launcher replays the full window
    (a shared cross-source timeline, HISTORY-FR-005); per-source scoping is a deliberate
    API refinement, not a forgotten control.
    """

    model_config = ConfigDict(extra="forbid")

    start: str = Field(min_length=1)
    end: str = Field(min_length=1)
    sources: list[Annotated[str, Field(max_length=64)]] | None = Field(
        default=None, max_length=_MAX_SOURCES
    )
    max_records: int | None = Field(default=None, ge=1)


def _normalize_iso(value: str) -> str:
    """Normalize an ISO-8601 bound to the store's canonical UTC-ISO form.

    The store compares ``observed_at`` lexically (UTC ISO with a fixed ``+00:00``
    offset), so a bound must be converted to that exact shape to compare
    chronologically — any offset is converted to UTC and a naive instant is read as
    UTC. Raises ``ValueError`` on an unparseable value (the endpoint maps that to 400).
    Mirrors the track-history endpoint's helper in :mod:`aether.backend.main`.
    """
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat()


def build_replay_router(cfg: Settings) -> APIRouter:
    """Build the replay router bound to this app's config.

    Holds its own bounded :class:`SessionRegistry`. Takes only ``cfg`` — no hub, no
    engine, no dispatcher — which is the structural guarantee that replay cannot fire
    live alerts/notifications (the M4 exit invariant, PRD §19.6/§32).
    """
    router = APIRouter(prefix="/api/v2/replay", tags=["replay"])
    registry = SessionRegistry()

    def _require_persist() -> None:
        # Replay reads the persistence store; with it off there is nothing to replay.
        # 503 (not "empty") so the client sees a categorical unavailability, mirroring
        # the geofence/history/alert-rule endpoints (PRD §37).
        if not cfg.persist:
            raise HTTPException(status_code=503, detail="persistence disabled; replay unavailable")

    @router.post("/sessions", status_code=200)
    async def create_session(body: ReplaySessionRequest) -> dict[str, Any]:
        """Reconstruct a ``[start, end)`` window and return it as a replay buffer.

        Reads observations in the window (read-only connection in a worker thread),
        reconstructs each row's verbatim payload into a wire record, and returns them
        ascending by ``observed_at``. The window is bounded: ``end`` must be after
        ``start`` and within ``cfg.replay_max_window_h``; the record count is clamped
        to ``cfg.replay_max_records`` (``truncated`` true when the cap is hit — only
        the *earliest* ``max_records`` are returned, the window may hold more). A row
        whose payload can't be reconstructed is skipped, never a 500 (PRD §37). This
        path never touches the hub, alert engine, or live state (PRD §19.6 invariant).
        """
        _require_persist()
        try:
            start_iso = _normalize_iso(body.start)
            end_iso = _normalize_iso(body.end)
        except ValueError:
            raise HTTPException(
                status_code=400, detail="start/end must be ISO-8601 timestamps"
            ) from None
        if end_iso <= start_iso:
            raise HTTPException(status_code=400, detail="end must be after start")
        span_h = (
            datetime.fromisoformat(end_iso) - datetime.fromisoformat(start_iso)
        ).total_seconds() / 3600.0
        if span_h > cfg.replay_max_window_h:
            raise HTTPException(
                status_code=400,
                detail=f"window {span_h:.1f}h exceeds limit {cfg.replay_max_window_h}h",
            )

        cap = cfg.replay_max_records
        want = cap if body.max_records is None else min(body.max_records, cap)
        sources = body.sources if body.sources else None

        def _read_and_reconstruct() -> tuple[int, list[dict[str, Any]]]:
            # Read one past the cap so a full read is *known* to be truncated rather
            # than merely "exactly at the limit" (the window may hold more than `want`),
            # then reconstruct — BOTH the read and the CPU-bound pydantic reconstruction
            # run on this worker thread so neither blocks the event loop (PRD §5: replay
            # must never gate serving live state). Returns the rows-read count (for
            # `truncated`) and the reconstructed wire records.
            rows = read_observations_window(
                cfg.db_path,
                start_iso=start_iso,
                end_iso=end_iso,
                sources=sources,
                limit=want + 1,
            )
            return len(rows), reconstruct_records(rows[:want])

        try:
            n_rows, records = await asyncio.to_thread(_read_and_reconstruct)
        except sqlite3.OperationalError:
            n_rows, records = 0, []  # store not created yet (nothing persisted) → empty
        except sqlite3.Error:
            log.warning(
                "replay window read failed for [%s, %s); returning empty",
                start_iso,
                end_iso,
                exc_info=True,
            )
            n_rows, records = 0, []
        truncated = n_rows > want

        session = ReplaySession(
            session_id=uuid.uuid4().hex,
            start=start_iso,
            end=end_iso,
            sources=sources,
            count=len(records),
            truncated=truncated,
            created_at=datetime.now(UTC).isoformat(),
        )
        registry.add(session)
        return {
            "session_id": session.session_id,
            "start": session.start,
            "end": session.end,
            "sources": session.sources,
            "count": session.count,
            "truncated": session.truncated,
            "records": records,
        }

    @router.get("/sessions/{session_id}")
    async def get_session(session_id: str) -> dict[str, Any]:
        """Return one session's metadata (not its records — those live in the client)."""
        _require_persist()
        session = registry.get(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail=f"no replay session {session_id!r}")
        return {
            "session_id": session.session_id,
            "start": session.start,
            "end": session.end,
            "sources": session.sources,
            "count": session.count,
            "truncated": session.truncated,
            "created_at": session.created_at,
        }

    @router.delete("/sessions/{session_id}", status_code=204)
    async def delete_session(session_id: str) -> Response:
        """Forget a replay session (the client drops its buffer alongside)."""
        _require_persist()
        if not registry.delete(session_id):
            raise HTTPException(status_code=404, detail=f"no replay session {session_id!r}")
        return Response(status_code=204)

    return router

"""FastAPI app: REST live state + sequence-numbered websocket (PRD §21–22).

In-memory only for M1. Records now flow over the MQTT bus (PRD §23): the lifespan
runs a subscriber that ingests ``aether/v2/...`` topics into the hub, which serves
``/api/state`` and streams ``/ws/v2`` (snapshot then deltas). For the no-hardware
demo the lifespan also runs the demo publisher in-process; a real deployment sets
``AETHER_DEMO_SOURCE=0`` and runs source adapters instead.

Run the no-hardware demo (broker first):
    docker compose up -d
    uvicorn aether.backend.main:app --host 127.0.0.1 --port 8000
"""

import asyncio
import contextlib
import logging
import sqlite3
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import aiomqtt
from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect

from aether.adapters.ais import run_ais
from aether.adapters.aprs_is import run_aprs_is
from aether.adapters.local_adsb import run_local_adsb
from aether.adapters.local_aprs import run_local_aprs
from aether.adapters.network_adsb import run_network_adsb
from aether.alerts.templates import default_rule_templates
from aether.backend.alert_rules_api import build_alert_rules_router
from aether.backend.geofence_api import build_geofence_router
from aether.backend.hub import Connection, Hub
from aether.backend.protocol import snapshot_message
from aether.backend.subscription import default_filter, parse_subscribe
from aether.bus.client import DEFAULT_RECONNECT_S, connect, run_record_subscriber
from aether.bus.demo_publisher import run_demo_publisher
from aether.config import Settings
from aether.persist.alert_rules import seed_alert_rules
from aether.persist.database import Database, ObservationRow, read_track_history
from aether.persist.geofences import list_geofences
from aether.persist.runner import run_persistence
from aether.schema.validation import dump_record

log = logging.getLogger(__name__)

#: Grace period for source tasks to stop on shutdown before they're abandoned.
SHUTDOWN_GRACE_S = 5.0

#: How often the backend ages out stale fused tracks (PRD §15.4). Independent of
#: the bus: it surfaces LOCAL→NET handoffs and removes tracks once every
#: contributor has gone silent, even with no new ingest.
EXPIRY_INTERVAL_S = 1.0

#: Minimum spacing between accepted ``subscribe`` frames on one connection. The
#: client debounces at ~300 ms; this is the SERVER guard so subscribe-spam can't
#: starve the send loop or the shared event loop (PRD §37 failure isolation). A
#: frame arriving inside the window is parsed for validity but does not re-snapshot.
SUBSCRIBE_MIN_INTERVAL_S = 0.25


def create_app(*, settings: Settings | None = None, demo_interval_s: float = 1.0) -> FastAPI:
    cfg = settings if settings is not None else Settings.from_env()
    hub = Hub()

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        ready = asyncio.Event()
        tasks = [
            asyncio.create_task(
                run_record_subscriber(cfg, hub.publish, ready=ready, identifier="aether-backend")
            ),
            asyncio.create_task(_run_expiry(hub)),
        ]
        if cfg.demo_source:
            tasks.append(asyncio.create_task(_run_demo(cfg, ready, demo_interval_s)))
        if cfg.local_adsb:
            tasks.append(asyncio.create_task(run_local_adsb(cfg, ready)))
        if cfg.local_aprs:
            tasks.append(asyncio.create_task(run_local_aprs(cfg, ready)))
        if cfg.network_adsb:
            tasks.append(asyncio.create_task(run_network_adsb(cfg, ready)))
        if cfg.aprs_is:
            tasks.append(asyncio.create_task(run_aprs_is(cfg, ready)))
        if cfg.ais:
            tasks.append(asyncio.create_task(run_ais(cfg, ready)))
        if cfg.persist:
            # Ensure the schema exists *before* the sibling tasks and the seed run:
            # a short opener applies migrations idempotently (the writer below reopens
            # an already-migrated store, a no-op). This makes startup seeding
            # deterministic on a brand-new store instead of racing the writer's async
            # open. Exception-isolated — a bad path degrades to no persistence rather
            # than wedging boot (PRD §5/§37).
            await _ensure_store(cfg)
            # A sibling bus consumer, not a dependency of live state (PRD §5): its own
            # broker session and queue, so a slow/failed disk never gates serving.
            tasks.append(asyncio.create_task(run_persistence(cfg)))
            # Project persisted geofences into live state so reconnecting clients see
            # them in the first snapshot (PRD §11.1/§21.5). Read-only and
            # exception-isolated: a missing/locked/corrupt store yields no overlays,
            # never a failed startup (PRD §5/§37).
            await _load_geofences(cfg, hub)
            # Seed the default alert-rule templates into the store (PRD §11.16
            # ALERT-FR-008), idempotently and disabled. Write path, so it runs after
            # the schema is ensured; exception-isolated like the geofence load.
            await _seed_alert_rules(cfg)
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
            # Bound the shutdown wait: aiomqtt's graceful disconnect-on-cancel can
            # intermittently hang (observed flaky on Python 3.11), and an unbounded
            # await would wedge shutdown — and any TestClient/lifespan around it —
            # forever. Abandon a straggler after a short grace; the loop is tearing
            # down regardless, so a stuck client connection is moot (PRD §37).
            _done, pending = await asyncio.wait(tasks, timeout=SHUTDOWN_GRACE_S)
            if pending:
                log.warning(
                    "%d source task(s) did not stop within %.0fs; abandoning",
                    len(pending),
                    SHUTDOWN_GRACE_S,
                )

    app = FastAPI(title="aether COP", version="0.1.0", lifespan=lifespan)
    app.include_router(build_geofence_router(cfg, hub))
    app.include_router(build_alert_rules_router(cfg))

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        return {"status": "ok", "seq": hub.state.seq, "clients": hub.client_count}

    @app.get("/api/state")
    async def state() -> dict[str, Any]:
        return snapshot_message(hub.state.snapshot())

    @app.get("/api/config")
    async def config() -> dict[str, Any]:
        """Runtime config for the frontend (PRD §5: coordinates are never committed).

        Feeds the M3.6a range-from-station filter origin. A 0,0 station is reported
        as ``null`` so the client degrades the range control to a disabled no-op
        rather than centering on null island.
        """
        configured = not (cfg.station_lat == 0.0 and cfg.station_lon == 0.0)
        return {
            "station": (
                {
                    "lat": cfg.station_lat,
                    "lon": cfg.station_lon,
                    "radius_nm": cfg.station_radius_nm,
                }
                if configured
                else None
            )
        }

    @app.get("/api/v2/tracks/{track_id}")
    async def track_detail(track_id: str) -> dict[str, Any]:
        """Current fused track by id (PRD §21.3), served from live state.

        404 when no such track is live — the same id the snapshot/websocket exposes
        is the lookup key (a fused track's id is its correlation key).
        """
        track = hub.state.get_track(track_id)
        if track is None:
            raise HTTPException(status_code=404, detail=f"no live track {track_id!r}")
        return dump_record(track)

    @app.get("/api/v2/tracks/{track_id}/history")
    async def track_history(
        track_id: str,
        start: str | None = Query(default=None),
        end: str | None = Query(default=None),
        limit: int | None = Query(default=None, ge=1),
    ) -> dict[str, Any]:
        """Persisted observations for one track, oldest-first (PRD §21.3/§11.15).

        Reads the persistence store on a fresh read-only connection in a worker
        thread, so a slow or locked store can never gate serving live state (PRD §5).
        Honest degradation (PRD §37): 503 when persistence is disabled (history is
        categorically unavailable, not "empty"); a not-yet-created store or a
        transient read error returns an empty list rather than a 500. ``truncated``
        flags that the per-request cap was hit, so a capped trail is never mistaken
        for the complete one.
        """
        if not cfg.persist:
            raise HTTPException(status_code=503, detail="persistence disabled; no track history")
        want = cfg.history_max_points if limit is None else min(limit, cfg.history_max_points)
        try:
            start_iso = _normalize_iso(start)
            end_iso = _normalize_iso(end)
        except ValueError:
            raise HTTPException(
                status_code=400, detail="start/end must be ISO-8601 timestamps"
            ) from None
        try:
            rows = await asyncio.to_thread(
                read_track_history,
                cfg.db_path,
                track_id,
                start_iso=start_iso,
                end_iso=end_iso,
                limit=want,
            )
        except sqlite3.OperationalError:
            rows = []  # store not created yet (nothing persisted) → empty history
        except sqlite3.Error:
            log.warning(
                "track history read failed for %s; returning empty", track_id, exc_info=True
            )
            rows = []
        return {
            "track_id": track_id,
            "count": len(rows),
            "limit": want,
            "truncated": len(rows) >= want,
            "points": [_history_point(r) for r in rows],
        }

    @app.websocket("/ws/v2")
    async def ws_v2(websocket: WebSocket) -> None:
        await websocket.accept()
        # Register before snapshotting so no delta is lost between the two (both are
        # synchronous — the bus cannot interleave until we await). The default
        # filter is the station-scoped one (PRD §16.3a) until the client subscribes.
        conn = hub.register(default_filter(cfg))
        try:
            await websocket.send_json(hub.snapshot_for(conn))
            sender = asyncio.create_task(_ws_send(websocket, conn))
            receiver = asyncio.create_task(_ws_receive(websocket, hub, conn, cfg))
            try:
                # Whichever finishes first (disconnect / error) tears the other down.
                _done, pending = await asyncio.wait(
                    [sender, receiver], return_when=asyncio.FIRST_COMPLETED
                )
                for task in pending:
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task
            finally:
                for task in (sender, receiver):
                    task.cancel()
        except WebSocketDisconnect:
            pass
        finally:
            hub.unregister(conn)

    return app


def _normalize_iso(value: str | None) -> str | None:
    """Normalize an ISO-8601 query bound to the store's canonical UTC-ISO form.

    The store compares ``observed_at`` lexically (UTC ISO with a fixed ``+00:00``
    offset), so a query bound must be converted to that exact shape to compare
    chronologically — any offset is converted to UTC and a naive instant is read as
    UTC. Returns ``None`` for ``None``; raises ``ValueError`` on an unparseable value
    (the endpoint maps that to HTTP 400).
    """
    if value is None:
        return None
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat()


def _history_point(row: ObservationRow) -> dict[str, Any]:
    """Project a persisted observation to a lightweight history-trail point.

    Only the indexed/flattened fields a trail or timeline needs — the full record
    JSON in ``payload`` is intentionally omitted so a long history stays small; the
    contributing ``source`` distinguishes local vs network points. Lossless
    reconstruction is replay's job (later M4 slice).
    """
    return {
        "observed_at": row.observed_at,
        "received_at": row.received_at,
        "source": row.source,
        "track_type": row.track_type,
        "lon": row.lon,
        "lat": row.lat,
        "alt_m": row.alt_m,
    }


async def _ensure_store(cfg: Settings) -> None:
    """Open the store once to apply migrations, then close (startup, blocking-safe).

    Runs in a worker thread and is exception-isolated: an unwritable/corrupt path
    logs and is skipped so a bad store degrades to no persistence rather than
    failing boot (PRD §5/§37). Idempotent — :meth:`Database.open` re-runs no applied
    migration, and the persistence writer reopens the same store harmlessly.
    """
    try:
        await asyncio.to_thread(_open_and_close, cfg.db_path)
    except Exception:
        log.warning("store schema init failed; continuing without persistence", exc_info=True)


def _open_and_close(db_path: str) -> None:
    db = Database(db_path)
    db.open()  # applies migrations
    db.close()


async def _seed_alert_rules(cfg: Settings) -> None:
    """Seed missing default alert-rule templates into the store (startup, blocking-safe).

    Idempotent and disabled-by-default (PRD §11.16 ALERT-FR-008): only template ids
    absent from the store are inserted, so an operator's edits survive and a re-seed
    is a no-op. Exception-isolated — a write error logs and startup continues
    (PRD §5/§37); the store is already migrated by :func:`_ensure_store`.
    """
    try:
        inserted = await asyncio.to_thread(
            seed_alert_rules, cfg.db_path, default_rule_templates(datetime.now(UTC))
        )
    except Exception:
        log.warning("alert-rule template seeding failed; continuing", exc_info=True)
        return
    if inserted:
        log.info("seeded %d default alert-rule template(s)", inserted)


async def _load_geofences(cfg: Settings, hub: Hub) -> None:
    """Publish every persisted geofence as a live overlay feature (startup, blocking-safe).

    The store read runs in a worker thread and the whole load is exception-isolated:
    a missing store yields an empty list (handled in :func:`list_geofences`), and a
    corrupt row or projection error logs and is skipped rather than wedging startup
    (PRD §5/§37). Idempotent — re-running just re-upserts the same feature ids.
    """
    try:
        geofences = await asyncio.to_thread(list_geofences, cfg.db_path)
    except Exception:  # corrupt store row etc. — start with no overlays, never crash
        log.warning("geofence startup load failed; continuing with none", exc_info=True)
        return
    for geofence in geofences:
        try:
            hub.publish(geofence.to_feature_record())
        except Exception:
            log.warning("skipping malformed geofence %s at startup", geofence.id, exc_info=True)


async def _ws_send(websocket: WebSocket, conn: Connection) -> None:
    """Drain this connection's queue to the socket — the sole owner of writes."""
    while True:
        await websocket.send_json(await conn.queue.get())


async def _ws_receive(websocket: WebSocket, hub: Hub, conn: Connection, cfg: Settings) -> None:
    """Handle ``subscribe`` frames: validate, rate-limit, re-snapshot (PRD §22.2).

    Every accepted subscribe is a resync point — :meth:`Hub.resubscribe` swaps the
    filter, resets ``cseq``, and rebuilds a filtered snapshot which we hand to the
    sender via the connection queue so all socket writes stay single-owner and
    ordered. A malformed frame is logged and the PRIOR filter kept; it never raises
    out of this loop (PRD §37). A server-side min-interval guard means subscribe
    spam can validate but cannot force re-snapshot churn that starves the sender.
    """
    last_accept = -SUBSCRIBE_MIN_INTERVAL_S
    while True:
        try:
            frame = await websocket.receive_json()
        except WebSocketDisconnect:
            return
        except Exception:
            # A non-JSON / oversized text frame must not kill the connection.
            log.debug("ignoring unparseable ws frame", exc_info=True)
            continue
        new_filter = parse_subscribe(frame, cfg)
        if new_filter is None:
            continue  # malformed → keep the prior filter (already logged)
        loop_now = asyncio.get_event_loop().time()
        if loop_now - last_accept < SUBSCRIBE_MIN_INTERVAL_S:
            # Valid but too soon: keep the filter (so the latest intent wins) but
            # skip the expensive re-snapshot to protect the send loop.
            conn.filter = new_filter
            continue
        last_accept = loop_now
        # Route through the hub's drop-oldest enqueue so a momentarily-full queue
        # can't raise out of the receive loop (PRD §37). The fresh snapshot is the
        # resync baseline regardless of which older frames get dropped.
        hub.enqueue(conn, hub.resubscribe(conn, new_filter))


async def _run_expiry(hub: Hub) -> None:
    """Periodically age out stale fused tracks until cancelled.

    Exception-isolated per tick: a transient fusion/expiry error logs and the loop
    continues rather than wedging the backend (PRD §37 failure isolation).
    """
    while True:
        await asyncio.sleep(EXPIRY_INTERVAL_S)
        try:
            hub.expire(datetime.now(UTC))
        except Exception:  # one bad sweep must not kill the expiry loop
            log.warning("track expiry sweep failed; continuing", exc_info=True)


async def _run_demo(cfg: Settings, ready: asyncio.Event, interval_s: float) -> None:
    """Publish the demo stream once the subscriber is live (avoids a startup race).

    Reconnects on broker errors with the same backoff as the subscriber, so a
    broker blip degrades the demo feed rather than crashing the lifespan.
    """
    await ready.wait()
    while True:
        try:
            async with connect(cfg, identifier="aether-demo-publisher") as bus:
                await run_demo_publisher(bus, interval_s=interval_s)
        except aiomqtt.MqttError:
            log.warning("demo publisher lost broker; reconnecting in %.0fs", DEFAULT_RECONNECT_S)
            await asyncio.sleep(DEFAULT_RECONNECT_S)


app = create_app()

"""Connection hub: owns live state and fans changes out to websocket clients.

Each client gets a bounded async queue plus a per-connection :class:`ClientFilter`
and a contiguous ``cseq`` counter (PRD §16.3, §22). The global ``seq`` (see
``state/sequence.py``) bumps on every mutation, so a filtered client would see seq
5 then 9 and false-trigger the §22.5 resync on every filtered frame. The fix:
gap-detect on the PER-CONNECTION ``cseq`` instead. A frame the connection's filter
rejects gets no ``cseq`` (no false gap); a real drop-oldest in :meth:`_enqueue`
leaves a ``cseq`` gap exactly when frames were truly dropped (correct resync) —
cleanly separating "filtered (expected)" from "dropped (resync)".

A ``remove`` for an id this connection has actually sent is force-forwarded
regardless of the filter (then forgotten), so a filtered client never strands a
ghost track — the same bug-class ``state/live.py`` already guards for fusion-id
mismatches.

When a client's queue fills, the hub drops that client's oldest queued frame to
make room; the resulting ``cseq`` gap makes the client resynchronize (§22.5)
instead of blocking ingestion for everyone — "one slow browser must not block
ingestion".

A single asyncio loop owns this object and ``publish`` never awaits, so iterating
clients during a broadcast is safe without locks.
"""

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from aether.backend.protocol import delta_message, snapshot_message
from aether.backend.subscription import ClientFilter
from aether.schema.records import AlertRecord, Record
from aether.state.live import LiveState, StateChange, StateKind

log = logging.getLogger(__name__)

#: Per-client outbound queue depth before back-pressure kicks in.
CLIENT_QUEUE_MAXSIZE = 1000

ClientQueue = asyncio.Queue[dict[str, Any]]


@dataclass(eq=False)  # identity-hashed so connections live in a set
class Connection:
    """Per-websocket state owned by the hub.

    ``cseq`` is the contiguous per-connection counter the client gap-detects on;
    ``sent_ids`` records which record ids this connection has actually been sent an
    upsert for, so a later ``remove`` for a now-filtered-out id is still forwarded
    (no ghost track). ``filter`` is swapped wholesale on every (re)subscribe.
    """

    queue: ClientQueue
    filter: ClientFilter
    cseq: int = 0
    sent_ids: set[str] = field(default_factory=set)

    def next_cseq(self) -> int:
        self.cseq += 1
        return self.cseq


class Hub:
    def __init__(
        self,
        state: LiveState | None = None,
        *,
        client_queue_maxsize: int = CLIENT_QUEUE_MAXSIZE,
    ) -> None:
        self._state = state if state is not None else LiveState()
        self._maxsize = client_queue_maxsize
        self._clients: set[Connection] = set()
        self._observers: list[Callable[[StateChange], None]] = []

    @property
    def state(self) -> LiveState:
        return self._state

    @property
    def client_count(self) -> int:
        return len(self._clients)

    def register(self, client_filter: ClientFilter) -> Connection:
        """Register a new connection with its initial (default station) filter."""
        conn = Connection(queue=asyncio.Queue(maxsize=self._maxsize), filter=client_filter)
        self._clients.add(conn)
        return conn

    def unregister(self, conn: Connection) -> None:
        self._clients.discard(conn)

    def add_observer(self, observer: Callable[[StateChange], None]) -> None:
        """Register a post-broadcast hook called once per state change.

        Each observer runs *after* a change has fanned out to clients, so an observer
        that mutates state in turn (e.g. the alert engine publishing an alert) cannot
        race the broadcast it is reacting to. Observers are error-isolated in
        :meth:`_notify` — a raising observer never aborts the fan-out (PRD §37).
        """
        self._observers.append(observer)

    def snapshot_for(self, conn: Connection) -> dict[str, Any]:
        """Build a fresh filtered snapshot for ``conn`` and re-anchor it.

        Every subscribe (initial / widened bbox / reconnect) is a resync point
        (PRD §22.5): the snapshot is filtered by the connection's CURRENT filter,
        ``cseq`` resets to 0, and ``sent_ids`` is rebuilt from exactly what the
        snapshot contains — so subsequent removes for those ids are force-forwarded
        and removes for ids never sent are correctly dropped.
        """
        conn.cseq = 0
        snapshot = self._state.snapshot()
        tracks = [r for r in snapshot.tracks if conn.filter.matches_record("track", r)]
        features = [r for r in snapshot.features if conn.filter.matches_record("feature", r)]
        events = (
            [r for r in snapshot.events if conn.filter.matches_record("event", r)]
            if conn.filter.include_events
            else []
        )
        alerts = snapshot.alerts if conn.filter.include_alerts else []
        snapshot.tracks = tracks
        snapshot.features = features
        snapshot.events = events
        snapshot.alerts = alerts
        # source_status always passes the filter (health reaches every client).
        conn.sent_ids = {r.id for r in tracks} | {r.id for r in features} | {r.id for r in alerts}
        return snapshot_message(snapshot, cseq=conn.cseq)

    def resubscribe(self, conn: Connection, client_filter: ClientFilter) -> dict[str, Any]:
        """Swap a connection's filter and return a fresh filtered snapshot frame."""
        conn.filter = client_filter
        return self.snapshot_for(conn)

    def enqueue(self, conn: Connection, message: dict[str, Any]) -> None:
        """Enqueue a pre-built frame (e.g. a resubscribe snapshot) with drop-oldest."""
        self._enqueue(conn, message)

    def publish(self, record: Record) -> None:
        """Apply a record to live state and broadcast the resulting delta.

        The wall clock is read once here, at the I/O edge, and passed into
        ``apply`` so fusion freshness is measured against real time while the
        fusion core itself stays clock-free and deterministic (FUSION-FR-007).
        """
        now = datetime.now(UTC)
        change = self._state.apply(record, now)
        for conn in self._clients:
            self._dispatch(conn, change)
        self._notify(change)

    def remove(self, kind: StateKind, id: str) -> None:
        """Remove a track/feature/alert by id and broadcast the removal.

        Symmetric with :meth:`publish` for records the backend mutates directly
        rather than receiving over the bus — e.g. an operator deleting a geofence
        feature via the REST API. The Hub force-forwards the remove to every client
        that was sent the id (so no ghost overlay is stranded, PRD §22.4).
        """
        change = self._state.remove(kind, id)
        for conn in self._clients:
            self._dispatch(conn, change)
        self._notify(change)

    def expire(self, now: datetime) -> None:
        """Age out stale tracks/features and broadcast each resulting delta.

        Driven by a periodic backend task (not the bus), this is what surfaces a
        fused track's LOCAL→NET handoff and removes tracks once every contributor
        has gone silent (PRD §15.4, FUSION-FR-004).
        """
        for change in self._state.expire(now):
            for conn in self._clients:
                self._dispatch(conn, change)
            self._notify(change)

    def transition_alert(
        self, alert_id: str, to_state: Literal["acknowledged", "resolved"], now: datetime
    ) -> AlertRecord | None:
        """Move a live alert to ``acknowledged``/``resolved`` and rebroadcast it.

        Backs ``POST /api/v2/alerts/{id}/acknowledge|resolve`` (PRD §21.4, §20.5).
        Returns the updated alert, or ``None`` if no live alert has that id (the API
        maps that to 404). Stamps the matching lifecycle timestamp (``acknowledged_at``
        is set only on the first ack so a re-ack is idempotent), then re-applies the
        alert as an upsert so every client and observer sees the new state. In-memory
        only — no store I/O — so it never blocks the loop.
        """
        existing = self._state.get_alert(alert_id)
        if existing is None:
            return None
        updates: dict[str, Any] = {"state": to_state, "published_at": now}
        if to_state == "acknowledged" and existing.acknowledged_at is None:
            updates["acknowledged_at"] = now
        if to_state == "resolved":
            updates["resolved_at"] = now
        updated = existing.model_copy(update=updates)
        change = self._state.apply(updated, now)
        for conn in self._clients:
            self._dispatch(conn, change)
        self._notify(change)
        return updated

    def _notify(self, change: StateChange) -> None:
        """Run every observer for one change, isolating failures (PRD §37).

        A raising observer is logged and skipped so neither the other observers nor
        the broadcast that preceded it are affected.
        """
        for observer in self._observers:
            try:
                observer(change)
            except Exception:  # one bad observer must not break the others / the loop
                log.warning("hub observer raised; skipping", exc_info=True)

    def _dispatch(self, conn: Connection, change: StateChange) -> None:
        """Filter, cseq-stamp, and enqueue one change for one connection.

        Filtering happens BEFORE ``_enqueue`` so a filtered frame consumes neither
        queue depth nor a ``cseq``. A ``remove`` for an id the connection was sent
        is force-forwarded (then forgotten) so the client never strands a ghost.
        ``filter.matches`` is wrapped per PRD §37: a bad predicate skips this one
        frame for this one client, never aborting the broadcast for the others.
        """
        if change.op == "remove":
            # A remove is decided ENTIRELY by sent_ids: never run it through
            # matches() (which returns True unconditionally for record-less removes
            # and would force-forward removes for ids this connection never saw,
            # burning a cseq on every AOI-wide expire()). Force-forward only the
            # removes we actually sent; drop the rest with no enqueue, no cseq.
            if change.id in conn.sent_ids:
                conn.sent_ids.discard(change.id)
                self._enqueue(conn, delta_message(change, cseq=conn.next_cseq()))
            return
        try:
            wanted = conn.filter.matches(change)
        except Exception:  # one bad predicate must not abort the fan-out (PRD §37)
            log.warning("client filter raised; skipping frame for one client", exc_info=True)
            return
        if not wanted:
            return
        # Only record kinds that can later produce a remove (track/feature/alert);
        # events/source_status are append-only and never removed, so tracking their
        # ids would grow sent_ids unbounded on a long-lived connection (PRD §37).
        if change.kind in ("track", "feature", "alert"):
            conn.sent_ids.add(change.id)
        self._enqueue(conn, delta_message(change, cseq=conn.next_cseq()))

    def _enqueue(self, conn: Connection, message: dict[str, Any]) -> None:
        queue = conn.queue
        try:
            queue.put_nowait(message)
        except asyncio.QueueFull:
            try:
                queue.get_nowait()  # drop oldest; client will see a cseq gap and resync
            except asyncio.QueueEmpty:  # pragma: no cover - racy, defensive
                pass
            queue.put_nowait(message)

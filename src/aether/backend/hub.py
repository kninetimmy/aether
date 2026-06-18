"""Connection hub: owns live state and fans changes out to websocket clients.

Each client gets a bounded async queue (PRD §22.6). When a client's queue fills,
the hub drops that client's oldest queued frame to make room; the resulting
sequence gap makes the client resynchronize (§22.5) instead of blocking
ingestion for everyone — "one slow browser must not block ingestion".

A single asyncio loop owns this object and ``publish`` never awaits, so iterating
clients during a broadcast is safe without locks.
"""

import asyncio
from datetime import UTC, datetime
from typing import Any

from aether.backend.protocol import delta_message
from aether.schema.records import Record
from aether.state.live import LiveState

#: Per-client outbound queue depth before back-pressure kicks in.
CLIENT_QUEUE_MAXSIZE = 1000

ClientQueue = asyncio.Queue[dict[str, Any]]


class Hub:
    def __init__(
        self,
        state: LiveState | None = None,
        *,
        client_queue_maxsize: int = CLIENT_QUEUE_MAXSIZE,
    ) -> None:
        self._state = state if state is not None else LiveState()
        self._maxsize = client_queue_maxsize
        self._clients: set[ClientQueue] = set()

    @property
    def state(self) -> LiveState:
        return self._state

    @property
    def client_count(self) -> int:
        return len(self._clients)

    def register(self) -> ClientQueue:
        queue: ClientQueue = asyncio.Queue(maxsize=self._maxsize)
        self._clients.add(queue)
        return queue

    def unregister(self, queue: ClientQueue) -> None:
        self._clients.discard(queue)

    def publish(self, record: Record) -> None:
        """Apply a record to live state and broadcast the resulting delta.

        The wall clock is read once here, at the I/O edge, and passed into
        ``apply`` so fusion freshness is measured against real time while the
        fusion core itself stays clock-free and deterministic (FUSION-FR-007).
        """
        now = datetime.now(UTC)
        change = self._state.apply(record, now)
        message = delta_message(change)
        for queue in self._clients:
            self._enqueue(queue, message)

    def expire(self, now: datetime) -> None:
        """Age out stale tracks/features and broadcast each resulting delta.

        Driven by a periodic backend task (not the bus), this is what surfaces a
        fused track's LOCAL→NET handoff and removes tracks once every contributor
        has gone silent (PRD §15.4, FUSION-FR-004).
        """
        for change in self._state.expire(now):
            message = delta_message(change)
            for queue in self._clients:
                self._enqueue(queue, message)

    def _enqueue(self, queue: ClientQueue, message: dict[str, Any]) -> None:
        try:
            queue.put_nowait(message)
        except asyncio.QueueFull:
            try:
                queue.get_nowait()  # drop oldest; client will see a gap and resync
            except asyncio.QueueEmpty:  # pragma: no cover - racy, defensive
                pass
            queue.put_nowait(message)

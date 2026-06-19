"""Alert notification dispatch (M4.7a, PRD §20.4, §20.5).

The alert engine stamps each rule-selected channel ``pending`` in an alert's
``delivery_status`` and publishes the alert into live state; this module is what
*resolves* those pending entries. A :class:`NotificationDispatcher` observes
newly-published open alerts and, **off the hot path**, settles each channel:

- ``dashboard`` is the in-app alert centre — delivered by the very act of the alert
  reaching live state, so the engine already stamps it ``delivered`` at creation and
  the dispatcher never touches it.
- ``browser`` is the client-side Notifications API (PRD §20.4 browser MVP). The
  backend's responsibility is the per-channel severity threshold: it marks the
  channel ``delivered`` (the frontend fires the Notification when the operator has
  granted permission and the app is open) or ``suppressed`` when the alert is below
  the browser threshold. The frontend wiring is a later UI slice.
- ``email`` / ``discord`` are server-side drivers (SMTP / webhook). The injection
  seam — a ``channel -> driver`` map — is here, but the drivers themselves land in
  M4.7b. Until a driver is wired, a selected email/discord channel that clears its
  threshold resolves to ``unconfigured`` (honest: the operator asked for it but no
  transport is configured); below threshold it resolves to ``suppressed``.

**Never gates serving live state (PRD §5).** :meth:`observe` is the synchronous hub
observer and only *enqueues* onto a bounded queue (drop-oldest, never blocks); the
actual resolution + write-back runs in the sibling :meth:`run` task on the same loop.
With nothing to settle (every channel already resolved) the dispatcher is inert.

**No dispatch loop.** Write-back re-publishes the updated alert through the same hub
publish path. The engine never drives a rule off an alert change, and the
re-published alert has no remaining ``pending`` channels, so :meth:`observe` ignores
it — the cycle terminates in one pass.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from aether.schema.records import AlertRecord
from aether.state.live import StateChange

log = logging.getLogger(__name__)

#: Severity ladder mirroring :data:`aether.schema.alert_rule.AlertSeverity`. An alert
#: is delivered on a channel only when its severity rank meets the channel threshold.
_SEVERITY_RANK: dict[str, int] = {"info": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}

#: Channels delivered by the browser/dashboard client transports — no server-side
#: driver, no external I/O. ``dashboard`` is settled by the engine at creation; the
#: dispatcher only ever resolves ``browser`` (a pure severity-threshold decision).
CLIENT_CHANNELS: frozenset[str] = frozenset({"dashboard", "browser"})

#: Channels delivered by a server-side driver (SMTP / webhook), wired in M4.7b.
SERVER_CHANNELS: frozenset[str] = frozenset({"email", "discord"})

#: Bounded dispatch queue depth. A burst of alerts beyond this drops the *oldest*
#: queued alert (drop-oldest) rather than back-pressuring the hub — delivery is a
#: best-effort sibling of live state, never a gate on it (PRD §5, §37).
DEFAULT_QUEUE_MAXSIZE = 1000


def meets_threshold(severity: str, threshold: str) -> bool:
    """True when ``severity`` is at least as severe as the channel ``threshold``.

    Unknown labels rank as ``info`` (0) so a misconfigured threshold fails *open*
    (delivers) rather than silently swallowing every alert.
    """
    return _SEVERITY_RANK.get(severity, 0) >= _SEVERITY_RANK.get(threshold, 0)


@dataclass(frozen=True)
class ChannelThresholds:
    """Per-channel minimum severity to deliver (PRD §20.5). ``info`` ⇒ deliver all."""

    browser: str = "info"
    email: str = "info"
    discord: str = "info"

    def for_channel(self, channel: str) -> str:
        return getattr(self, channel, "info")


class NotificationDriver:
    """Server-side delivery driver protocol (SMTP/webhook), implemented in M4.7b.

    A driver attempts to deliver one alert and returns ``True`` on success. It must
    never raise out of :meth:`deliver` for an ordinary delivery failure — the
    dispatcher records a ``failed`` status — and must never log secrets.
    """

    async def deliver(self, alert: AlertRecord) -> bool:  # pragma: no cover - protocol
        raise NotImplementedError


class NotificationDispatcher:
    """Settle the ``delivery_status`` of newly-fired alerts off the hot path."""

    def __init__(
        self,
        publish: Callable[[AlertRecord], None],
        *,
        clock: Callable[[], datetime],
        thresholds: ChannelThresholds | None = None,
        drivers: dict[str, NotificationDriver] | None = None,
        queue_maxsize: int = DEFAULT_QUEUE_MAXSIZE,
    ) -> None:
        self._publish = publish
        self._clock = clock
        self._thresholds = thresholds or ChannelThresholds()
        self._drivers = dict(drivers or {})
        self._queue: asyncio.Queue[AlertRecord] = asyncio.Queue(maxsize=queue_maxsize)

    # -- hub observer (synchronous, hot path) --------------------------------

    def observe(self, change: StateChange) -> None:
        """Enqueue an open alert that still has channels awaiting delivery.

        The sole synchronous touchpoint on the publish path: it inspects the change
        and, at most, does one non-blocking enqueue. Everything else — threshold
        checks, driver I/O, write-back — happens in :meth:`run`. A re-published alert
        whose channels are already settled has no ``pending`` entry, so it is ignored
        and the dispatch cycle cannot recur.
        """
        if change.op != "upsert" or change.kind != "alert":
            return
        alert = change.record
        if not isinstance(alert, AlertRecord) or alert.state != "open":
            return
        if not any(status == "pending" for status in alert.delivery_status.values()):
            return
        self._enqueue(alert)

    def _enqueue(self, alert: AlertRecord) -> None:
        """Bounded, drop-oldest enqueue (mirrors the hub's client-queue policy)."""
        try:
            self._queue.put_nowait(alert)
        except asyncio.QueueFull:
            try:
                self._queue.get_nowait()  # drop oldest; delivery is best-effort
            except asyncio.QueueEmpty:  # pragma: no cover - racy, defensive
                pass
            self._queue.put_nowait(alert)

    # -- sibling delivery task (async, off the hot path) ---------------------

    async def run(self) -> None:
        """Drain the queue forever, settling each alert's pending channels.

        One bad dispatch is isolated (PRD §37): it is logged and the loop continues,
        so a single malformed alert or a driver bug never wedges delivery for the rest.
        """
        while True:
            alert = await self._queue.get()
            try:
                await self._dispatch_one(alert)
            except Exception:  # one bad alert must not kill the delivery loop
                log.warning(
                    "notification dispatch failed for alert %s; continuing",
                    alert.id,
                    exc_info=True,
                )

    async def drain(self) -> list[AlertRecord]:
        """Process every *currently* queued alert once; return the updated alerts.

        Used by tests to run the dispatcher deterministically without the forever
        loop; harmless in production (the run loop simply finds the queue empty).
        """
        out: list[AlertRecord] = []
        while True:
            try:
                alert = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                return out
            updated = await self._dispatch_one(alert)
            if updated is not None:
                out.append(updated)

    async def _dispatch_one(self, alert: AlertRecord) -> AlertRecord | None:
        """Resolve every pending channel of one alert and write the result back.

        Returns the updated alert (and re-publishes it) when at least one channel was
        settled, else ``None`` (nothing to do — a no-op, no redundant broadcast).
        """
        updates: dict[str, str] = {}
        for channel, status in alert.delivery_status.items():
            if status != "pending":
                continue
            updates[channel] = await self._resolve_channel(channel, alert)
        if not updates:
            return None
        new_status = {**alert.delivery_status, **updates}
        updated = alert.model_copy(
            update={"delivery_status": new_status, "published_at": self._clock()}
        )
        self._publish(updated)
        return updated

    async def _resolve_channel(self, channel: str, alert: AlertRecord) -> str:
        """Settle one channel to a terminal ``delivery_status`` value.

        ``delivered`` (sent / handed to the client transport), ``suppressed`` (below
        the channel's severity threshold), ``failed`` (driver attempted and gave up),
        or ``unconfigured`` (a server-side channel with no driver wired yet).
        """
        if channel == "dashboard":
            return "delivered"  # defensive: the engine already pre-delivers dashboard
        if not meets_threshold(alert.severity, self._thresholds.for_channel(channel)):
            return "suppressed"
        if channel == "browser":
            return "delivered"  # client transport; the frontend fires the Notification
        driver = self._drivers.get(channel)
        if driver is None:
            return "unconfigured"  # server-side channel selected, no transport (pre-M4.7b)
        ok = await driver.deliver(alert)
        return "delivered" if ok else "failed"

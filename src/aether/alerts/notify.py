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
- ``email`` / ``discord`` are server-side drivers (:class:`EmailDriver` over stdlib
  SMTP, :class:`DiscordDriver` over a webhook POST; M4.7b). They plug into the
  ``channel -> driver`` map; a cleared-threshold channel resolves to ``delivered`` /
  ``failed`` per the driver, or ``unconfigured`` when no driver is wired (honest: the
  operator asked for it but no transport is configured); below threshold it resolves
  to ``suppressed``. Drivers never raise out of :meth:`NotificationDriver.deliver` and
  never log a credential (PRD §20.4): the SMTP password is dropped, the Discord
  webhook URL is redacted to scheme+host.

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
import json
import logging
import smtplib
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from email.message import EmailMessage

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

    @property
    def target(self) -> str:  # pragma: no cover - overridden by concrete drivers
        """Credential-free destination label for logs / the test endpoint."""
        return "(server driver)"


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

    # -- test-notification path (read-only sibling of the live dispatch) ------

    async def resolve_channels(self, alert: AlertRecord) -> dict[str, str]:
        """Resolve every channel of ``alert`` to a terminal status WITHOUT publishing.

        The live path (:meth:`_dispatch_one`) settles only *pending* channels and
        writes the result back into live state. This is the read-only sibling the test
        endpoint (``POST /api/v2/notifications/test``, PRD §21.4) uses: it fires a
        synthetic alert through the *real* drivers + thresholds and reports each
        channel's outcome, with no ``hub.publish`` and no effect on live state (PRD §5).
        """
        return {
            channel: await self._resolve_channel(channel, alert)
            for channel in alert.delivery_status
        }

    def target_for(self, channel: str) -> str:
        """Credential-free destination label for ``channel`` (test endpoint / logs).

        A wired server driver supplies its own redacted :attr:`NotificationDriver.target`
        (SMTP host+recipient, Discord scheme+host — never a password or webhook token);
        client channels describe their transport; an unwired server channel says so.
        """
        driver = self._drivers.get(channel)
        if driver is not None:
            return driver.target
        if channel in CLIENT_CHANNELS:
            return f"{channel} (client transport)"
        return "(no driver configured)"

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
            return "unconfigured"  # server-side channel selected, no transport configured
        ok = await driver.deliver(alert)
        return "delivered" if ok else "failed"


# === server-side delivery drivers (M4.7b, PRD §20.4) ========================

#: Transient-failure retries and base linear backoff shared by both server drivers.
#: Delivery is a best-effort sibling of live state (PRD §5): a couple of quick retries
#: ride out a flaky relay/webhook without holding the dispatch queue for long.
DEFAULT_DELIVERY_RETRIES = 2
DEFAULT_DELIVERY_BACKOFF_S = 1.0
DEFAULT_DELIVERY_TIMEOUT_S = 10.0

#: Discord embed accent colour per severity (decimal RGB) — purely cosmetic.
_DISCORD_COLORS: dict[str, int] = {
    "info": 0x3498DB,
    "low": 0x2ECC71,
    "medium": 0xF1C40F,
    "high": 0xE67E22,
    "critical": 0xE74C3C,
}


@dataclass(frozen=True)
class _Attempt:
    """Outcome of one delivery attempt: succeeded, or failed (retryable or not)."""

    ok: bool
    retryable: bool = False


def _exc_label(exc: BaseException) -> str:
    """Exception class name only — never the message, which may carry a credential
    (e.g. an :class:`urllib.error.HTTPError` whose ``url`` is the webhook token)."""
    return type(exc).__name__


async def _retry(
    attempt: Callable[[AlertRecord], _Attempt],
    alert: AlertRecord,
    *,
    retries: int,
    backoff_s: float,
) -> bool:
    """Run ``attempt`` (a blocking send) in a worker thread, retrying transient fails.

    Each call hops to a thread so SMTP/HTTP I/O never touches the event loop. A
    non-retryable failure (auth/recipient rejection, 4xx) stops at once; a retryable
    one (network blip, 429/5xx) is retried up to ``retries`` times with linear backoff.
    Returns the final success/failure as a plain ``bool`` for :meth:`deliver`.
    """
    for i in range(retries + 1):
        result = await asyncio.to_thread(attempt, alert)
        if result.ok:
            return True
        if not result.retryable or i == retries:
            return False
        if backoff_s > 0:
            await asyncio.sleep(backoff_s * (i + 1))
    return False  # pragma: no cover - loop always returns inside


def _alert_text(alert: AlertRecord) -> str:
    """Plain-text body shared by email (and the basis of the Discord embed)."""
    lines = [
        alert.summary,
        "",
        f"Severity: {alert.severity}",
        f"State: {alert.state}",
        f"Triggered: {alert.triggered_at.isoformat()}",
    ]
    if alert.subject_id:
        lines.append(f"Subject: {alert.subject_id}")
    lines += [
        "",
        f"Alert ID: {alert.id}",
        f"Rule: {alert.rule_id}",
        "",
        "— aether COP (not authoritative for any life-safety or operational use)",
    ]
    return "\n".join(lines)


def redact_webhook(url: str) -> str:
    """Mask a Discord webhook URL's secret token for logs/API (PRD §20.4).

    Keeps the scheme + host (useful for debugging which endpoint was hit) and drops
    the ``/api/webhooks/{id}/{token}`` tail, so the token never reaches a log line or
    API response. A URL we can't even parse collapses to ``<redacted>``.
    """
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError:
        return "<redacted>"
    if not parsed.scheme or not parsed.netloc:
        return "<redacted>"
    return f"{parsed.scheme}://{parsed.netloc}/…"


@dataclass(frozen=True)
class EmailConfig:
    """Resolved SMTP settings for :class:`EmailDriver` (PRD §20.4 email).

    ``password`` is excluded from ``repr`` so an accidental log/repr of the config can
    never leak the SMTP credential — the driver stores the *delivery result*, not the
    password (PRD §20.4).
    """

    host: str
    sender: str
    recipient: str
    port: int = 587
    tls: str = "starttls"  # "starttls" | "ssl" | "none"
    username: str = ""
    password: str = field(default="", repr=False)


class EmailDriver(NotificationDriver):
    """Deliver an alert as a plain-text email over stdlib SMTP (PRD §20.4).

    The blocking SMTP exchange runs in a worker thread; transient errors (network /
    server) retry, while a credential or recipient rejection fails fast. The password
    is never logged — only the exception *class* is, and only the host/port/recipient
    appear in :attr:`target`.
    """

    def __init__(
        self,
        config: EmailConfig,
        *,
        retries: int = DEFAULT_DELIVERY_RETRIES,
        backoff_s: float = DEFAULT_DELIVERY_BACKOFF_S,
        timeout_s: float = DEFAULT_DELIVERY_TIMEOUT_S,
    ) -> None:
        self._cfg = config
        self._retries = retries
        self._backoff_s = backoff_s
        self._timeout_s = timeout_s

    @property
    def target(self) -> str:
        return f"smtp://{self._cfg.host}:{self._cfg.port} → {self._cfg.recipient}"

    async def deliver(self, alert: AlertRecord) -> bool:
        return await _retry(self._attempt, alert, retries=self._retries, backoff_s=self._backoff_s)

    def _attempt(self, alert: AlertRecord) -> _Attempt:
        try:
            self._send(self._build_message(alert))
        except (
            smtplib.SMTPAuthenticationError,
            smtplib.SMTPSenderRefused,
            smtplib.SMTPRecipientsRefused,
        ) as exc:
            log.warning("email delivery rejected for alert %s: %s", alert.id, _exc_label(exc))
            return _Attempt(ok=False, retryable=False)
        except (smtplib.SMTPException, OSError) as exc:
            log.warning("email delivery error for alert %s: %s", alert.id, _exc_label(exc))
            return _Attempt(ok=False, retryable=True)
        return _Attempt(ok=True)

    def _build_message(self, alert: AlertRecord) -> EmailMessage:
        msg = EmailMessage()
        msg["Subject"] = f"[aether {alert.severity.upper()}] {alert.title}"
        msg["From"] = self._cfg.sender
        msg["To"] = self._cfg.recipient
        msg.set_content(_alert_text(alert))
        return msg

    def _send(self, msg: EmailMessage) -> None:
        cfg = self._cfg
        smtp: smtplib.SMTP
        if cfg.tls == "ssl":
            smtp = smtplib.SMTP_SSL(cfg.host, cfg.port, timeout=self._timeout_s)
        else:
            smtp = smtplib.SMTP(cfg.host, cfg.port, timeout=self._timeout_s)
        try:
            if cfg.tls == "starttls":
                smtp.starttls()
            if cfg.username:
                smtp.login(cfg.username, cfg.password)
            smtp.send_message(msg)
        finally:
            try:
                smtp.quit()
            except smtplib.SMTPException:  # pragma: no cover - cleanup best-effort
                pass


class DiscordDriver(NotificationDriver):
    """Deliver an alert as a concise Discord embed via an incoming webhook (PRD §20.4).

    The webhook POST runs in a worker thread; HTTP 429 / 5xx and network errors retry,
    other 4xx fail fast. The webhook URL — which carries the secret token — is never
    logged or surfaced in full: logs and :attr:`target` use :func:`redact_webhook`.
    """

    def __init__(
        self,
        webhook_url: str,
        *,
        retries: int = DEFAULT_DELIVERY_RETRIES,
        backoff_s: float = DEFAULT_DELIVERY_BACKOFF_S,
        timeout_s: float = DEFAULT_DELIVERY_TIMEOUT_S,
    ) -> None:
        self._url = webhook_url
        self._retries = retries
        self._backoff_s = backoff_s
        self._timeout_s = timeout_s

    @property
    def target(self) -> str:
        return f"discord webhook {redact_webhook(self._url)}"

    async def deliver(self, alert: AlertRecord) -> bool:
        return await _retry(self._attempt, alert, retries=self._retries, backoff_s=self._backoff_s)

    def _attempt(self, alert: AlertRecord) -> _Attempt:
        data = json.dumps(self._build_payload(alert)).encode("utf-8")
        req = urllib.request.Request(
            self._url,
            data=data,
            headers={"Content-Type": "application/json", "User-Agent": "aether-cop"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout_s) as resp:
                code = int(resp.status)
        except urllib.error.HTTPError as exc:
            retryable = exc.code == 429 or exc.code >= 500
            log.warning(
                "discord delivery HTTP %d for alert %s (%s)",
                exc.code,
                alert.id,
                redact_webhook(self._url),
            )
            return _Attempt(ok=False, retryable=retryable)
        except (urllib.error.URLError, OSError) as exc:
            log.warning(
                "discord delivery error for alert %s: %s (%s)",
                alert.id,
                _exc_label(exc),
                redact_webhook(self._url),
            )
            return _Attempt(ok=False, retryable=True)
        if 200 <= code < 300:
            return _Attempt(ok=True)
        return _Attempt(ok=False, retryable=code == 429 or code >= 500)  # pragma: no cover

    def _build_payload(self, alert: AlertRecord) -> dict[str, object]:
        fields: list[dict[str, object]] = [
            {"name": "Severity", "value": alert.severity, "inline": True},
            {"name": "State", "value": alert.state, "inline": True},
        ]
        if alert.subject_id:
            fields.append({"name": "Subject", "value": alert.subject_id, "inline": False})
        return {
            "embeds": [
                {
                    "title": alert.title[:256],
                    "description": alert.summary[:2048],
                    "color": _DISCORD_COLORS.get(alert.severity, _DISCORD_COLORS["info"]),
                    "timestamp": alert.triggered_at.isoformat(),
                    "fields": fields,
                }
            ]
        }


def drivers_from_settings(
    *,
    smtp_host: str,
    smtp_port: int,
    smtp_tls: str,
    smtp_username: str,
    smtp_password: str,
    email_from: str,
    email_to: str,
    discord_webhook_url: str,
) -> dict[str, NotificationDriver]:
    """Build the wired server-side drivers from resolved config (M4.7b).

    A channel is wired only when its credentials are present: email needs an SMTP host
    *and* both addresses; discord needs a webhook URL. An unwired channel is simply
    absent from the map, so the dispatcher resolves it to ``unconfigured`` — the
    operator sees a visible "no transport", never a crash (PRD §37).
    """
    drivers: dict[str, NotificationDriver] = {}
    if smtp_host and email_from and email_to:
        drivers["email"] = EmailDriver(
            EmailConfig(
                host=smtp_host,
                port=smtp_port,
                tls=smtp_tls,
                username=smtp_username,
                password=smtp_password,
                sender=email_from,
                recipient=email_to,
            )
        )
    if discord_webhook_url:
        drivers["discord"] = DiscordDriver(discord_webhook_url)
    return drivers


#: Channels a notification-test request may target (PRD §21.4): the client transports
#: plus the server drivers. ``dashboard`` is accepted but trivially resolves.
TESTABLE_CHANNELS: frozenset[str] = CLIENT_CHANNELS | SERVER_CHANNELS


def describe_targets(dispatcher: NotificationDispatcher, channels: Iterable[str]) -> dict[str, str]:
    """Credential-free destination label per channel, for the test-endpoint response."""
    return {channel: dispatcher.target_for(channel) for channel in channels}

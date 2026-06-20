"""Server-side notification drivers (M4.7b, PRD §20.4).

Exercises :class:`EmailDriver` (stdlib SMTP) and :class:`DiscordDriver` (webhook POST)
without any real network: the SMTP class and ``urlopen`` are monkeypatched with fakes.
Covers the success path, transient-retry vs fail-fast, and — the security-critical
contract — that the SMTP password and the Discord webhook token never reach a log line
or a public-facing string. Drivers are built with ``backoff_s=0`` so retries don't
sleep. Follows the repo's ``asyncio.run`` async-test convention.
"""

from __future__ import annotations

import asyncio
import logging
import smtplib
from datetime import UTC, datetime
from typing import Any

import pytest

from aether.alerts import notify
from aether.alerts.notify import (
    DiscordDriver,
    EmailConfig,
    EmailDriver,
    drivers_from_settings,
    redact_webhook,
)
from aether.schema.records import AlertRecord

T0 = datetime(2026, 6, 19, 12, 0, 0, tzinfo=UTC)

#: A realistic-looking webhook whose final path segment is the secret token.
_WEBHOOK = "https://discord.com/api/webhooks/123456789/SECRET-token-abcdEFGH"
_TOKEN = "SECRET-token-abcdEFGH"
_PASSWORD = "hunter2-smtp-pass"


def _alert(severity: str = "high") -> AlertRecord:
    return AlertRecord(
        id="alert-1",
        source="alert-engine",
        observed_at=T0,
        received_at=T0,
        published_at=T0,
        rule_id="rule-1",
        subject_id="aircraft:icao:abc",
        state="open",
        severity=severity,  # type: ignore[arg-type]
        title="Emergency squawk",
        summary="Emergency squawk — abc",
        triggered_at=T0,
        delivery_status={},
    )


# == EmailDriver ============================================================


class _FakeSMTP:
    """Records the SMTP exchange; raises a queued exception from ``send_message``."""

    def __init__(self, host: str, port: int, timeout: float | None = None) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.started_tls = False
        self.login_args: tuple[str, str] | None = None
        self.sent: Any = None
        self.quit_called = False
        self._raise: BaseException | None = None

    def starttls(self) -> None:
        self.started_tls = True

    def login(self, user: str, password: str) -> None:
        self.login_args = (user, password)

    def send_message(self, msg: Any) -> None:
        if self._raise is not None:
            raise self._raise
        self.sent = msg

    def quit(self) -> None:
        self.quit_called = True


def _smtp_factory(behaviors: list[BaseException | None]) -> tuple[Any, list[_FakeSMTP]]:
    """A drop-in for ``smtplib.SMTP``: the Nth connection's ``send_message`` raises
    ``behaviors[N]`` (or succeeds when ``None``)."""
    created: list[_FakeSMTP] = []

    def make(host: str, port: int, timeout: float | None = None) -> _FakeSMTP:
        smtp = _FakeSMTP(host, port, timeout)
        idx = len(created)
        if idx < len(behaviors):
            smtp._raise = behaviors[idx]
        created.append(smtp)
        return smtp

    return make, created


def _email_driver() -> EmailDriver:
    cfg = EmailConfig(
        host="smtp.example.test",
        sender="aether@example.test",
        recipient="ops@example.test",
        port=587,
        tls="starttls",
        username="aether",
        password=_PASSWORD,
    )
    return EmailDriver(cfg, backoff_s=0.0)


def test_email_delivered_runs_full_smtp_exchange(monkeypatch: pytest.MonkeyPatch) -> None:
    make, created = _smtp_factory([None])
    monkeypatch.setattr(notify.smtplib, "SMTP", make)

    ok = asyncio.run(_email_driver().deliver(_alert()))

    assert ok is True
    smtp = created[0]
    assert smtp.started_tls is True  # starttls mode
    assert smtp.login_args == ("aether", _PASSWORD)
    assert smtp.quit_called is True
    assert smtp.sent["Subject"] == "[aether HIGH] Emergency squawk"
    assert smtp.sent["To"] == "ops@example.test"


def test_email_ssl_mode_uses_smtp_ssl_and_skips_starttls(monkeypatch: pytest.MonkeyPatch) -> None:
    make, created = _smtp_factory([None])
    monkeypatch.setattr(notify.smtplib, "SMTP_SSL", make)
    cfg = EmailConfig(host="h", sender="a@b", recipient="c@d", tls="ssl")
    ok = asyncio.run(EmailDriver(cfg, backoff_s=0.0).deliver(_alert()))
    assert ok is True
    assert created[0].started_tls is False  # SSL connects encrypted; no STARTTLS


def test_email_retries_transient_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    make, created = _smtp_factory([smtplib.SMTPServerDisconnected("blip"), None])
    monkeypatch.setattr(notify.smtplib, "SMTP", make)
    ok = asyncio.run(_email_driver().deliver(_alert()))
    assert ok is True
    assert len(created) == 2  # first attempt failed transiently, second delivered


def test_email_rejection_fails_fast_without_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    make, created = _smtp_factory([smtplib.SMTPRecipientsRefused({})])
    monkeypatch.setattr(notify.smtplib, "SMTP", make)
    ok = asyncio.run(_email_driver().deliver(_alert()))
    assert ok is False
    assert len(created) == 1  # a recipient rejection is permanent — no retry


def test_email_gives_up_after_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    make, created = _smtp_factory([OSError("conn refused")] * 5)
    monkeypatch.setattr(notify.smtplib, "SMTP", make)
    ok = asyncio.run(EmailDriver(_email_driver()._cfg, retries=2, backoff_s=0.0).deliver(_alert()))
    assert ok is False
    assert len(created) == 3  # initial + 2 retries


def test_email_never_logs_password(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    make, _ = _smtp_factory([smtplib.SMTPAuthenticationError(535, b"bad creds")])
    monkeypatch.setattr(notify.smtplib, "SMTP", make)
    with caplog.at_level(logging.WARNING):
        ok = asyncio.run(_email_driver().deliver(_alert()))
    assert ok is False
    assert _PASSWORD not in caplog.text  # credential never reaches the log


def test_email_config_repr_hides_password() -> None:
    cfg = EmailConfig(host="h", sender="a@b", recipient="c@d", password=_PASSWORD)
    assert _PASSWORD not in repr(cfg)


# == DiscordDriver ==========================================================


class _FakeResponse:
    def __init__(self, status: int) -> None:
        self.status = status

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *_: object) -> bool:
        return False


def _urlopen_factory(behaviors: list[int | BaseException]) -> tuple[Any, list[Any]]:
    """A drop-in for ``urllib.request.urlopen``: the Nth call returns a response with
    status ``behaviors[N]`` (int) or raises it (Exception); the last entry repeats."""
    seen: list[Any] = []

    def make(req: Any, timeout: float | None = None) -> _FakeResponse:
        idx = len(seen)
        seen.append(req)
        beh = behaviors[idx] if idx < len(behaviors) else behaviors[-1]
        if isinstance(beh, BaseException):
            raise beh
        return _FakeResponse(beh)

    return make, seen


def _http_error(code: int) -> Any:
    import urllib.error

    # url carries the secret token — the driver must not log it.
    return urllib.error.HTTPError(_WEBHOOK, code, "err", {}, None)  # type: ignore[arg-type]


def test_discord_delivered_posts_embed(monkeypatch: pytest.MonkeyPatch) -> None:
    make, seen = _urlopen_factory([204])
    monkeypatch.setattr(notify.urllib.request, "urlopen", make)
    ok = asyncio.run(DiscordDriver(_WEBHOOK, backoff_s=0.0).deliver(_alert()))
    assert ok is True
    assert seen[0].method == "POST"
    body = seen[0].data.decode("utf-8")
    assert "Emergency squawk" in body  # the embed carries the alert title/summary


def test_discord_retries_rate_limited_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    make, seen = _urlopen_factory([_http_error(429), 204])
    monkeypatch.setattr(notify.urllib.request, "urlopen", make)
    ok = asyncio.run(DiscordDriver(_WEBHOOK, backoff_s=0.0).deliver(_alert()))
    assert ok is True
    assert len(seen) == 2  # 429 retried, second attempt delivered


def test_discord_client_error_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    make, seen = _urlopen_factory([_http_error(400)])
    monkeypatch.setattr(notify.urllib.request, "urlopen", make)
    ok = asyncio.run(DiscordDriver(_WEBHOOK, backoff_s=0.0).deliver(_alert()))
    assert ok is False
    assert len(seen) == 1  # a 4xx (≠429) is permanent — no retry


def test_discord_network_error_retries_and_gives_up(monkeypatch: pytest.MonkeyPatch) -> None:
    import urllib.error

    make, seen = _urlopen_factory([urllib.error.URLError("dns")])
    monkeypatch.setattr(notify.urllib.request, "urlopen", make)
    ok = asyncio.run(DiscordDriver(_WEBHOOK, retries=2, backoff_s=0.0).deliver(_alert()))
    assert ok is False
    assert len(seen) == 3  # initial + 2 retries


def test_discord_never_logs_webhook_token(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    make, _ = _urlopen_factory([_http_error(500)])
    monkeypatch.setattr(notify.urllib.request, "urlopen", make)
    with caplog.at_level(logging.WARNING):
        ok = asyncio.run(DiscordDriver(_WEBHOOK, retries=0, backoff_s=0.0).deliver(_alert()))
    assert ok is False
    assert _TOKEN not in caplog.text  # token redacted from the error log


# == redaction + wiring =====================================================


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (_WEBHOOK, "https://discord.com/…"),
        ("https://discord.com/api/webhooks/1/2", "https://discord.com/…"),
        ("not a url", "<redacted>"),
        ("", "<redacted>"),
    ],
)
def test_redact_webhook(url: str, expected: str) -> None:
    out = redact_webhook(url)
    assert out == expected
    assert _TOKEN not in out


def test_driver_targets_are_credential_free() -> None:
    email = EmailDriver(
        EmailConfig(host="smtp.x", sender="a@b", recipient="c@d", password=_PASSWORD)
    )
    discord = DiscordDriver(_WEBHOOK)
    assert _PASSWORD not in email.target
    assert "c@d" in email.target  # the recipient is fine to show
    assert _TOKEN not in discord.target
    assert "discord.com" in discord.target


def test_drivers_from_settings_wires_only_configured_channels() -> None:
    # Fully configured → both wired.
    both = drivers_from_settings(
        smtp_host="smtp.x",
        smtp_port=587,
        smtp_tls="starttls",
        smtp_username="u",
        smtp_password="p",
        email_from="a@b",
        email_to="c@d",
        discord_webhook_url=_WEBHOOK,
    )
    assert set(both) == {"email", "discord"}

    # Email missing a recipient → not wired; no webhook → discord not wired.
    partial = drivers_from_settings(
        smtp_host="smtp.x",
        smtp_port=587,
        smtp_tls="starttls",
        smtp_username="u",
        smtp_password="p",
        email_from="a@b",
        email_to="",
        discord_webhook_url="",
    )
    assert partial == {}

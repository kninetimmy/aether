"""Test-notification endpoint — ``POST /api/v2/notifications/test`` (PRD §21.4, §20.5).

Fires a *synthetic* alert through the operator-selected channels using the very same
:class:`~aether.alerts.notify.NotificationDispatcher` (its real drivers + per-channel
severity thresholds), so a successful test proves the live config actually works. It
is **isolated from live state** (PRD §5): resolution goes through
:meth:`NotificationDispatcher.resolve_channels`, which never publishes — the synthetic
alert never enters the hub, never reaches a client, never persists.

The response reports each channel's outcome plus a **credential-free** destination
label per channel (SMTP host+recipient, Discord scheme+host — never the password or
webhook token; PRD §20.4). The request never carries secrets either: channels are
chosen by name and the transport config comes from the server's environment.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field, field_validator

from aether.alerts.notify import TESTABLE_CHANNELS, NotificationDispatcher, describe_targets
from aether.schema.records import AlertRecord

#: A test always targets at least one channel; cap the list so a request can't ask for
#: an unbounded set (only four channels exist anyway).
_MAX_TEST_CHANNELS = 8


class NotificationTestRequest(BaseModel):
    """Which channels to fire the synthetic alert through, and at what severity.

    ``severity`` lets the operator confirm a per-channel threshold actually suppresses
    (e.g. send ``low`` and watch a ``critical``-gated channel resolve ``suppressed``).
    """

    model_config = ConfigDict(extra="forbid")

    channels: Annotated[list[str], Field(min_length=1, max_length=_MAX_TEST_CHANNELS)] = Field(
        default_factory=lambda: ["browser", "email", "discord"]
    )
    severity: Literal["info", "low", "medium", "high", "critical"] = "info"

    @field_validator("channels")
    @classmethod
    def _known_channels(cls, value: list[str]) -> list[str]:
        unknown = sorted({c for c in value if c not in TESTABLE_CHANNELS})
        if unknown:
            allowed = ", ".join(sorted(TESTABLE_CHANNELS))
            raise ValueError(f"unknown channel(s) {unknown}; allowed: {allowed}")
        # De-dupe while preserving order, so a repeated channel is settled once.
        return list(dict.fromkeys(value))


def _synthetic_alert(req: NotificationTestRequest, *, now: datetime) -> AlertRecord:
    """A throwaway ``open`` alert that never enters live state — every requested
    channel starts ``pending`` so :meth:`resolve_channels` settles each one."""
    return AlertRecord(
        id=f"test-{uuid.uuid4().hex[:12]}",
        source="notification-test",
        observed_at=now,
        received_at=now,
        published_at=now,
        rule_id="notification-test",
        subject_id=None,
        state="open",
        severity=req.severity,
        title="aether test notification",
        summary="Test notification requested via POST /api/v2/notifications/test.",
        triggered_at=now,
        delivery_status={channel: "pending" for channel in req.channels},
    )


def build_notifications_router(
    dispatcher: NotificationDispatcher,
    *,
    clock: Callable[[], datetime],
) -> APIRouter:
    """Build the test-notification router bound to this app's dispatcher.

    Shares the dispatcher so the test path uses the exact drivers + thresholds the
    live path does — there is one notification configuration, not a test copy.
    """
    router = APIRouter(prefix="/api/v2/notifications", tags=["notifications"])

    @router.post("/test")
    async def test_notification(body: NotificationTestRequest) -> dict[str, Any]:
        alert = _synthetic_alert(body, now=clock())
        channels = await dispatcher.resolve_channels(alert)  # no publish — isolated
        return {
            "alert_id": alert.id,
            "severity": alert.severity,
            "channels": channels,
            "targets": describe_targets(dispatcher, channels),  # credential-free
        }

    return router

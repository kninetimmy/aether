"""Alert notification dispatcher (M4.7a, PRD §20.4, §20.5).

Drives :class:`aether.alerts.notify.NotificationDispatcher` with a fake publish sink
(collects re-published alerts) and an injected clock, so channel resolution,
per-channel severity thresholds, the drop-oldest queue, and the no-dispatch-loop
guarantee are all exercised without a running backend or any external I/O.

Follows the repo's async-test convention (``asyncio.run`` in a sync test, no
``pytest-asyncio``); the dispatcher is built *inside* the scenario coroutine so its
``asyncio.Queue`` binds to the same loop that drains it.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from datetime import UTC, datetime

import pytest

from aether.alerts.notify import (
    ChannelThresholds,
    NotificationDispatcher,
    NotificationDriver,
    meets_threshold,
)
from aether.schema.records import AlertRecord
from aether.state.live import StateChange

T0 = datetime(2026, 6, 19, 12, 0, 0, tzinfo=UTC)
T1 = datetime(2026, 6, 19, 12, 0, 5, tzinfo=UTC)  # the dispatcher's write-back clock


def _alert(
    *,
    delivery_status: dict[str, str],
    severity: str = "high",
    state: str = "open",
    alert_id: str = "alert-1",
) -> AlertRecord:
    return AlertRecord(
        id=alert_id,
        source="alert-engine",
        observed_at=T0,
        received_at=T0,
        published_at=T0,
        rule_id="rule-1",
        subject_id="aircraft:icao:abc",
        state=state,  # type: ignore[arg-type]
        severity=severity,  # type: ignore[arg-type]
        title="Emergency squawk",
        summary="Emergency squawk — abc",
        triggered_at=T0,
        delivery_status=delivery_status,
    )


def _change(alert: AlertRecord, *, op: str = "upsert") -> StateChange:
    return StateChange(seq=1, op=op, kind="alert", id=alert.id, record=alert)  # type: ignore[arg-type]


class _Sink:
    """Collects alerts the dispatcher re-publishes (the ``hub.publish`` stand-in)."""

    def __init__(self) -> None:
        self.published: list[AlertRecord] = []

    def __call__(self, alert: AlertRecord) -> None:
        self.published.append(alert)


class _RecordingDriver(NotificationDriver):
    """A server-side driver double: records calls, returns a fixed result."""

    def __init__(self, *, ok: bool) -> None:
        self.ok = ok
        self.calls: list[AlertRecord] = []

    async def deliver(self, alert: AlertRecord) -> bool:
        self.calls.append(alert)
        return self.ok


def _settle(
    changes: Iterable[StateChange],
    *,
    thresholds: ChannelThresholds | None = None,
    drivers: dict[str, NotificationDriver] | None = None,
    queue_maxsize: int = 1000,
) -> tuple[list[AlertRecord], _Sink]:
    """Build a dispatcher, observe ``changes``, drain once; return (updated, sink)."""

    async def scenario() -> tuple[list[AlertRecord], _Sink]:
        sink = _Sink()
        disp = NotificationDispatcher(
            publish=sink,
            clock=lambda: T1,
            thresholds=thresholds,
            drivers=drivers,
            queue_maxsize=queue_maxsize,
        )
        for change in changes:
            disp.observe(change)
        return await disp.drain(), sink

    return asyncio.run(scenario())


# -- threshold helper --------------------------------------------------------


@pytest.mark.parametrize(
    ("severity", "threshold", "expected"),
    [
        ("info", "info", True),
        ("critical", "info", True),
        ("low", "high", False),
        ("high", "high", True),
        ("critical", "high", True),
        ("medium", "critical", False),
        ("anything", "info", True),  # unknown severity ranks as info, fails open
        ("high", "bogus", True),  # unknown threshold ranks as info, fails open
    ],
)
def test_meets_threshold(severity: str, threshold: str, expected: bool) -> None:
    assert meets_threshold(severity, threshold) is expected


# -- observe() gating (the synchronous hot-path touchpoint) ------------------


def test_observe_ignores_non_alert_resolved_and_settled() -> None:
    updated, sink = _settle(
        [
            StateChange(seq=1, op="upsert", kind="track", id="t", record=None),  # wrong kind
            _change(_alert(delivery_status={"browser": "pending"}), op="remove"),  # wrong op
            _change(_alert(delivery_status={"browser": "pending"}, state="resolved")),  # not open
            _change(_alert(delivery_status={"dashboard": "delivered"})),  # nothing pending
        ]
    )
    assert updated == []
    assert sink.published == []


def test_dashboard_only_alert_needs_no_dispatch() -> None:
    # The engine pre-delivers dashboard, so a dashboard-only alert has no pending
    # channel and the dispatcher never touches it (no redundant re-publish).
    updated, sink = _settle([_change(_alert(delivery_status={"dashboard": "delivered"}))])
    assert updated == []
    assert sink.published == []


# -- channel resolution ------------------------------------------------------


def test_browser_delivered_when_meeting_threshold() -> None:
    updated, sink = _settle(
        [_change(_alert(delivery_status={"dashboard": "delivered", "browser": "pending"}))]
    )
    assert len(updated) == 1
    assert updated[0].delivery_status == {"dashboard": "delivered", "browser": "delivered"}
    assert updated[0].published_at == T1  # write-back bumps published_at
    assert sink.published == updated  # re-published through the hub


def test_browser_suppressed_below_threshold() -> None:
    updated, _ = _settle(
        [
            _change(
                _alert(
                    delivery_status={"dashboard": "delivered", "browser": "pending"},
                    severity="low",
                )
            )
        ],
        thresholds=ChannelThresholds(browser="critical"),
    )
    assert updated[0].delivery_status == {"dashboard": "delivered", "browser": "suppressed"}


def test_server_channel_unconfigured_without_driver() -> None:
    # email/discord selected but no driver wired (pre-M4.7b) → honest "unconfigured".
    updated, _ = _settle(
        [_change(_alert(delivery_status={"email": "pending", "discord": "pending"}))]
    )
    assert updated[0].delivery_status == {"email": "unconfigured", "discord": "unconfigured"}


def test_server_channel_suppressed_below_threshold_before_driver() -> None:
    # Below the channel threshold the driver is never consulted.
    driver = _RecordingDriver(ok=True)
    updated, _ = _settle(
        [_change(_alert(delivery_status={"email": "pending"}, severity="medium"))],
        thresholds=ChannelThresholds(email="critical"),
        drivers={"email": driver},
    )
    assert updated[0].delivery_status == {"email": "suppressed"}
    assert driver.calls == []  # threshold short-circuits the driver


def test_server_driver_delivered_and_failed() -> None:
    for ok, expected in ((True, "delivered"), (False, "failed")):
        driver = _RecordingDriver(ok=ok)
        updated, _ = _settle(
            [_change(_alert(delivery_status={"discord": "pending"}))],
            drivers={"discord": driver},
        )
        assert updated[0].delivery_status == {"discord": expected}
        assert len(driver.calls) == 1


# -- no dispatch loop --------------------------------------------------------


def test_writeback_does_not_redispatch() -> None:
    # Feeding the re-published (settled) alert back through observe enqueues nothing.
    async def scenario() -> tuple[list[AlertRecord], list[AlertRecord]]:
        sink = _Sink()
        disp = NotificationDispatcher(publish=sink, clock=lambda: T1)
        disp.observe(
            _change(_alert(delivery_status={"dashboard": "delivered", "browser": "pending"}))
        )
        first = await disp.drain()
        disp.observe(_change(first[0]))  # the write-back, observed again
        second = await disp.drain()
        return first, second

    first, second = asyncio.run(scenario())
    assert len(first) == 1
    assert second == []  # settled → not re-enqueued


# -- bounded queue, drop-oldest ----------------------------------------------


def test_queue_drops_oldest_when_full() -> None:
    updated, _ = _settle(
        [
            _change(_alert(delivery_status={"browser": "pending"}, alert_id=f"alert-{n}"))
            for n in range(4)  # 0,1 dropped as 2,3 arrive → only the newest two survive
        ],
        queue_maxsize=2,
    )
    assert {a.id for a in updated} == {"alert-2", "alert-3"}

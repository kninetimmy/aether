"""Unit tests for the local APRS adapter runtime (PRD §17.1, §17.4, §18.1, §37).

No broker and no socket: the records generator is driven by a fake source whose
``frames()`` yields canned TNC2 lines (or raises mid-stream). Covers the throttle
gate, the jittered backoff, the starting/connected/degraded status flow, the
records_received/records_rejected counting, socket-drop reconnect, and the
fresh-generator-per-broker-reconnect guard. Mirrors the ADS-B twin.
"""

import asyncio
import contextlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import aiomqtt
import pytest

from aether.adapters import local_aprs
from aether.adapters.local_aprs import (
    MAX_BACKOFF_S,
    ThrottleGate,
    _backoff,
    local_aprs_records,
    run_local_aprs,
)
from aether.config import Settings
from aether.schema.records import Record

T0 = datetime(2026, 6, 17, 12, 0, 0, tzinfo=UTC)

_POSITION = "N0CALL>APRS,WIDE1-1:!4903.50N/07201.75W>Test position"
_OBJECT = "W1OBJ>APRS,WIDE2-1:;LEADER   *092345z4903.50N/07201.75W>088/036Edge"
_DEFERRED = "N0TLM>APRS:T#005,199,000,255,073,123,01101001"  # telemetry: skipped


class _FakeAprsSource:
    """Yields queued TNC2 lines, optionally raising a queued error mid-stream.

    Re-iterable: each ``frames()`` call replays the lines, so a reconnect (a fresh
    ``frames()``) re-opens with the same canned data, modeling a live channel.
    """

    def __init__(self, lines: list[Any], *, host: str = "127.0.0.1", port: int = 8001) -> None:
        self._lines = lines
        self.host = host
        self.port = port
        self.closes = 0

    async def frames(self) -> AsyncIterator[str]:
        for item in self._lines:
            if isinstance(item, Exception):
                raise item
            yield item

    async def close(self) -> None:
        self.closes += 1


async def _take(agen: AsyncIterator[Record], n: int) -> list[Record]:
    out: list[Record] = []
    async for item in agen:
        out.append(item)
        if len(out) >= n:
            break
    return out


def take(agen: AsyncIterator[Record], n: int) -> list[Record]:
    return asyncio.run(_take(agen, n))


# --- ThrottleGate (PRD §18.1) -------------------------------------------------


def test_throttle_admits_first_then_suppresses_until_interval() -> None:
    gate = ThrottleGate(1.0)
    assert gate.admit("a", T0, emergency=False) is True
    assert gate.admit("a", T0 + timedelta(seconds=0.5), emergency=False) is False
    assert gate.admit("a", T0 + timedelta(seconds=1.0), emergency=False) is True


def test_throttle_emergency_transition_bypasses_interval() -> None:
    gate = ThrottleGate(100.0)
    assert gate.admit("b", T0, emergency=False) is True
    assert gate.admit("b", T0 + timedelta(seconds=0.1), emergency=True) is True
    assert gate.admit("b", T0 + timedelta(seconds=0.2), emergency=True) is False


def test_throttle_evicts_stations_idle_past_ttl() -> None:
    # A station untouched longer than the TTL is forgotten, so its NEXT admit is
    # treated as first-seen (returns True) rather than throttled — and its state
    # no longer occupies the gate's tables.
    gate = ThrottleGate(1.0, ttl_s=10.0)
    assert gate.admit("a", T0, emergency=False) is True
    # A different station keeps the gate active well past "a"'s TTL window.
    assert gate.admit("b", T0 + timedelta(seconds=11.0), emergency=False) is True
    assert "a" not in gate._last_published  # type: ignore[attr-defined]
    assert "a" not in gate._emergency  # type: ignore[attr-defined]
    # "a" returns as first-seen, not suppressed.
    assert gate.admit("a", T0 + timedelta(seconds=11.0), emergency=False) is True


def test_throttle_table_stays_bounded_under_distinct_callsign_flood() -> None:
    # Feed many MORE distinct callsigns than the cap, all within one TTL window so
    # time eviction alone can't help, and assert the size backstop keeps the tables
    # bounded (PRD §17.3, §37 — a multi-day soak / junk flood must not grow forever).
    from aether.adapters.local_aprs import _GATE_MAX_ENTRIES

    gate = ThrottleGate(1.0, ttl_s=1_000_000.0)
    now = T0
    for i in range(_GATE_MAX_ENTRIES * 3):
        now = T0 + timedelta(milliseconds=i)  # monotonic, all inside the TTL window
        gate.admit(f"call-{i}", now, emergency=False)
    assert len(gate._last_published) <= _GATE_MAX_ENTRIES  # type: ignore[attr-defined]
    assert len(gate._emergency) <= _GATE_MAX_ENTRIES  # type: ignore[attr-defined]


# --- backoff (PRD §17.1) ------------------------------------------------------


def test_backoff_jitters_within_bounds_and_grows() -> None:
    sleep_for, next_delay = _backoff(1.0)
    assert 0.0 <= sleep_for <= 1.0
    assert next_delay == 2.0


def test_backoff_caps_at_max() -> None:
    sleep_for, next_delay = _backoff(1000.0)
    assert sleep_for <= MAX_BACKOFF_S
    assert next_delay == MAX_BACKOFF_S


# --- records() generator (PRD §17.1, §17.4, §18.1) ----------------------------


def test_generator_emits_starting_then_tracks_and_connected_status() -> None:
    src = _FakeAprsSource([_POSITION, _DEFERRED, _OBJECT])
    items = take(local_aprs_records(src, throttle_s=0.0), 6)

    assert items[0].kind == "source_status"
    assert items[0].status == "starting"  # type: ignore[union-attr]

    tracks = [i for i in items if i.kind == "track"]
    assert tracks
    assert all(t.source == "local_aprs" for t in tracks)  # type: ignore[union-attr]
    assert all(t.locally_received is True for t in tracks)  # type: ignore[union-attr]
    assert any(t.id == "aprs:station:N0CALL" for t in tracks)  # type: ignore[union-attr]
    assert any(t.id == "aprs:object:LEADER" for t in tracks)  # type: ignore[union-attr]

    connected = [
        i
        for i in items
        if i.kind == "source_status" and i.status == "connected"  # type: ignore[union-attr]
    ]
    assert connected
    last = connected[-1]
    assert last.records_received == len(tracks)  # type: ignore[union-attr]
    assert last.records_rejected == 1  # the telemetry line was deferred  # type: ignore[union-attr]
    assert last.attributes["connection"] == "kiss"


def test_generator_reopens_after_socket_drop() -> None:
    # frames() raises mid-stream; the generator must emit 'degraded' and resume
    # from a fresh connection (starting/connected again) rather than ending.
    src = _FakeAprsSource([_POSITION, ConnectionError("socket closed"), _POSITION])
    items = take(local_aprs_records(src, throttle_s=0.0), 8)

    statuses = [i.status for i in items if i.kind == "source_status"]  # type: ignore[union-attr]
    assert statuses[0] == "starting"
    assert "degraded" in statuses
    # After the drop the SAME source object is re-iterated (a fresh socket), so a
    # later 'connected' status proves the stream resumed, not ended.
    degraded_idx = statuses.index("degraded")
    assert "connected" in statuses[degraded_idx + 1 :]
    assert src.closes >= 1  # the source was closed on the drop


# --- run_local_aprs reconnect (PRD §17.1, §37) --------------------------------


class _FakeBus:
    """Publishes into a sink, optionally raising ``MqttError`` after N publishes."""

    def __init__(self, fail_after: int | None, sink: list[Any]) -> None:
        self._fail_after = fail_after
        self._sink = sink
        self.count = 0

    async def publish_record(self, record: Any) -> None:
        self.count += 1
        if self._fail_after is not None and self.count > self._fail_after:
            raise aiomqtt.MqttError("broker dropped")
        self._sink.append(record)


def test_run_local_aprs_keeps_publishing_after_broker_drop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A mid-publish broker drop must not silently end the adapter.

    The first connection fails after one publish; the runner must reconnect and
    keep publishing on a *fresh* generator (and a fresh source). The buggy version
    that reused the closed generator would never publish on the second connection
    and this test would time out.
    """
    monkeypatch.setattr(local_aprs, "_backoff", lambda _delay: (0.0, 0.0))

    # A source that streams forever so each connection has data to publish.
    def fake_source(_host: str, _port: int, *, timeout_s: float = 5.0) -> _FakeAprsSource:
        return _FakeAprsSource([_POSITION] * 1000)

    monkeypatch.setattr(local_aprs, "AprsSource", fake_source)

    async def scenario() -> dict[str, int]:
        state = {"connects": 0, "second_conn_publishes": 0}
        second_published = asyncio.Event()

        @contextlib.asynccontextmanager
        async def fake_connect(_cfg: Settings, *, identifier: str | None = None) -> Any:
            state["connects"] += 1
            if state["connects"] == 1:
                yield _FakeBus(fail_after=1, sink=[])  # drops after the first publish
            else:

                class _CountingBus(_FakeBus):
                    async def publish_record(self, record: Any) -> None:
                        await super().publish_record(record)
                        state["second_conn_publishes"] += 1
                        second_published.set()

                yield _CountingBus(fail_after=None, sink=[])

        monkeypatch.setattr(local_aprs, "connect", fake_connect)
        cfg = Settings(local_aprs=True, local_aprs_throttle_s=0.0)
        ready = asyncio.Event()
        ready.set()
        task = asyncio.create_task(run_local_aprs(cfg, ready))
        try:
            await asyncio.wait_for(second_published.wait(), timeout=5.0)
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        return state

    state = asyncio.run(scenario())
    assert state["connects"] >= 2, "runner must reconnect after a broker drop"
    assert state["second_conn_publishes"] >= 1, "runner must resume publishing after reconnect"

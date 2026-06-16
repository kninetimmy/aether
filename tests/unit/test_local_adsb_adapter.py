"""Unit tests for the local ADS-B adapter runtime (PRD §17.1, §17.4, §18.1).

Covers the throttle gate (interval + emergency-immediate), the file/URL source
(read, size cap, 304 -> unchanged), the jittered backoff, and the records()
generator (starting -> tracks + connected health, emergency bypass, degraded on a
failed poll). No broker and no SDR: the generator is driven by a fake source and
a real (fast) event loop via ``asyncio.run``.
"""

import asyncio
import contextlib
import json
import urllib.error
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import aiomqtt
import pytest

from aether.adapters import local_adsb
from aether.adapters.local_adsb import (
    MAX_BACKOFF_S,
    ReadsbSource,
    SnapshotUnchanged,
    ThrottleGate,
    _backoff,
    _loads_snapshot,
    local_adsb_records,
    run_local_adsb,
)
from aether.adapters.readsb_fake_feeder import fake_snapshot
from aether.config import Settings
from aether.schema.records import Record

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "readsb" / "aircraft.json"
T0 = datetime(2026, 6, 16, 12, 0, 0, tzinfo=UTC)


class _FakeSource:
    """Returns queued snapshots (or raises queued errors) on each ``fetch``."""

    def __init__(self, items: list[Any]) -> None:
        self._items = items
        self.calls = 0

    async def fetch(self) -> dict[str, Any]:
        item = self._items[min(self.calls, len(self._items) - 1)]
        self.calls += 1
        if isinstance(item, Exception):
            raise item
        return item


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
    assert gate.admit("a", T0, emergency=False) is True  # first sighting always
    assert gate.admit("a", T0 + timedelta(seconds=0.5), emergency=False) is False
    assert gate.admit("a", T0 + timedelta(seconds=1.0), emergency=False) is True  # due


def test_throttle_emergency_transition_bypasses_interval() -> None:
    gate = ThrottleGate(100.0)
    assert gate.admit("b", T0, emergency=False) is True
    # Flip to emergency well within the throttle window: published immediately.
    assert gate.admit("b", T0 + timedelta(seconds=0.1), emergency=True) is True
    # Still emergency but no new transition -> back under the throttle.
    assert gate.admit("b", T0 + timedelta(seconds=0.2), emergency=True) is False


def test_throttle_prune_forgets_absent_aircraft() -> None:
    gate = ThrottleGate(100.0)
    gate.admit("a", T0, emergency=False)
    gate.admit("b", T0, emergency=False)
    gate.prune({"a"})
    # 'a' still throttled; 'b' was pruned so it's a fresh sighting again.
    assert gate.admit("a", T0 + timedelta(seconds=0.1), emergency=False) is False
    assert gate.admit("b", T0 + timedelta(seconds=0.1), emergency=False) is True


# --- ReadsbSource + snapshot loading (PRD §17.2, §17.4) -----------------------


def test_source_reads_file(tmp_path: Path) -> None:
    path = tmp_path / "aircraft.json"
    path.write_text(json.dumps({"now": 1.0, "aircraft": []}))
    data = asyncio.run(ReadsbSource(str(path)).fetch())
    assert data["aircraft"] == []


def test_loads_rejects_non_object() -> None:
    with pytest.raises(ValueError):
        _loads_snapshot(b"[1, 2, 3]")


def test_loads_rejects_oversize(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(local_adsb, "MAX_SNAPSHOT_BYTES", 4)
    with pytest.raises(ValueError):
        _loads_snapshot(b'{"now": 1.0}')


def test_url_304_maps_to_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    source = ReadsbSource("http://127.0.0.1:8080/data/aircraft.json")

    def fake_urlopen(req: Any, timeout: float) -> Any:
        raise urllib.error.HTTPError(req.full_url, 304, "Not Modified", {}, None)  # type: ignore[arg-type]

    monkeypatch.setattr(local_adsb.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(SnapshotUnchanged):
        asyncio.run(source.fetch())


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


def test_generator_emits_starting_then_tracks_and_connected_health() -> None:
    snapshot = json.loads(FIXTURE.read_text())
    items = take(local_adsb_records(_FakeSource([snapshot]), poll_s=0.0, throttle_s=1.0), 8)

    assert items[0].kind == "source_status"
    assert items[0].status == "starting"  # type: ignore[union-attr]

    tracks = [i for i in items if i.kind == "track"]
    assert tracks  # fixture yields identified aircraft
    assert all(t.source == "local_adsb" for t in tracks)  # type: ignore[union-attr]

    connected = next(i for i in items if i.kind == "source_status" and i.status == "connected")  # type: ignore[union-attr]
    assert connected.records_received == len(tracks)  # type: ignore[union-attr]
    assert connected.attributes["aircraft_visible"] >= 1
    assert "messages_total" in connected.attributes


def test_generator_publishes_emergency_transition_immediately() -> None:
    # tick 0 has the would-be emergency aircraft squawking normally; tick 3 flips
    # it to 7500. A large throttle window proves the publish is the transition,
    # not the interval.
    snaps = [fake_snapshot(0, now_epoch=1.0), fake_snapshot(3, now_epoch=2.0)]
    items = take(local_adsb_records(_FakeSource(snaps), poll_s=0.0, throttle_s=100.0), 7)

    emergency_tracks = [
        i
        for i in items
        if i.kind == "track" and i.id == "aircraft:icao:e00001" and "emergency" in i.tags
    ]
    assert emergency_tracks, "emergency-squawk transition must publish despite the throttle"


def test_generator_marks_degraded_on_failed_poll() -> None:
    items = take(local_adsb_records(_FakeSource([OSError("source down")]), poll_s=0.0), 2)
    assert items[0].status == "starting"  # type: ignore[union-attr]
    assert items[1].status == "degraded"  # type: ignore[union-attr]
    assert items[1].error_code == "OSError"  # type: ignore[union-attr]


# --- run_local_adsb reconnect (PRD §17.1, §37) --------------------------------


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


def test_run_local_adsb_keeps_publishing_after_broker_drop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A mid-publish broker drop must not silently end the adapter.

    The first connection fails after one publish; the runner must reconnect and
    keep publishing on a *fresh* stream. The buggy version reused the closed
    generator and stopped for good, so the second connection would never publish
    and this test would time out.
    """
    monkeypatch.setattr(local_adsb, "_backoff", lambda _delay: (0.0, 0.0))

    async def scenario() -> dict[str, int]:
        state = {"connects": 0, "second_conn_publishes": 0}
        second_published = asyncio.Event()
        second_sink: list[Any] = []

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

                yield _CountingBus(fail_after=None, sink=second_sink)

        monkeypatch.setattr(local_adsb, "connect", fake_connect)
        cfg = Settings(
            local_adsb_source=str(FIXTURE), local_adsb_poll_s=0.0, local_adsb_throttle_s=0.0
        )
        ready = asyncio.Event()
        ready.set()
        task = asyncio.create_task(run_local_adsb(cfg, ready))
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

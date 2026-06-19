"""Unit tests for the AIS (AISStream.io) vessel adapter (PRD §18.5, §17.3, §37).

Covers the AIS-specific runtime with no live feed and no API key: the bounding-box
and subscription builders, the dynamic/static merge by MMSI, duplicate-relay
collapsing, the record stream's parse/merge/throttle/reject plumbing with network-only
provenance, reconnect recovery, and the fail-visible runner. A fake in-memory source
(and one real local WebSocket server) stand in for AISStream.
"""

import asyncio
import contextlib
import json
import socket
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import aiomqtt
import pytest
from tests.fixtures.ais.messages import TIME_UTC, position_report, ship_static

from aether.adapters import ais as ais_mod
from aether.adapters.ais import (
    SOURCE,
    AisStreamSource,
    VesselMerger,
    ais_bbox,
    ais_records,
    build_subscription,
    dup_signature,
    run_ais,
)
from aether.adapters.ais_fake_feeder import serve_ais
from aether.config import Settings
from aether.schema.records import SourceStatusRecord, TrackRecord

NOW = datetime(2026, 6, 18, 12, 0, 0, tzinfo=UTC)
_KT = 1852.0 / 3600.0


# --- pure builders ------------------------------------------------------------


def test_ais_bbox_covers_aoi_around_station() -> None:
    # 500 NM is 8.333° of latitude; longitude is wider, scaled by 1/cos(lat).
    box = ais_bbox(39.0, -77.0, 500.0)
    assert len(box) == 1
    (min_lat, min_lon), (max_lat, max_lon) = box[0]
    assert min_lat == pytest.approx(39.0 - 500.0 / 60.0, abs=1e-6)
    assert max_lat == pytest.approx(39.0 + 500.0 / 60.0, abs=1e-6)
    assert min_lon < -77.0 < max_lon
    assert (max_lon - min_lon) > (max_lat - min_lat)  # lon span wider at 39°N


def test_ais_bbox_clamps_to_wgs84_bounds() -> None:
    # A huge radius near a pole/antimeridian must clamp, never overflow WGS 84.
    (min_lat, min_lon), (max_lat, max_lon) = ais_bbox(89.0, 179.0, 1000.0)[0]
    assert -90.0 <= min_lat <= max_lat <= 90.0
    assert -180.0 <= min_lon <= max_lon <= 180.0


def test_ais_bbox_rejects_non_finite_and_out_of_bounds() -> None:
    # A nan/inf or out-of-bounds AOI must fail VISIBLY (raised -> offline ConfigError
    # in run_ais), not format a meaningless box the server silently ignores.
    with pytest.raises(ValueError, match="latitude"):
        ais_bbox(float("nan"), 0.0, 500.0)
    with pytest.raises(ValueError, match="latitude"):
        ais_bbox(91.0, 0.0, 500.0)
    with pytest.raises(ValueError, match="longitude"):
        ais_bbox(0.0, float("inf"), 500.0)
    with pytest.raises(ValueError, match="longitude"):
        ais_bbox(0.0, -181.0, 500.0)
    with pytest.raises(ValueError, match="radius"):
        ais_bbox(0.0, 0.0, 0.0)
    with pytest.raises(ValueError, match="radius"):
        ais_bbox(0.0, 0.0, float("inf"))


def test_build_subscription_format_and_key_in_body() -> None:
    bbox = ais_bbox(0.0, 0.0, 100.0)
    payload = json.loads(build_subscription("my-key", bbox))
    assert payload == {"APIKey": "my-key", "BoundingBoxes": bbox}


def test_build_subscription_requires_api_key() -> None:
    # The adapter is opt-in; an enabled-but-unconfigured key must fail visibly, never
    # connect anonymously or bake in a default (PRD §2/§37).
    with pytest.raises(ValueError, match="API key is required"):
        build_subscription("", [[[0.0, 0.0], [1.0, 1.0]]])
    with pytest.raises(ValueError, match="API key is required"):
        build_subscription("   ", [[[0.0, 0.0], [1.0, 1.0]]])


# --- message decoding + dynamic/static merge ----------------------------------


def test_position_report_becomes_a_vessel_track() -> None:
    merger = VesselMerger()
    env = position_report(123456789, 38.5, -74.5, sog=12.5, cog=270.0, heading=271, nav=0)
    track = merger.update(env, received_at=NOW)
    assert track is not None
    assert track.id == "ais:vessel:123456789"
    assert track.correlation_key == track.id
    assert track.source == SOURCE
    assert track.track_type == "vessel"
    assert track.geometry is not None and track.geometry.coordinates == [-74.5, 38.5]  # [lon, lat]
    assert track.speed_mps == pytest.approx(12.5 * _KT)
    assert track.heading_deg == 271.0  # TrueHeading preferred over Cog
    assert track.attributes["nav_status_text"] == "under_way_using_engine"
    assert track.attributes["mmsi"] == "123456789"
    # Network-only: no local RF leg in scope.
    assert track.locally_received is False
    assert [p.local_rf for p in track.provenance] == [False]
    assert track.provenance[0].source == SOURCE
    assert track.provenance[0].provider == "aisstream"
    # observed_at comes off the broadcast time, not receipt.
    assert track.observed_at == datetime(2026, 6, 18, 12, 0, 0, tzinfo=UTC)


def test_static_then_position_merges_voyage_into_track() -> None:
    # AIS sends position and static (name/type/voyage) as separate messages; the
    # merger folds the latest static into the position so the vessel is one track
    # (PRD §18.5 / AIS-FR-003).
    merger = VesselMerger()
    assert merger.update(ship_static(111, name="EVER GIVEN", ship_type=70), received_at=NOW) is None
    track = merger.update(position_report(111, 1.0, 2.0), received_at=NOW)
    assert track is not None
    assert track.label == "EVER GIVEN"
    assert track.attributes["vessel_name"] == "EVER GIVEN"
    assert track.attributes["ship_type"] == 70
    assert track.attributes["ship_type_text"] == "cargo"
    assert track.attributes["destination"] == "PORT DEMO"
    assert track.attributes["imo"] == "1000001"
    assert track.attributes["length_m"] == pytest.approx(120.0)  # A(100)+B(20)
    assert track.attributes["beam_m"] == pytest.approx(20.0)  # C(10)+D(10)


def test_position_only_vessel_is_labeled_by_mmsi() -> None:
    merger = VesselMerger()
    track = merger.update(position_report(222, 1.0, 2.0, ship_name=""), received_at=NOW)
    assert track is not None
    assert track.label == "222"
    assert "vessel_name" not in track.attributes


def test_static_arriving_after_a_position_updates_the_next_position() -> None:
    # position(no name) -> static -> position(name now merged): proves the merge is
    # order-independent and that a static-only message plots nothing on its own.
    merger = VesselMerger()
    first = merger.update(position_report(333, 1.0, 2.0), received_at=NOW)
    assert first is not None and first.label == "333"
    assert merger.update(ship_static(333, name="SECOND WIND"), received_at=NOW) is None
    second = merger.update(position_report(333, 1.1, 2.1), received_at=NOW)
    assert second is not None and second.label == "SECOND WIND"


def test_extended_classb_carries_name_and_position() -> None:
    merger = VesselMerger()
    env = position_report(
        444, 3.0, 4.0, message_type="ExtendedClassBPositionReport", ship_name="CLASSB"
    )
    env["Message"]["ExtendedClassBPositionReport"]["Name"] = "CLASSB"
    env["Message"]["ExtendedClassBPositionReport"]["Type"] = 37  # pleasure craft
    track = merger.update(env, received_at=NOW)
    assert track is not None
    assert track.geometry is not None
    assert track.label == "CLASSB"
    assert track.attributes["ship_type_text"] == "pleasure_craft"


def test_heading_falls_back_to_cog_and_sentinels_drop_to_none() -> None:
    merger = VesselMerger()
    # TrueHeading 511 (n/a) -> fall back to Cog 120.
    t1 = merger.update(position_report(1, 0.0, 0.0, heading=511, cog=120.0), received_at=NOW)
    assert t1 is not None and t1.heading_deg == 120.0
    # All sentinels: Sog 102.3, Cog 360, TrueHeading 511 -> all None.
    env = position_report(2, 0.0, 0.0, sog=102.3, cog=360.0, heading=511)
    t2 = merger.update(env, received_at=NOW)
    assert t2 is not None and t2.speed_mps is None and t2.heading_deg is None


def test_message_without_mmsi_is_dropped() -> None:
    merger = VesselMerger()
    env = position_report(1, 0.0, 0.0)
    del env["MetaData"]["MMSI"]
    del env["Message"]["PositionReport"]["UserID"]
    assert merger.update(env, received_at=NOW) is None


def test_static_only_and_unsupported_types_plot_nothing() -> None:
    merger = VesselMerger()
    assert merger.update(ship_static(7, name="X"), received_at=NOW) is None  # no position yet
    base_station = {
        "MessageType": "BaseStationReport",
        "MetaData": {"MMSI": 8, "latitude": 1.0, "longitude": 2.0, "time_utc": TIME_UTC},
        "Message": {"BaseStationReport": {"UserID": 8, "Latitude": 1.0, "Longitude": 2.0}},
    }
    assert merger.update(base_station, received_at=NOW) is None  # unsupported type


# --- duplicate signature ------------------------------------------------------


def test_dup_signature_collapses_same_broadcast_distinguishes_others() -> None:
    a = position_report(123, 1.0, 2.0, time_utc=TIME_UTC)
    b = position_report(123, 9.9, 9.9, time_utc=TIME_UTC)  # same mmsi+type+time -> same relay
    c = position_report(123, 1.0, 2.0, time_utc="2026-06-18 12:00:05.0 +0000 UTC")  # later
    assert dup_signature(a) == dup_signature(b)
    assert dup_signature(a) != dup_signature(c)


def test_dup_signature_is_none_without_a_broadcast_time() -> None:
    # No timestamp -> skip dedup, so genuinely distinct messages are never collapsed.
    assert dup_signature(position_report(123, 1.0, 2.0, time_utc=None)) is None


# --- record stream ------------------------------------------------------------


class _FakeSource:
    """A stand-in :class:`AisStreamSource`: yields canned frames then blocks forever."""

    url = "ws://fake/v0/stream"

    def __init__(self, frames: list[str]) -> None:
        self._frames = frames
        self.closed = 0

    async def messages(self) -> AsyncIterator[str]:
        for frame in self._frames:
            yield frame
        await asyncio.Event().wait()  # never a clean return in the test

    async def close(self) -> None:
        self.closed += 1


class _RaisingSource:
    """A source whose connection fails immediately (no frames)."""

    url = "ws://fake/v0/stream"

    def __init__(self) -> None:
        self.closed = 0

    async def messages(self) -> AsyncIterator[str]:
        raise ConnectionError("connect boom")
        yield ""  # unreachable; makes this an async generator

    async def close(self) -> None:
        self.closed += 1


class _ReplaySource:
    """Re-iterable source: replays queued frames, raising a queued exception in place."""

    url = "ws://fake/v0/stream"

    def __init__(self, items: list[object]) -> None:
        self._items = items
        self.closes = 0

    async def messages(self) -> AsyncIterator[str]:
        for item in self._items:
            if isinstance(item, Exception):
                raise item
            assert isinstance(item, str)
            yield item
        await asyncio.Event().wait()

    async def close(self) -> None:
        self.closes += 1


async def _drive(agen: AsyncIterator, stop: Callable[[object], bool]) -> list:
    """Collect records until ``stop`` is true for one, then close the generator."""
    out: list = []
    async for record in agen:
        out.append(record)
        if stop(record):
            break
    await agen.aclose()
    return out


def _frames(*envelopes: dict[str, Any]) -> list[str]:
    return [json.dumps(e) for e in envelopes]


def test_records_first_is_starting() -> None:
    records = asyncio.run(_drive(ais_records(_FakeSource([]), throttle_s=0.0), lambda _: True))
    assert isinstance(records[0], SourceStatusRecord)
    assert records[0].status == "starting"
    assert records[0].source == SOURCE


def test_records_parse_merge_dedup_and_reject() -> None:
    pos = position_report(111, 38.5, -74.5, time_utc=TIME_UTC)
    frames = _frames(
        ship_static(111, name="MERGED", ship_type=80),  # static-only -> rejected (nothing to plot)
        pos,  # first position -> track (carries the merged name/type)
        pos,  # exact relay (same mmsi/type/time) -> dropped by dedup
        position_report(222, 39.0, -74.0, ship_name="", time_utc=TIME_UTC),  # second vessel
    )

    # Stop on the status emitted AFTER both distinct vessels: the dropped duplicate
    # is only reflected in a later status (the dup path increments and `continue`s
    # without yielding), so stopping on the second track would miss it.
    def _both_received(r: object) -> bool:
        return isinstance(r, SourceStatusRecord) and r.records_received >= 2

    records = asyncio.run(_drive(ais_records(_FakeSource(frames), throttle_s=0.0), _both_received))

    tracks = [r for r in records if isinstance(r, TrackRecord)]
    assert [t.id for t in tracks] == ["ais:vessel:111", "ais:vessel:222"]
    assert tracks[0].label == "MERGED"  # static folded into the position
    assert tracks[0].attributes["ship_type_text"] == "tanker"

    statuses = [r for r in records if isinstance(r, SourceStatusRecord)]
    last = statuses[-1]
    assert last.attributes["duplicates"] == 1  # the exact relay was dropped
    assert last.records_rejected == 1  # the static-only frame had nothing to plot
    assert last.records_received == 2  # the two distinct vessel positions


def test_source_error_yields_degraded() -> None:
    def _is_degraded(r: object) -> bool:
        return isinstance(r, SourceStatusRecord) and r.status == "degraded"

    records = asyncio.run(_drive(ais_records(_RaisingSource(), throttle_s=0.0), _is_degraded))
    assert records[0].status == "starting"  # type: ignore[union-attr]
    degraded = next(r for r in records if _is_degraded(r))
    assert degraded.error_code == "ConnectionError"
    assert "boom" in (degraded.error_summary or "")


def test_records_reopens_after_drop(monkeypatch: pytest.MonkeyPatch) -> None:
    # A mid-stream socket error must yield 'degraded' then RESUME on a fresh
    # connection (a later 'connected'), not end the stream (PRD §17.4/§37).
    monkeypatch.setattr(ais_mod, "_backoff", lambda _delay: (0.0, 0.0))
    frame = json.dumps(position_report(111, 1.0, 2.0))
    src = _ReplaySource([frame, ConnectionError("socket closed"), frame])

    seen_degraded = {"v": False}

    def stop(r: object) -> bool:
        if isinstance(r, SourceStatusRecord) and r.status == "degraded":
            seen_degraded["v"] = True
        return seen_degraded["v"] and isinstance(r, SourceStatusRecord) and r.status == "connected"

    records = asyncio.run(_drive(ais_records(src, throttle_s=0.0), stop))
    statuses = [r.status for r in records if isinstance(r, SourceStatusRecord)]
    assert statuses[0] == "starting"
    assert "degraded" in statuses
    degraded_idx = statuses.index("degraded")
    assert "connected" in statuses[degraded_idx + 1 :]  # resumed on a fresh connection
    assert src.closes >= 1


def test_records_isolates_a_malformed_frame() -> None:
    # One unparseable frame is counted as a rejection and never drops the rest of the
    # stream or crashes the adapter (PRD §37 failure isolation).
    frames = ["this is not json", json.dumps(position_report(111, 1.0, 2.0))]
    records = asyncio.run(
        _drive(
            ais_records(_FakeSource(frames), throttle_s=0.0),
            lambda r: isinstance(r, TrackRecord),
        )
    )
    tracks = [r for r in records if isinstance(r, TrackRecord)]
    assert [t.id for t in tracks] == ["ais:vessel:111"]  # good frame survived the bad one
    statuses = [r for r in records if isinstance(r, SourceStatusRecord)]
    assert any(s.records_rejected >= 1 for s in statuses)


# --- real WebSocket source ----------------------------------------------------


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def test_source_subscribes_and_receives_frames_over_websocket() -> None:
    # The real AisStreamSource against the real (local) fake WS server: connect,
    # send the subscription, decode JSON frames.
    async def run() -> None:
        port = _free_port()
        server = await serve_ais("127.0.0.1", port, interval_s=0.01)
        try:
            source = AisStreamSource(
                f"ws://127.0.0.1:{port}/v0/stream",
                build_subscription("demo", ais_bbox(38.5, -74.5, 100.0)),
                timeout_s=2.0,
            )
            agen = source.messages()
            try:
                first = json.loads(await agen.__anext__())
                assert first["MessageType"] == "ShipStaticData"
                assert first["MetaData"]["MMSI"] == 111111111
            finally:
                await agen.aclose()
                await source.close()
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(run())


def test_source_raises_connection_error_when_server_closes() -> None:
    # A server that completes the handshake then closes (no frames) must surface as a
    # ConnectionError so the runner reconnects (PRD §17.3).
    async def run() -> None:
        port = _free_port()
        server = await serve_ais("127.0.0.1", port, frames=[], loop_forever=False)
        try:
            source = AisStreamSource(
                f"ws://127.0.0.1:{port}/v0/stream",
                build_subscription("demo", ais_bbox(0.0, 0.0, 100.0)),
                timeout_s=2.0,
            )
            agen = source.messages()
            with pytest.raises(ConnectionError):
                await agen.__anext__()
            await agen.aclose()
            await source.close()
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(run())


def test_source_connect_failure_is_a_connection_error() -> None:
    # Connecting to a refused port must raise ConnectionError, not leak a raw OSError.
    async def run() -> None:
        port = _free_port()  # nothing listening here
        source = AisStreamSource(
            f"ws://127.0.0.1:{port}/v0/stream",
            build_subscription("k", ais_bbox(0.0, 0.0, 100.0)),
            timeout_s=1.0,
        )
        agen = source.messages()
        with pytest.raises(ConnectionError):
            await agen.__anext__()
        await agen.aclose()

    asyncio.run(run())


# --- runner: fail-visibly on a bad config (PRD §2/§37) ------------------------


def _run_offline_scenario(cfg: Settings, monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    monkeypatch.setattr(ais_mod, "_backoff", lambda _delay: (0.0, 0.0))

    async def scenario() -> list[Any]:
        published: list[Any] = []

        @contextlib.asynccontextmanager
        async def fake_connect(_cfg: Settings, *, identifier: str | None = None) -> Any:
            class _Bus:
                async def publish_record(self, record: Any) -> None:
                    published.append(record)

            yield _Bus()

        monkeypatch.setattr(ais_mod, "connect", fake_connect)
        ready = asyncio.Event()
        ready.set()
        await asyncio.wait_for(run_ais(cfg, ready), timeout=5.0)  # must return, not spin
        return published

    return asyncio.run(scenario())


def test_run_ais_reports_offline_on_missing_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    published = _run_offline_scenario(Settings(ais=True, ais_api_key=""), monkeypatch)
    offline = [r for r in published if isinstance(r, SourceStatusRecord) and r.status == "offline"]
    assert offline, "missing API key must publish a visible offline status"
    assert offline[0].error_code == "ConfigError"
    assert offline[0].source == SOURCE


def test_run_ais_reports_offline_on_invalid_aoi(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = Settings(ais=True, ais_api_key="key", ais_center_lat=float("nan"))
    published = _run_offline_scenario(cfg, monkeypatch)
    offline = [r for r in published if isinstance(r, SourceStatusRecord) and r.status == "offline"]
    assert offline and offline[0].error_code == "ConfigError"


# --- merger accumulator is bounded (PRD §27.2, §37) ---------------------------


def test_vessel_merger_evicts_stale_entries() -> None:
    # A vessel unheard past ttl_s is dropped from the static accumulator, so it does
    # not grow for the life of the (reconnect-spanning) session.
    merger = VesselMerger(ttl_s=30.0, max_entries=64)
    merger.update(position_report(1, 0.0, 0.0), received_at=NOW)
    assert "1" in merger._static
    merger.update(position_report(2, 0.0, 0.0), received_at=NOW + timedelta(seconds=31))
    assert "1" not in merger._static  # past TTL -> evicted
    assert "2" in merger._static


def test_vessel_merger_is_size_bounded() -> None:
    # Even with a huge TTL (so only size evicts), an unbounded flood of unique MMSIs
    # stays capped (PRD §37 — the failure-isolation memory bound).
    merger = VesselMerger(ttl_s=1e9, max_entries=4)
    for i in range(50):
        merger.update(position_report(i, 0.0, 0.0), received_at=NOW + timedelta(seconds=i))
    assert len(merger._static) <= 4
    assert len(merger._last_seen) <= 4


def test_observed_at_falls_back_to_received_at_without_broadcast_time() -> None:
    # A position with no time_utc is stamped at receipt rather than crashing or
    # looking infinitely old (PRD §17.2 graceful missing-field handling).
    merger = VesselMerger()
    track = merger.update(position_report(1, 0.0, 0.0, time_utc=None), received_at=NOW)
    assert track is not None and track.observed_at == NOW


def test_throttle_paces_repeated_positions_per_vessel() -> None:
    # Three positions for one vessel inside the throttle window collapse to ONE track
    # (PRD §18.1); a distinct vessel still gets through. Distinct broadcast times so
    # the dedup filter admits all three (isolating the throttle from the dedup).
    frames = _frames(
        position_report(1, 0.0, 0.0, time_utc="2026-06-18 12:00:00.0 +0000 UTC"),
        position_report(1, 0.1, 0.1, time_utc="2026-06-18 12:00:01.0 +0000 UTC"),
        position_report(1, 0.2, 0.2, time_utc="2026-06-18 12:00:02.0 +0000 UTC"),
        position_report(2, 1.0, 1.0, time_utc="2026-06-18 12:00:00.0 +0000 UTC"),
    )

    def _vessel_2(r: object) -> bool:
        return isinstance(r, TrackRecord) and r.id == "ais:vessel:2"

    records = asyncio.run(_drive(ais_records(_FakeSource(frames), throttle_s=1000.0), _vessel_2))
    tracks = [r for r in records if isinstance(r, TrackRecord)]
    assert [t.id for t in tracks] == ["ais:vessel:1", "ais:vessel:2"]  # vessel 1 paced to one


# --- runner: reconnect on broker loss (PRD §17.1, §37) ------------------------


class _FakeBus:
    """Publishes into a sink, optionally raising ``MqttError`` after N publishes."""

    def __init__(
        self, fail_after: int | None, on_publish: Callable[[], None] | None = None
    ) -> None:
        self._fail_after = fail_after
        self._on_publish = on_publish
        self.count = 0

    async def publish_record(self, record: Any) -> None:
        self.count += 1
        if self._fail_after is not None and self.count > self._fail_after:
            raise aiomqtt.MqttError("broker dropped")
        if self._on_publish is not None:
            self._on_publish()


def test_run_ais_keeps_publishing_after_broker_drop(monkeypatch: pytest.MonkeyPatch) -> None:
    # A mid-publish broker drop must reconnect and keep publishing on a FRESH records
    # generator + source (the PEP 525 / M2.1b lesson). The buggy reuse-the-closed-
    # generator version would never publish on the second connection and time out.
    monkeypatch.setattr(ais_mod, "_backoff", lambda _delay: (0.0, 0.0))
    frame = json.dumps(position_report(1, 0.0, 0.0))

    def fake_source(_url: str, _subscription: str, *, timeout_s: float = 10.0) -> _FakeSource:
        return _FakeSource([frame] * 1000)

    monkeypatch.setattr(ais_mod, "AisStreamSource", fake_source)

    async def scenario() -> int:
        state = {"connects": 0}
        second_published = asyncio.Event()

        @contextlib.asynccontextmanager
        async def fake_connect(_cfg: Settings, *, identifier: str | None = None) -> Any:
            state["connects"] += 1
            if state["connects"] == 1:
                yield _FakeBus(fail_after=1)  # drops right after the first publish
            else:
                yield _FakeBus(fail_after=None, on_publish=second_published.set)

        monkeypatch.setattr(ais_mod, "connect", fake_connect)
        cfg = Settings(ais=True, ais_api_key="k", ais_throttle_s=0.0)
        ready = asyncio.Event()
        ready.set()
        task = asyncio.create_task(run_ais(cfg, ready))
        try:
            await asyncio.wait_for(second_published.wait(), timeout=5.0)
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        return state["connects"]

    connects = asyncio.run(scenario())
    assert connects >= 2  # reconnected after the broker drop

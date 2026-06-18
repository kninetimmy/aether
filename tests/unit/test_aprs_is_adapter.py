"""Unit tests for the APRS-IS display adapter (PRD §18.4, §17.3, §37).

The shared TNC2 parser is covered in ``test_aprs_adapter.py``; this covers the
APRS-IS-specific runtime around it — the login/filter builders, multi-igate
duplicate detection, stall detection, and the record stream's dedup/parse/throttle
plumbing with ``local_rf=False`` provenance. No broker, no live APRS-IS: a fake
in-memory source (and one tiny silent TCP server) stand in.
"""

import asyncio
import contextlib
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from aether.adapters import aprs_is as aprs_is_mod
from aether.adapters.aprs_is import (
    SOURCE,
    AprsIsSource,
    DuplicateFilter,
    aprs_is_filter,
    aprs_is_records,
    build_login,
    dup_signature,
    run_aprs_is,
)
from aether.config import Settings
from aether.schema.records import SourceStatusRecord, TrackRecord

NOW = datetime(2026, 6, 18, 12, 0, 0, tzinfo=UTC)


# --- pure builders ------------------------------------------------------------


def test_aprs_is_filter_converts_nm_to_km() -> None:
    # The APRS-IS range filter distance is in km; the AOI radius is in NM.
    assert aprs_is_filter(39.0, -77.0, 500.0) == "r/39.00000/-77.00000/926"  # 500 * 1.852
    assert aprs_is_filter(0.0, 0.0, 250.0) == "r/0.00000/0.00000/463"


def test_aprs_is_filter_rejects_non_finite_and_out_of_bounds() -> None:
    # A nan/inf or out-of-bounds AOI must fail VISIBLY (raised -> offline ConfigError
    # in run_aprs_is), not format a silently-broken filter the server rejects.
    with pytest.raises(ValueError, match="latitude"):
        aprs_is_filter(float("nan"), 0.0, 500.0)
    with pytest.raises(ValueError, match="latitude"):
        aprs_is_filter(91.0, 0.0, 500.0)
    with pytest.raises(ValueError, match="longitude"):
        aprs_is_filter(0.0, float("inf"), 500.0)
    with pytest.raises(ValueError, match="longitude"):
        aprs_is_filter(0.0, -181.0, 500.0)
    with pytest.raises(ValueError, match="radius"):
        aprs_is_filter(0.0, 0.0, 0.0)
    with pytest.raises(ValueError, match="radius"):
        aprs_is_filter(0.0, 0.0, float("inf"))


def test_build_login_format_and_receive_only_default() -> None:
    line = build_login("N0CALL", "-1", "r/39.00000/-77.00000/926")
    assert line == "user N0CALL pass -1 vers aether 0.1 filter r/39.00000/-77.00000/926"


def test_build_login_requires_callsign() -> None:
    # The adapter is opt-in; an enabled-but-unconfigured callsign must fail visibly,
    # never default to a baked-in identity (PRD §2/§37).
    with pytest.raises(ValueError, match="callsign is required"):
        build_login("", "-1", "r/0/0/0")
    with pytest.raises(ValueError, match="callsign is required"):
        build_login("   ", "-1", "r/0/0/0")


def test_build_login_rejects_injection() -> None:
    # Callsign and passcode are single tokens: any whitespace (CR/LF would break out
    # to a second line; a space/tab would rewrite the token structure, e.g. smuggle
    # extra vers/filter terms) is rejected. The filter may have internal spaces
    # (multi-term) but never CR/LF.
    with pytest.raises(ValueError, match="whitespace"):
        build_login("N0CALL\r\nXYZ", "-1", "r/0/0/0")  # CR/LF in callsign
    with pytest.raises(ValueError, match="whitespace"):
        build_login("N0 CALL", "-1", "r/0/0/0")  # space in callsign
    with pytest.raises(ValueError, match="whitespace"):
        build_login("N0CALL", "-1\n", "r/0/0/0")  # CR/LF in passcode
    with pytest.raises(ValueError, match="whitespace"):
        build_login("N0CALL", "-1 vers EVIL 9", "r/0/0/0")  # token-rewriting passcode
    with pytest.raises(ValueError, match="CR/LF"):
        build_login("N0CALL", "-1", "r/0/0/0\nfilter x")  # CR/LF in filter


# --- duplicate detection ------------------------------------------------------


def test_dup_signature_ignores_relay_path() -> None:
    # The same packet relayed by two igates differs only in the q-construct/path.
    a = "N0CALL>APRS,WIDE1-1,qAR,IGATE1:!4903.50N/07201.75W>x"
    b = "N0CALL>APRS,TCPIP*,qAO,IGATE2:!4903.50N/07201.75W>x"
    assert dup_signature(a) == dup_signature(b) == "N0CALL>APRS:!4903.50N/07201.75W>x"


def test_duplicate_filter_window_and_reentry() -> None:
    dedup = DuplicateFilter(ttl_s=30.0)
    assert dedup.admit("sig", NOW) is True  # first sighting admits
    assert dedup.admit("sig", NOW + timedelta(seconds=5)) is False  # relay within window
    assert dedup.admit("other", NOW) is True  # a distinct packet is unaffected
    # After the window elapses, a re-beacon of the same content is a fresh obs.
    assert dedup.admit("sig", NOW + timedelta(seconds=31)) is True


def test_duplicate_filter_is_bounded() -> None:
    dedup = DuplicateFilter(ttl_s=1e9, max_entries=4)  # ttl huge so only size evicts
    for i in range(50):
        assert dedup.admit(f"sig-{i}", NOW + timedelta(seconds=i)) is True
    # The table never exceeds the cap despite 50 distinct signatures (PRD §37).
    assert len(dedup._seen) <= 4


def test_duplicate_filter_at_capacity_does_not_overevict_on_duplicate() -> None:
    # A repeat of an EXISTING signature while the table is full must not evict an
    # unrelated still-valid signature: the insert reserve is conditional on the
    # incoming key being new (mirrors ThrottleGate), so a no-insert duplicate leaves
    # the table untouched.
    dedup = DuplicateFilter(ttl_s=1e9, max_entries=4)
    for i in range(4):
        dedup.admit(f"sig-{i}", NOW + timedelta(seconds=i))
    assert set(dedup._seen) == {"sig-0", "sig-1", "sig-2", "sig-3"}
    assert dedup.admit("sig-3", NOW + timedelta(seconds=5)) is False  # in-window dup
    assert set(dedup._seen) == {"sig-0", "sig-1", "sig-2", "sig-3"}  # nothing evicted


# --- record stream ------------------------------------------------------------


class _FakeSource:
    """A stand-in :class:`AprsIsSource`: yields canned lines then blocks forever.

    Blocking after the last line (instead of returning) keeps the stream in a
    single deterministic pass — the runner only reconnects on a *clean* return or
    an error, so a test can collect exactly what the canned lines produce.
    """

    host = "fake"
    port = 0

    def __init__(self, lines: list[str]) -> None:
        self._lines = lines
        self.closed = 0

    async def lines(self) -> AsyncIterator[str]:
        for line in self._lines:
            yield line
        await asyncio.Event().wait()  # never a clean return in the test

    async def close(self) -> None:
        self.closed += 1


class _RaisingSource:
    """A source whose connection fails immediately (no lines)."""

    host = "fake"
    port = 0

    def __init__(self) -> None:
        self.closed = 0

    async def lines(self) -> AsyncIterator[str]:
        raise ConnectionError("connect boom")
        yield ""  # unreachable; makes this an async generator

    async def close(self) -> None:
        self.closed += 1


async def _drive(agen: AsyncIterator, stop: Callable[[object], bool]) -> list:
    """Collect records until ``stop`` is true for one, then close the generator.

    Content-driven (like the network adapter's test driver), never timeout-driven:
    breaking out of an ``async for`` and ``aclose()``-ing leaves the generator
    suspended at a ``yield`` so GeneratorExit unwinds it cleanly — whereas
    cancelling ``__anext__`` mid-await wedges an async generator.
    """
    out: list = []
    async for record in agen:
        out.append(record)
        if stop(record):
            break
    await agen.aclose()
    return out


def test_records_first_is_starting() -> None:
    records = asyncio.run(_drive(aprs_is_records(_FakeSource([]), throttle_s=0.0), lambda _: True))
    assert isinstance(records[0], SourceStatusRecord)
    assert records[0].status == "starting"
    assert records[0].source == SOURCE


def test_records_parse_network_only_dedup_and_reject() -> None:
    lines = [
        "# logresp N0CALL verified, server T2TEST",  # comment → connected liveness
        "N0CALL>APU25N,TCPIP*,qAC,T2TEST:!4903.50N/07201.75W>copy",  # a track
        "N0CALL>APU25N,TCPIP*,qAR,IG2:!4903.50N/07201.75W>copy",  # exact relay → dropped
        "N0CALL>APU25N::WU2Z     :a message",  # message type → parser None → rejected
    ]

    # Stop once the message line has registered a rejection (the last thing the
    # canned lines produce), by which point the track and the dropped dup are in.
    def _seen_rejection(r: object) -> bool:
        return isinstance(r, SourceStatusRecord) and r.records_rejected >= 1

    agen = aprs_is_records(_FakeSource(lines), throttle_s=0.0)
    records = asyncio.run(_drive(agen, _seen_rejection))

    tracks = [r for r in records if isinstance(r, TrackRecord)]
    assert len(tracks) == 1, "the multi-igate relay must not produce a second track"
    track = tracks[0]
    assert track.id == "aprs:station:N0CALL"
    assert track.source == SOURCE
    assert track.locally_received is False  # network-only until fused with local RF
    assert track.provenance[0].local_rf is False

    statuses = [r for r in records if isinstance(r, SourceStatusRecord)]
    # The `# logresp` comment surfaces as a connected liveness status.
    assert any(
        s.status == "connected" and "logresp" in (s.attributes.get("server_message") or "")
        for s in statuses
    )
    # The dropped duplicate is counted, and the message line is a rejection.
    last = statuses[-1]
    assert last.attributes["duplicates"] == 1
    assert last.records_rejected == 1
    assert last.records_received == 1


def test_source_error_yields_degraded() -> None:
    def _is_degraded(r: object) -> bool:
        return isinstance(r, SourceStatusRecord) and r.status == "degraded"

    records = asyncio.run(_drive(aprs_is_records(_RaisingSource(), throttle_s=0.0), _is_degraded))
    assert records[0].status == "starting"  # type: ignore[union-attr]
    degraded = next(r for r in records if _is_degraded(r))
    assert degraded.error_code == "ConnectionError"
    assert "boom" in (degraded.error_summary or "")


# --- stall detection (real socket) -------------------------------------------


def test_stall_detection_raises_connection_error() -> None:
    # A server that accepts, reads the login, then goes silent must trip the
    # last-line stall timeout and raise ConnectionError so the runner reconnects
    # (PRD §17.3 "detect silent/stalled connections").
    async def run() -> None:
        async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            await reader.readline()  # consume the login, then send nothing back
            await reader.read()  # block (silent) until the client disconnects, then return
            writer.close()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        async with server:
            source = AprsIsSource(
                "127.0.0.1",
                port,
                build_login("N0CALL", "-1", "r/0/0/0"),
                timeout_s=2.0,
                stall_s=0.2,
            )
            agen = source.lines()
            with pytest.raises(ConnectionError, match="stalled"):
                await agen.__anext__()  # connect + login, then no data within stall_s
            await agen.aclose()
            await source.close()

    asyncio.run(run())


def test_overlength_line_is_skipped() -> None:
    # An over-spec line (> _MAX_LINE_BYTES, but under the reader buffer limit) is
    # skipped, not yielded; the next good line still comes through (PRD §17.2).
    async def run() -> None:
        good = b"N0CALL>APRS:!4903.50N/07201.75W>ok\r\n"
        toolong = b"X" * 700 + b"\r\n"  # 702 B > 600 cap, < 1200 reader limit

        async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            await reader.readline()  # login
            writer.write(toolong)
            writer.write(good)
            await writer.drain()
            await reader.read()  # wait for the client to disconnect
            writer.close()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        async with server:
            source = AprsIsSource(
                "127.0.0.1",
                port,
                build_login("N0CALL", "-1", "r/0/0/0"),
                timeout_s=2.0,
                stall_s=2.0,
            )
            agen = source.lines()
            first = await agen.__anext__()  # the over-long line is skipped
            assert first == "N0CALL>APRS:!4903.50N/07201.75W>ok"
            await agen.aclose()
            await source.close()

    asyncio.run(run())


# --- reconnect recovery + failure isolation -----------------------------------


class _ReplaySource:
    """Re-iterable source: replays queued lines, raising a queued exception in place.

    Mirrors ``_FakeAprsSource`` in the local APRS tests — each ``lines()`` call
    re-opens with the same canned data, modeling a live feed across a reconnect.
    A trailing block (after a clean pass) keeps a no-error run from looping.
    """

    host = "fake"
    port = 0

    def __init__(self, items: list[object]) -> None:
        self._items = items
        self.closes = 0

    async def lines(self) -> AsyncIterator[str]:
        for item in self._items:
            if isinstance(item, Exception):
                raise item
            assert isinstance(item, str)
            yield item
        await asyncio.Event().wait()

    async def close(self) -> None:
        self.closes += 1


def test_records_reopens_after_drop(monkeypatch: pytest.MonkeyPatch) -> None:
    # A mid-stream socket error must yield 'degraded' and then RESUME from a fresh
    # connection (a later 'connected') rather than ending the stream — the §17.4/§37
    # reconnect guarantee, otherwise untested for this adapter.
    monkeypatch.setattr(aprs_is_mod, "_backoff", lambda _delay: (0.0, 0.0))
    track = "N0CALL>APU25N,TCPIP*,qAC,T2X:!4903.50N/07201.75W>copy"
    src = _ReplaySource([track, ConnectionError("socket closed"), track])

    seen_degraded = {"v": False}

    def stop(r: object) -> bool:
        if isinstance(r, SourceStatusRecord) and r.status == "degraded":
            seen_degraded["v"] = True
        return seen_degraded["v"] and isinstance(r, SourceStatusRecord) and r.status == "connected"

    records = asyncio.run(_drive(aprs_is_records(src, throttle_s=0.0), stop))
    statuses = [r.status for r in records if isinstance(r, SourceStatusRecord)]
    assert statuses[0] == "starting"
    assert "degraded" in statuses
    degraded_idx = statuses.index("degraded")
    assert "connected" in statuses[degraded_idx + 1 :]  # resumed on a fresh connection
    assert src.closes >= 1  # the source was closed on the drop


def test_records_isolates_a_malformed_line(monkeypatch: pytest.MonkeyPatch) -> None:
    # One unparseable line is counted as a rejection and never drops the rest of the
    # stream or crashes the adapter (PRD §37 failure isolation).
    lines = [
        "garbage with no delimiters",  # unparseable -> rejected
        "N0CALL>APU25N,TCPIP*,qAC,T2X:!4903.50N/07201.75W>good",  # still flows after
    ]
    records = asyncio.run(
        _drive(
            aprs_is_records(_FakeSource(lines), throttle_s=0.0),
            lambda r: isinstance(r, TrackRecord),
        )
    )
    tracks = [r for r in records if isinstance(r, TrackRecord)]
    assert [t.id for t in tracks] == ["aprs:station:N0CALL"]  # good line survived the bad one
    statuses = [r for r in records if isinstance(r, SourceStatusRecord)]
    assert any(s.records_rejected >= 1 for s in statuses)  # the bad line was counted, not fatal


# --- runner: fail-visibly on a missing callsign (PRD §2/§37) ------------------


def test_run_aprs_is_reports_offline_on_missing_callsign(monkeypatch: pytest.MonkeyPatch) -> None:
    # An enabled-but-unconfigured callsign must publish a VISIBLE offline status and
    # then return (not spin) — never silently and never as a baked-in identity.
    monkeypatch.setattr(aprs_is_mod, "_backoff", lambda _delay: (0.0, 0.0))

    async def scenario() -> list[Any]:
        published: list[Any] = []

        @contextlib.asynccontextmanager
        async def fake_connect(_cfg: Settings, *, identifier: str | None = None) -> Any:
            class _Bus:
                async def publish_record(self, record: Any) -> None:
                    published.append(record)

            yield _Bus()

        monkeypatch.setattr(aprs_is_mod, "connect", fake_connect)
        cfg = Settings(aprs_is=True, aprs_is_callsign="")  # enabled, no callsign
        ready = asyncio.Event()
        ready.set()
        # Must complete (return), not spin, after publishing the offline status.
        await asyncio.wait_for(run_aprs_is(cfg, ready), timeout=5.0)
        return published

    published = asyncio.run(scenario())
    offline = [r for r in published if isinstance(r, SourceStatusRecord) and r.status == "offline"]
    assert offline, "missing callsign must publish a visible offline status"
    assert offline[0].error_code == "ConfigError"
    assert offline[0].source == SOURCE

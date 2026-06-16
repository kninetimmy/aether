"""Local ADS-B (`readsb`) adapter runner (PRD §17.1, §18.1).

The runtime around the pure :mod:`aether.adapters.readsb` parser: a poller that
reads ``aircraft.json`` from a file or http(s) URL, a generator that turns each
snapshot into throttled track records plus periodic source-status/receiver
health, and a runner that pumps the stream onto the bus with reconnect.

Responsibility split (PRD §17.1):

- :class:`ReadsbSource` — source connection: read a snapshot atomically (whole
  file / single HTTP GET), with a size cap, a timeout, and conditional requests
  (ETag / Last-Modified) so an unchanged URL snapshot costs a 304, not a reparse.
- :func:`local_adsb_records` — the ``records()`` contract: normalize via the
  parser, apply the §18.1 throttle (≤1 ordinary update per aircraft per interval,
  emergency-squawk transitions published immediately), surface receiver health,
  and keep last good state while marking the source degraded on a failed poll
  (PRD §17.4). Blocking I/O is its own concern; this stays pure-async.
- :func:`run_local_adsb` — bus connection + serialization + jittered exponential
  backoff on broker loss, mirroring the demo/subscriber lifespan tasks.

Receive-only: this reads the operator's own antenna feed and never transmits.
"""

import asyncio
import json
import logging
import random
import urllib.error
import urllib.request
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any, Literal

import aiomqtt

from aether.adapters.readsb import SOURCE, parse_aircraft_snapshot
from aether.bus.client import Bus, connect
from aether.config import Settings
from aether.schema.records import Record, SourceStatusRecord

log = logging.getLogger(__name__)

#: Reject a snapshot larger than this before parsing (PRD §17.2 size limits).
#: A busy receiver's aircraft.json is well under a megabyte; this is generous.
MAX_SNAPSHOT_BYTES = 16 * 1024 * 1024

#: Jittered exponential backoff bounds for source/bus retries (PRD §17.1).
INITIAL_BACKOFF_S = 1.0
MAX_BACKOFF_S = 30.0

#: Stable id for this source's retained health record (PRD §23 status stream).
STATUS_ID = f"source_status:{SOURCE}"


class SnapshotUnchanged(Exception):
    """A conditional URL poll returned 304 — keep the last snapshot."""


def _now() -> datetime:
    return datetime.now(UTC)


def _backoff(delay: float) -> tuple[float, float]:
    """Full-jitter backoff: sleep a random slice of ``delay``, then double it.

    Returns ``(sleep_for, next_delay)``. Jitter avoids a thundering-herd retry
    when a source or the broker comes back (PRD §17.1, §17.4 "add request jitter").
    """
    capped = min(delay, MAX_BACKOFF_S)
    sleep_for = random.uniform(0.0, capped)
    return sleep_for, min(capped * 2.0, MAX_BACKOFF_S)


def _loads_snapshot(raw: bytes) -> dict[str, Any]:
    """Decode snapshot bytes to a JSON object, enforcing the size cap."""
    if len(raw) > MAX_SNAPSHOT_BYTES:
        raise ValueError(f"aircraft.json {len(raw)} bytes exceeds limit {MAX_SNAPSHOT_BYTES}")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("aircraft.json top level is not an object")
    return data


class ReadsbSource:
    """Reads one ``aircraft.json`` snapshot per call from a file or http(s) URL.

    File reads and the (blocking) urllib request both run in a worker thread so
    the async loop is never stalled. URL reads send conditional headers and raise
    :class:`SnapshotUnchanged` on a 304 so an idle receiver isn't re-parsed.
    """

    def __init__(self, location: str, *, timeout_s: float = 5.0) -> None:
        self._location = location
        self._timeout_s = timeout_s
        self._is_url = location.startswith(("http://", "https://"))
        self._etag: str | None = None
        self._last_modified: str | None = None

    @property
    def location(self) -> str:
        return self._location

    async def fetch(self) -> dict[str, Any]:
        if self._is_url:
            return await asyncio.to_thread(self._fetch_url)
        return await asyncio.to_thread(self._fetch_file)

    def _fetch_file(self) -> dict[str, Any]:
        # A single read() sees a consistent file: readsb writes atomically via
        # rename, so we never observe a half-written snapshot (PRD §18.1).
        with open(self._location, "rb") as fh:
            raw = fh.read(MAX_SNAPSHOT_BYTES + 1)
        return _loads_snapshot(raw)

    def _fetch_url(self) -> dict[str, Any]:
        # Scheme is constrained to http(s) in __init__; conditional headers make
        # an idle receiver's repoll a cheap 304 (PRD §17.4).
        req = urllib.request.Request(self._location)
        if self._etag:
            req.add_header("If-None-Match", self._etag)
        if self._last_modified:
            req.add_header("If-Modified-Since", self._last_modified)
        try:
            with urllib.request.urlopen(req, timeout=self._timeout_s) as resp:
                self._etag = resp.headers.get("ETag")
                self._last_modified = resp.headers.get("Last-Modified")
                raw = resp.read(MAX_SNAPSHOT_BYTES + 1)
        except urllib.error.HTTPError as exc:
            if exc.code == 304:
                raise SnapshotUnchanged from exc
            raise
        return _loads_snapshot(raw)


class ThrottleGate:
    """Per-aircraft publish gate enforcing the §18.1 update policy.

    Admits a track when at least ``interval_s`` has elapsed since its last
    publish, or immediately on an emergency-squawk transition (not-emergency ->
    emergency) regardless of the interval. State is pruned to the live aircraft
    each cycle so memory stays bounded (PRD §17.3).
    """

    def __init__(self, interval_s: float) -> None:
        self._interval_s = interval_s
        self._last_published: dict[str, datetime] = {}
        self._emergency: dict[str, bool] = {}

    def admit(self, track_id: str, now: datetime, *, emergency: bool) -> bool:
        was_emergency = self._emergency.get(track_id, False)
        self._emergency[track_id] = emergency
        last = self._last_published.get(track_id)
        due = last is None or (now - last).total_seconds() >= self._interval_s
        if (emergency and not was_emergency) or due:
            self._last_published[track_id] = now
            return True
        return False

    def prune(self, live_ids: set[str]) -> None:
        self._last_published = {k: v for k, v in self._last_published.items() if k in live_ids}
        self._emergency = {k: v for k, v in self._emergency.items() if k in live_ids}


def _epoch(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _receiver_health(snapshot: dict[str, Any], aircraft_visible: int) -> dict[str, Any]:
    """Receiver-health attributes from the snapshot itself (PRD §18.1).

    aircraft.json carries enough to report health without a separate
    ``receiver.json`` fetch: how many aircraft are currently tracked and the
    cumulative message count. Absent fields are simply omitted.
    """
    health: dict[str, Any] = {"aircraft_visible": aircraft_visible}
    messages = _epoch(snapshot.get("messages"))
    if messages is not None:
        health["messages_total"] = messages
    return health


def _status(
    status: Literal["starting", "connected", "degraded", "stale", "offline", "disabled"],
    now: datetime,
    *,
    records_received: int = 0,
    records_rejected: int = 0,
    last_record_at: datetime | None = None,
    lag_s: float | None = None,
    error_code: str | None = None,
    error_summary: str | None = None,
    attributes: dict[str, Any] | None = None,
) -> SourceStatusRecord:
    return SourceStatusRecord(
        id=STATUS_ID,
        source=SOURCE,
        observed_at=now,
        received_at=now,
        published_at=now,
        status=status,
        last_record_at=last_record_at,
        lag_s=lag_s,
        records_received=records_received,
        records_rejected=records_rejected,
        error_code=error_code,
        error_summary=error_summary,
        attributes=attributes or {},
    )


async def local_adsb_records(
    source: ReadsbSource,
    *,
    poll_s: float = 1.0,
    throttle_s: float = 1.0,
) -> AsyncIterator[Record]:
    """Yield the local ADS-B record stream: status, then throttled tracks + health.

    Emits ``starting`` immediately, then polls forever. Each good poll yields the
    admitted tracks (see :class:`ThrottleGate`) followed by a ``connected`` status
    carrying receiver health and lag. A failed poll yields a ``degraded`` status
    (keeping the last good tracks on the map) and backs off with jitter before
    retrying — one bad poll never tears down the stream (PRD §17.4, §37).
    """
    yield _status("starting", _now())
    gate = ThrottleGate(throttle_s)
    received = 0
    backoff = INITIAL_BACKOFF_S
    while True:
        now = _now()
        try:
            snapshot = await source.fetch()
        except SnapshotUnchanged:
            yield _status("connected", now, records_received=received)
            await asyncio.sleep(poll_s)
            continue
        except Exception as exc:  # source down / unreadable / malformed JSON
            log.warning("local ADS-B poll failed (%s); backing off", exc)
            yield _status(
                "degraded",
                now,
                records_received=received,
                error_code=type(exc).__name__,
                error_summary=str(exc)[:200],
            )
            sleep_for, backoff = _backoff(backoff)
            await asyncio.sleep(sleep_for)
            continue
        backoff = INITIAL_BACKOFF_S  # reset on a successful read

        tracks = parse_aircraft_snapshot(snapshot, received_at=now, source=SOURCE)
        live_ids = {t.id for t in tracks}
        last_record_at: datetime | None = None
        for track in tracks:
            if gate.admit(track.id, now, emergency="emergency" in track.tags):
                received += 1
                last_record_at = track.observed_at
                yield track
        gate.prune(live_ids)

        snapshot_now = _epoch(snapshot.get("now"))
        lag_s = now.timestamp() - snapshot_now if snapshot_now is not None else None
        yield _status(
            "connected",
            now,
            records_received=received,
            last_record_at=last_record_at,
            lag_s=lag_s,
            attributes=_receiver_health(snapshot, len(live_ids)),
        )
        await asyncio.sleep(poll_s)


async def run_local_adsb(
    cfg: Settings,
    ready: asyncio.Event,
    *,
    poll_s: float | None = None,
    throttle_s: float | None = None,
) -> None:
    """Pump the local ADS-B stream onto the bus until cancelled (PRD §17.1).

    Waits for the subscriber to be live (avoids a startup race), then publishes
    the :func:`local_adsb_records` stream. A broker drop triggers a jittered
    exponential reconnect rather than crashing the lifespan; the generator is
    resumed across reconnects so source state (throttle, counters) is preserved.
    """
    await ready.wait()
    source = ReadsbSource(cfg.local_adsb_source, timeout_s=cfg.local_adsb_timeout_s)
    stream = local_adsb_records(
        source,
        poll_s=poll_s if poll_s is not None else cfg.local_adsb_poll_s,
        throttle_s=throttle_s if throttle_s is not None else cfg.local_adsb_throttle_s,
    )
    log.info("local ADS-B adapter -> %s", source.location)
    backoff = INITIAL_BACKOFF_S
    while True:
        try:
            async with connect(cfg, identifier="aether-local-adsb") as bus:
                backoff = INITIAL_BACKOFF_S  # reset once connected
                await _pump(bus, stream)
                return  # generator exhausted (only on cancellation in practice)
        except aiomqtt.MqttError as exc:
            sleep_for, backoff = _backoff(backoff)
            log.warning("local ADS-B lost broker (%s); reconnecting in %.1fs", exc, sleep_for)
            await asyncio.sleep(sleep_for)


async def _pump(bus: Bus, stream: AsyncIterator[Record]) -> None:
    async for record in stream:
        await bus.publish_record(record)

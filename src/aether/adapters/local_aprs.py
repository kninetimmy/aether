"""Local APRS adapter runner (PRD §18.3, §17.1).

The runtime around the pure :mod:`aether.adapters.aprs` parser and the
:mod:`aether.adapters.aprs_kiss` decoder: a TCP reader of Dire Wolf's KISS port,
a generator that decodes KISS/AX.25 -> TNC2 -> throttled tracks plus periodic
source-status, and a runner that pumps the stream onto the bus with reconnect.

Responsibility split mirrors :mod:`aether.adapters.local_adsb`:

- :class:`AprsSource` — the source connection: open the KISS TCP socket, read
  frames, hand each de-framed AX.25 frame to the decoder, and yield TNC2 lines.
  RECEIVE-ONLY: only the read half is used; the write half is closed immediately
  and never written to, so aether can never ask Dire Wolf to transmit (PRD §18.3).
- :class:`ThrottleGate` — the §18.1 per-station publish gate (same shape as the
  ADS-B gate; emergency bypass kept for parity though the APRS parser emits no
  ``emergency`` tag yet — it lands with Mic-E emergency, deferred).
- :func:`local_aprs_records` — the ``records()`` contract: ``starting``, then
  decode -> parse -> throttle -> emit, with a ``connected`` status carrying
  connection health and ``records_received``/``records_rejected``. A dropped
  socket yields ``degraded`` and re-opens a fresh connection rather than ending
  the stream (PRD §17.4, §37).
- :func:`run_local_aprs` — bus connection + jittered exponential backoff on broker
  loss, building a FRESH records generator (and a fresh source) per reconnect.

Source-status honesty: we report OUR connection health + record counts only. The
iGate gating decisions/counts are Dire Wolf's; KISS carries decoded frames, not
gating decisions, so they are not reconstructed here (PRD §18.3).
"""

import asyncio
import logging
import random
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any, Literal

import aiomqtt

from aether.adapters.aprs import SOURCE, parse_aprs_packet
from aether.adapters.aprs_kiss import MAX_LINE_BYTES, iter_kiss_frames, kiss_frame_to_tnc2
from aether.bus.client import connect
from aether.config import Settings
from aether.schema.records import Record, SourceStatusRecord

log = logging.getLogger(__name__)

#: Jittered exponential backoff bounds for source/bus retries (PRD §17.1).
INITIAL_BACKOFF_S = 1.0
MAX_BACKOFF_S = 30.0

#: Stable id for this source's retained health record (PRD §23 status stream).
STATUS_ID = f"source_status:{SOURCE}"

#: Read size per socket read; APRS frames are tiny so this drains a burst at once.
_READ_CHUNK = 4096

#: Floor for the throttle-gate eviction TTL. APRS stations beacon on the order of
#: minutes, so an entry untouched for an hour is from a station that has gone
#: quiet/out of range — its throttle state is dead weight and can be dropped.
_GATE_MIN_TTL_S = 3600.0

#: Hard cap on distinct stations the gate tracks. A genuinely busy 144.39 channel
#: hears a few hundred stations a day; this leaves generous headroom while turning
#: an adversarial flood of junk callsigns into a bounded eviction instead of
#: unbounded growth (PRD §37 failure isolation).
_GATE_MAX_ENTRIES = 4096


def _now() -> datetime:
    return datetime.now(UTC)


def _backoff(delay: float) -> tuple[float, float]:
    """Full-jitter backoff: sleep a random slice of ``delay``, then double it.

    Returns ``(sleep_for, next_delay)``. Jitter avoids a thundering-herd retry
    when the source or the broker comes back (PRD §17.1, §17.4). Identical to the
    ADS-B runner's backoff so both adapters behave the same on reconnect.
    """
    capped = min(delay, MAX_BACKOFF_S)
    sleep_for = random.uniform(0.0, capped)
    return sleep_for, min(capped * 2.0, MAX_BACKOFF_S)


def _status(
    status: Literal["starting", "connected", "degraded", "stale", "offline", "disabled"],
    now: datetime,
    *,
    records_received: int = 0,
    records_rejected: int = 0,
    last_record_at: datetime | None = None,
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
        records_received=records_received,
        records_rejected=records_rejected,
        error_code=error_code,
        error_summary=error_summary,
        attributes=attributes or {},
    )


class AprsSource:
    """Reads Dire Wolf's KISS TCP port and yields decoded TNC2 monitor lines.

    Owns the socket lifecycle: connect with a timeout, then read forever, carrying
    a partial-frame buffer across reads so a frame split over two TCP reads is
    reassembled. Each complete KISS frame is de-framed + AX.25-decoded to a TNC2
    line by :mod:`aether.adapters.aprs_kiss`; ``None`` results (command frames,
    non-UI, malformed) are simply not yielded.

    RECEIVE-ONLY: only the reader half is consumed. The writer half is closed
    immediately on connect and never written to — aether never sends a KISS frame
    back toward the TNC, which is what would ask Dire Wolf to transmit (PRD §18.3).
    """

    def __init__(self, host: str, port: int, *, timeout_s: float = 5.0) -> None:
        self._host = host
        self._port = port
        self._timeout_s = timeout_s
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None

    @property
    def host(self) -> str:
        return self._host

    @property
    def port(self) -> int:
        return self._port

    async def frames(self) -> AsyncIterator[str]:
        """Connect, then yield TNC2 lines until the socket closes or errors.

        Raises ``ConnectionError`` when the TNC closes the socket (empty read) so
        :func:`local_aprs_records` can mark the source degraded and reconnect. The
        read loop has no per-read timeout: an idle APRS channel is normal, not an
        error — only the initial connect is bounded by ``timeout_s``.
        """
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(self._host, self._port), self._timeout_s
        )
        self._reader, self._writer = reader, writer
        # Receive-only: we never write to the KISS socket. Half-close the write
        # side immediately so nothing can ever push a frame toward the TNC.
        try:
            if writer.can_write_eof():
                writer.write_eof()
        except OSError:  # some transports disallow half-close; just never write
            pass

        buffer = b""
        while True:
            chunk = await reader.read(_READ_CHUNK)
            if chunk == b"":  # TNC closed the socket
                raise ConnectionError("KISS socket closed by peer")
            buffer += chunk
            kiss_frames, buffer = iter_kiss_frames(buffer)
            for kiss_frame in kiss_frames:
                line = kiss_frame_to_tnc2(kiss_frame)
                if line is not None:
                    yield line
            if len(buffer) > MAX_LINE_BYTES:  # belt-and-braces; decoder also caps
                buffer = b""

    async def close(self) -> None:
        """Close the socket. Closing only; we never wrote to it (receive-only)."""
        writer = self._writer
        self._reader = self._writer = None
        if writer is None:
            return
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass


class ThrottleGate:
    """Per-station publish gate enforcing the §18.1 update policy.

    Admits a track when at least ``interval_s`` has elapsed since its last publish,
    or immediately on an emergency transition (not-emergency -> emergency). Same
    shape as the ADS-B gate; the emergency path is best-effort parity (the APRS
    parser emits no ``emergency`` tag yet — that arrives with Mic-E emergency).

    Memory is kept bounded by time/size eviction, NOT by snapshot pruning. The
    ADS-B twin sees a full live set each poll and prunes to it; this per-packet
    stream never sees such a snapshot, so on every admit it evicts any station
    untouched for ``ttl_s`` (default :data:`_GATE_MIN_TTL_S`, never below
    ``interval_s``) and, as a backstop against a junk-callsign flood, drops the
    oldest entries once the table exceeds :data:`_GATE_MAX_ENTRIES`. A multi-day
    soak on a busy channel therefore stays bounded (PRD §17.3, §37).
    """

    def __init__(self, interval_s: float, *, ttl_s: float | None = None) -> None:
        self._interval_s = interval_s
        # An entry stays alive at least one throttle interval, and at least the
        # min-TTL floor, so eviction never races the throttle it backs.
        self._ttl_s = max(interval_s, ttl_s if ttl_s is not None else _GATE_MIN_TTL_S)
        self._last_published: dict[str, datetime] = {}
        self._emergency: dict[str, bool] = {}

    def admit(self, track_id: str, now: datetime, *, emergency: bool) -> bool:
        self._evict(now, incoming=track_id)
        was_emergency = self._emergency.get(track_id, False)
        self._emergency[track_id] = emergency
        last = self._last_published.get(track_id)
        due = last is None or (now - last).total_seconds() >= self._interval_s
        if (emergency and not was_emergency) or due:
            self._last_published[track_id] = now
            return True
        return False

    def _evict(self, now: datetime, *, incoming: str) -> None:
        """Drop dead/overflow entries so the gate's tables can't grow unbounded.

        Time eviction: any station whose last publish is older than ``ttl_s`` is
        gone (quiet/out of range) and forgotten. Size eviction: if the table would
        still exceed :data:`_GATE_MAX_ENTRIES` (e.g. a junk-callsign flood inside
        one TTL window), drop the oldest-published entries so that after the caller
        inserts ``incoming`` the table holds at most the cap — one slot of headroom
        is reserved when ``incoming`` is a new id. Both tables stay in lockstep
        (PRD §37).
        """
        dead = [
            tid
            for tid, last in self._last_published.items()
            if (now - last).total_seconds() > self._ttl_s
        ]
        for tid in dead:
            del self._last_published[tid]
            self._emergency.pop(tid, None)
        # Reserve a slot for the pending insert only when it's a genuinely new id;
        # an existing id reuses its slot and needs no headroom.
        reserve = 0 if incoming in self._last_published else 1
        overflow = len(self._last_published) + reserve - _GATE_MAX_ENTRIES
        if overflow > 0:
            oldest = sorted(self._last_published, key=self._last_published.__getitem__)[:overflow]
            for tid in oldest:
                del self._last_published[tid]
                self._emergency.pop(tid, None)


async def local_aprs_records(
    source: AprsSource,
    *,
    throttle_s: float = 1.0,
) -> AsyncIterator[Record]:
    """Yield the local APRS record stream: status, then throttled tracks + health.

    Emits ``starting`` immediately, then connects and streams. Each decoded TNC2
    line is parsed (:func:`parse_aprs_packet`); a deferred/garbage line (Mic-E,
    telemetry, messages, junk) counts as ``records_rejected``, an admitted track
    counts as ``records_received`` and is yielded, and a ``connected`` status with
    the running counts is emitted after each line. A socket error yields a
    ``degraded`` status, backs off with jitter, and RE-OPENS a fresh connection —
    one dropped socket never ends the stream (PRD §17.4, §37).

    Connection-health attributes only: ``records_received``/``records_rejected``
    are ours; iGate gating counts are Dire Wolf's and not recoverable from KISS.
    """
    yield _status("starting", _now())
    gate = ThrottleGate(throttle_s)
    received = 0
    rejected = 0
    backoff = INITIAL_BACKOFF_S
    attrs: dict[str, Any] = {
        "connection": "kiss",
        "host": source.host,
        "port": source.port,
    }
    while True:
        try:
            async for line in source.frames():
                backoff = INITIAL_BACKOFF_S  # a live read means we're connected
                now = _now()
                try:
                    track = parse_aprs_packet(line, received_at=now, source=SOURCE)
                except Exception:  # one bad packet must not drop the rest of the stream
                    # Mirrors parse_aprs_lines' per-line guard: a parser edge on one
                    # decoded line is contained as a rejected record, never unwinds
                    # the stream and crashes the adapter (PRD §17.2, §37).
                    log.warning("skipping malformed APRS packet", exc_info=True)
                    rejected += 1
                    yield _status(
                        "connected",
                        now,
                        records_received=received,
                        records_rejected=rejected,
                        attributes=attrs,
                    )
                    continue
                if track is None:
                    # Deferred (Mic-E/telemetry/message) or junk: counted, not shown.
                    rejected += 1
                    yield _status(
                        "connected",
                        now,
                        records_received=received,
                        records_rejected=rejected,
                        attributes=attrs,
                    )
                    continue
                if gate.admit(track.id, now, emergency="emergency" in track.tags):
                    received += 1
                    yield track
                    yield _status(
                        "connected",
                        now,
                        records_received=received,
                        records_rejected=rejected,
                        last_record_at=track.observed_at,
                        attributes=attrs,
                    )
        except (TimeoutError, ConnectionError, OSError) as exc:
            now = _now()
            log.warning("local APRS socket error (%s); backing off", exc)
            yield _status(
                "degraded",
                now,
                records_received=received,
                records_rejected=rejected,
                error_code=type(exc).__name__,
                error_summary=str(exc)[:200],
                attributes=attrs,
            )
            await source.close()
            sleep_for, backoff = _backoff(backoff)
            await asyncio.sleep(sleep_for)
            # Re-open the SAME source object (a fresh socket) and resume the stream.
            continue
        # frames() returned without an error (the async-for fell through): the
        # socket closed cleanly. Treat like a drop and reconnect.
        await source.close()
        sleep_for, backoff = _backoff(backoff)
        await asyncio.sleep(sleep_for)


async def run_local_aprs(
    cfg: Settings,
    ready: asyncio.Event,
    *,
    throttle_s: float | None = None,
) -> None:
    """Pump the local APRS stream onto the bus until cancelled (PRD §17.1).

    Waits for the subscriber to be live (avoids a startup race), then publishes the
    :func:`local_aprs_records` stream. A broker drop triggers a jittered
    exponential reconnect rather than crashing the lifespan.

    A FRESH records generator (and a fresh :class:`AprsSource`) is built per bus
    connection: an ``MqttError`` raised mid-publish unwinds the ``async for`` and
    (PEP 525) closes the generator, which cannot be resumed — reusing it would
    silently end the adapter after the first reconnect (the M2.1b lesson). APRS has
    no per-connection cache to preserve, so creating the source inside the loop is
    both simpler and equally correct.
    """
    await ready.wait()
    resolved_throttle = throttle_s if throttle_s is not None else cfg.local_aprs_throttle_s
    log.info("local APRS adapter -> %s:%d", cfg.local_aprs_host, cfg.local_aprs_port)
    backoff = INITIAL_BACKOFF_S
    while True:
        try:
            async with connect(cfg, identifier="aether-local-aprs") as bus:
                backoff = INITIAL_BACKOFF_S  # reset once connected
                source = AprsSource(
                    cfg.local_aprs_host,
                    cfg.local_aprs_port,
                    timeout_s=cfg.local_aprs_timeout_s,
                )
                async for record in local_aprs_records(source, throttle_s=resolved_throttle):
                    await bus.publish_record(record)
                return  # generator exhausted (only on cancellation in practice)
        except aiomqtt.MqttError as exc:
            sleep_for, backoff = _backoff(backoff)
            log.warning("local APRS lost broker (%s); reconnecting in %.1fs", exc, sleep_for)
            await asyncio.sleep(sleep_for)

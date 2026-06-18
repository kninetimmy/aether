"""APRS-IS display adapter runner (PRD §18.4, §11.7, §17.1, §17.3).

The Internet APRS feed for the station's area of interest — the network sibling of
the local Dire Wolf adapter (:mod:`aether.adapters.local_aprs`). It connects to an
APRS-IS server, applies a server-side range filter covering the configured AOI,
reads TNC2 monitor lines, normalizes each through the *shared* APRS parser
(:func:`aether.adapters.aprs.parse_aprs_packet`, with ``local_rf=False``), and
pumps schema-v2 ``TrackRecord``\\s onto the bus. Because both APRS adapters mint the
same ``aprs:station:<CALL>`` / ``aprs:object:<NAME>`` identity (independent of
source), the backend's fusion engine collapses a local-RF and an APRS-IS
observation of the same callsign into ONE track with both provenance paths — the
M3 exit criterion (PRD §11.7, §15.3).

Responsibility split mirrors :mod:`aether.adapters.local_aprs` and
:mod:`aether.adapters.network_adsb`:

- :func:`build_login` / :func:`aprs_is_filter` — pure builders for the APRS-IS
  login line and the server-side range filter (unit-testable in isolation).
- :class:`AprsIsSource` — the socket: connect, send the login ONCE, read TNC2
  lines, detect a stalled/silent feed via a last-line timeout (PRD §17.3).
- :class:`DuplicateFilter` — drop exact packets re-relayed by multiple igates
  within the classic ~30 s APRS dupe window (PRD §18.4); the local RF adapter
  never needs this (one antenna, no multi-igate fan-out).
- :func:`aprs_is_records` — the ``records()`` contract: ``starting``, then
  connect → dedup → parse → throttle → emit, with ``connected``/``degraded``
  health. A dropped/stalled socket reconnects (re-login = resubscribe) rather than
  ending the stream (PRD §17.4, §37).
- :func:`run_aprs_is` — bus connection + jittered exponential backoff on broker
  loss, building a FRESH records generator (and source) per reconnect (PEP 525 /
  M2.1b lesson). A missing/invalid callsign fails *visibly* as an ``offline``
  source status, never silently and never as the maintainer's identity.

**Receive-only / no RF transmit (PRD §2, §18.4):** the login line is the ONLY
thing aether ever writes, and with passcode ``-1`` the connection is receive-only
and cannot inject packets into APRS-IS. This is an Internet *read* subscription —
there is no RF path here at all (unlike the KISS adapter, which half-closes its
write side precisely because a KISS write WOULD key a transmitter). aether never
re-gates APRS-IS packets and never opens an Internet-to-RF path.
"""

import asyncio
import logging
import math
import random
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any, Literal

import aiomqtt

from aether.adapters.aprs import parse_aprs_packet
from aether.adapters.local_aprs import ThrottleGate
from aether.bus.client import connect
from aether.config import Settings
from aether.schema.records import Record, SourceStatusRecord

log = logging.getLogger(__name__)

#: Per-source identifier; the MQTT topic suffix (PRD §23: ``records/aprs_is``) and
#: the freshness-table key. Distinct from ``local_aprs`` so the two APRS legs carry
#: separate provenance/health while fusing on the shared identity key.
SOURCE = "aprs_is"

#: Stable id for this source's retained health record (PRD §23 status stream).
STATUS_ID = f"source_status:{SOURCE}"

#: Jittered exponential backoff bounds for source/bus retries (PRD §17.1). Shared
#: shape with every other adapter so a downed feed/broker is retried the same way.
INITIAL_BACKOFF_S = 1.0
MAX_BACKOFF_S = 30.0

#: NM → km for the APRS-IS range filter, whose distance is in km (aprs-is.net
#: javAPRSFilter.aspx). The configured AOI radius is in NM (PRD §16.2).
_NM_TO_KM = 1.852

#: APRS-IS lines are ≤512 bytes including CR/LF (aprs-is.net Connecting.aspx); a
#: longer line is not a valid APRS-IS line. Cap a little higher and skip anything
#: past it, and bound the stream reader so a newline-less flood can't grow memory
#: (PRD §17.2 payload limits, §37 failure isolation).
_MAX_LINE_BYTES = 600

#: Default APRS duplicate-detection window. The APRS spec dedupe interval is ~30 s;
#: a repeat of the exact same packet inside it is a multi-igate relay, not a new
#: observation (PRD §18.4).
_DUP_TTL_S = 30.0
#: Hard cap on distinct packet signatures tracked, so an adversarial flood of
#: unique junk lines becomes bounded eviction, not unbounded growth (PRD §37).
_DUP_MAX_ENTRIES = 8192

#: Software identity advertised in the login line. Not a transmit capability — it
#: is the conventional ``vers`` field every APRS-IS client sends.
_SOFTWARE_NAME = "aether"
_SOFTWARE_VERSION = "0.1"


def _now() -> datetime:
    return datetime.now(UTC)


def _backoff(delay: float) -> tuple[float, float]:
    """Full-jitter backoff: sleep a random slice of ``delay``, then double it.

    Returns ``(sleep_for, next_delay)``. Identical to the other adapters' backoff
    so a downed feed/broker is retried the same way everywhere (PRD §17.1, §17.4).
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


def aprs_is_filter(center_lat: float, center_lon: float, radius_nm: float) -> str:
    """Build the APRS-IS server-side range filter ``r/lat/lon/dist`` (PRD §18.4, §16.2).

    ``dist`` is in KILOMETRES per the filter spec, so the configured NM AOI radius
    is converted; ``lat``/``lon`` are signed decimal degrees. This is what keeps
    APRS-IS traffic inside the operator's area of interest (APRSIS-FR-003) rather
    than firehosing the whole world.

    Validates the operator-supplied AOI is finite and in WGS 84 bounds, raising
    ``ValueError`` otherwise. A non-finite ``nan``/``inf`` would format into a
    syntactically-valid-but-semantically-broken filter (``r/nan/0/926``) that the
    server silently rejects — leaving the adapter "connected" yet deaf with no
    diagnostic. Raising here routes a bad AOI through :func:`run_aprs_is`'s
    ``ConfigError`` path so it fails *visibly* as an ``offline`` status, the same
    stance as a missing callsign (PRD §2/§37 fail-visibly).
    """
    if not (math.isfinite(center_lat) and -90.0 <= center_lat <= 90.0):
        raise ValueError(f"APRS-IS center latitude out of WGS 84 range: {center_lat!r}")
    if not (math.isfinite(center_lon) and -180.0 <= center_lon <= 180.0):
        raise ValueError(f"APRS-IS center longitude out of WGS 84 range: {center_lon!r}")
    if not (math.isfinite(radius_nm) and radius_nm > 0.0):
        raise ValueError(f"APRS-IS radius must be a positive finite number of NM: {radius_nm!r}")
    dist_km = radius_nm * _NM_TO_KM
    return f"r/{center_lat:.5f}/{center_lon:.5f}/{dist_km:.0f}"


def build_login(
    callsign: str,
    passcode: str,
    filter_str: str,
    *,
    software: str = _SOFTWARE_NAME,
    version: str = _SOFTWARE_VERSION,
) -> str:
    """Build the APRS-IS login line (PRD §18.4).

    Format (aprs-is.net/Connecting.aspx):
    ``user <CALL> pass <PASS> vers <software> <version> filter <filter>``. This is
    the ONLY line aether ever writes to APRS-IS; with passcode ``-1`` the session
    is receive-only and cannot inject packets.

    Validates the operator-supplied inputs so a bad config fails *visibly* and
    cannot do login injection:

    - An empty callsign raises — the adapter is opt-in, and we must never default
      to the maintainer's identity or a baked-in callsign (PRD §2, §37).
    - Any whitespace in the callsign or passcode raises — both are single tokens on
      the one login line. A CR/LF would break out into a second protocol line; a
      space/tab would rewrite the line's token structure (e.g. smuggle extra
      ``vers``/``filter`` terms). The filter is allowed internal spaces (a
      multi-term filter is space-separated) but never CR/LF.
    """
    call = callsign.strip()
    if not call:
        raise ValueError("APRS-IS callsign is required (set AETHER_APRS_IS_CALLSIGN)")
    for label, value in (("callsign", call), ("passcode", passcode)):
        if any(ch.isspace() for ch in value):
            raise ValueError(f"APRS-IS {label} must not contain whitespace")
    if "\r" in filter_str or "\n" in filter_str:
        raise ValueError("APRS-IS filter must not contain CR/LF")
    return f"user {call} pass {passcode} vers {software} {version} filter {filter_str}"


def dup_signature(line: str) -> str:
    """Signature identifying the same APRS packet across igate relays (PRD §18.4).

    APRS-IS relays the same transmission from every igate that heard it, each with
    a *different* path/q-construct (e.g. ``,qAR,IGATE1`` vs ``,qAO,IGATE2``). The
    underlying packet is the source callsign, the destination, and the info field —
    the path is exactly what differs between relays — so the signature drops the
    path: ``SRC>DEST:info`` from ``SRC>DEST,DIGI,qAR,IGATE:info``.
    """
    header, _, info = line.partition(":")
    src_dest = header.split(",", 1)[0]
    return f"{src_dest}:{info}"


class DuplicateFilter:
    """Bounded TTL dedup of identical APRS-IS packets (PRD §18.4, §37).

    :meth:`admit` returns ``False`` for a signature seen within ``ttl_s`` (a
    multi-igate relay of the same packet) and ``True`` otherwise — including the
    *first* sighting and any sighting after the window elapses, so a station
    re-beaconing the same position 30 s later is admitted as a fresh observation
    (which keeps its fused track live). First-seen time is NOT refreshed on a
    duplicate, or a continuously-relayed packet would never re-admit. Memory stays
    bounded by time eviction plus an oldest-first size cap.
    """

    def __init__(self, *, ttl_s: float = _DUP_TTL_S, max_entries: int = _DUP_MAX_ENTRIES) -> None:
        self._ttl_s = ttl_s
        self._max_entries = max_entries
        self._seen: dict[str, datetime] = {}

    def admit(self, signature: str, now: datetime) -> bool:
        self._evict(now, incoming=signature)
        last = self._seen.get(signature)
        if last is not None and (now - last).total_seconds() < self._ttl_s:
            return False  # exact packet re-relayed within the dupe window: drop
        self._seen[signature] = now
        return True

    def _evict(self, now: datetime, *, incoming: str) -> None:
        """Drop entries older than ``ttl_s``, then oldest-first past the size cap.

        The size reserve is conditional on whether ``incoming`` is a *new* key, like
        the sibling :class:`~aether.adapters.local_aprs.ThrottleGate`: an in-window
        duplicate (``admit`` returns False, inserts nothing) reuses its slot and
        needs no headroom, so an unconditional ``+1`` would over-evict one unrelated
        still-valid signature a window early at capacity.
        """
        dead = [s for s, t in self._seen.items() if (now - t).total_seconds() > self._ttl_s]
        for sig in dead:
            del self._seen[sig]
        reserve = 0 if incoming in self._seen else 1
        overflow = len(self._seen) + reserve - self._max_entries
        if overflow > 0:
            oldest = sorted(self._seen, key=self._seen.__getitem__)[:overflow]
            for sig in oldest:
                del self._seen[sig]


class AprsIsSource:
    """Connects to an APRS-IS server, logs in, and yields TNC2 monitor lines.

    Owns the socket lifecycle: connect (bounded by ``timeout_s``), send the login
    line ONCE, then read lines until the socket closes/errors or goes silent. Lines
    are CR/LF-terminated; ``#`` lines are server comments/keepalives and are yielded
    too (the runner uses them as liveness, not as packets). An overlong line is
    skipped (not a valid APRS-IS line), and the stream reader is bounded so a
    newline-less flood cannot grow memory.

    Stall detection (PRD §17.3): APRS-IS sends a ``#`` keepalive roughly every 20 s,
    so genuine silence for ``stall_s`` means a dead/half-open socket — a read that
    times out raises ``ConnectionError`` so the runner reconnects.

    RECEIVE-ONLY: the login is the only write; with passcode ``-1`` the session
    cannot transmit to APRS-IS, and there is no RF path at all.
    """

    def __init__(
        self,
        host: str,
        port: int,
        login_line: str,
        *,
        timeout_s: float = 10.0,
        stall_s: float = 60.0,
    ) -> None:
        self._host = host
        self._port = port
        self._login_line = login_line
        self._timeout_s = timeout_s
        self._stall_s = stall_s
        self._writer: asyncio.StreamWriter | None = None

    @property
    def host(self) -> str:
        return self._host

    @property
    def port(self) -> int:
        return self._port

    async def lines(self) -> AsyncIterator[str]:
        """Connect, send the login, then yield stripped non-empty lines forever.

        Raises ``ConnectionError`` when the server closes the socket (empty read),
        when no line arrives within ``stall_s`` (silent/stalled feed), or when a
        line overruns the reader buffer — each is recoverable by reconnecting.
        """
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(self._host, self._port, limit=_MAX_LINE_BYTES * 2),
            self._timeout_s,
        )
        self._writer = writer
        # The login line is the ONLY write (receive-only; passcode -1 cannot TX).
        writer.write((self._login_line + "\r\n").encode("ascii", "replace"))
        await writer.drain()

        while True:
            try:
                raw = await asyncio.wait_for(reader.readline(), self._stall_s)
            except TimeoutError as exc:  # no data (not even a keepalive): dead socket
                raise ConnectionError("APRS-IS stalled: no data within keepalive window") from exc
            except ValueError as exc:  # line exceeded the reader limit: garbage stream
                raise ConnectionError("APRS-IS line exceeded the buffer limit") from exc
            if raw == b"":  # server closed the socket
                raise ConnectionError("APRS-IS socket closed by peer")
            if len(raw) > _MAX_LINE_BYTES:  # over-spec line: not valid APRS-IS, skip
                continue
            line = raw.decode("ascii", "replace").strip()
            if line:
                yield line

    async def close(self) -> None:
        """Close the socket. We only ever wrote the login line; nothing else."""
        writer = self._writer
        self._writer = None
        if writer is None:
            return
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass


async def aprs_is_records(
    source: AprsIsSource,
    *,
    throttle_s: float = 1.0,
    dup_ttl_s: float = _DUP_TTL_S,
    filter_str: str = "",
) -> AsyncIterator[Record]:
    """Yield the APRS-IS record stream: status, then deduped/throttled tracks + health.

    Emits ``starting`` immediately, then connects/logs in and streams. For each
    line: a ``#`` server comment/keepalive yields a ``connected`` liveness status
    (and surfaces the login response / server name); a real TNC2 packet is
    de-duplicated against multi-igate relays (PRD §18.4), parsed with
    ``local_rf=False`` (network-only provenance until fused), throttled per station
    (§18.1), and yielded with a ``connected`` status carrying running counts. A
    line the parser cannot use (Mic-E/telemetry/message/junk) is a
    ``records_rejected``. A socket/stall error yields ``degraded``, backs off with
    jitter, and RE-OPENS a fresh connection (re-login = resubscribe) — one dropped
    socket never ends the stream (PRD §17.4, §37 failure isolation).
    """
    yield _status("starting", _now())
    gate = ThrottleGate(throttle_s)
    dedup = DuplicateFilter(ttl_s=dup_ttl_s)
    received = 0
    rejected = 0
    duplicates = 0
    backoff = INITIAL_BACKOFF_S
    attrs: dict[str, Any] = {
        "connection": "aprs-is",
        "server": source.host,
        "port": source.port,
        "filter": filter_str,
    }

    def health(now: datetime, **extra: Any) -> SourceStatusRecord:
        return _status(
            "connected",
            now,
            records_received=received,
            records_rejected=rejected,
            attributes={**attrs, "duplicates": duplicates, **extra},
        )

    while True:
        try:
            async for line in source.lines():
                backoff = INITIAL_BACKOFF_S  # a live read means we're connected
                now = _now()
                if line.startswith("#"):
                    # Server comment / keepalive / login response: proves the link
                    # is alive (incl. the first `# logresp ...` right after login).
                    yield health(now, server_message=line[1:].strip()[:120])
                    continue
                if not dedup.admit(dup_signature(line), now):
                    duplicates += 1  # exact multi-igate relay of a packet we have
                    continue
                try:
                    track = parse_aprs_packet(line, received_at=now, source=SOURCE, local_rf=False)
                except Exception:  # one bad packet must not drop the rest of the stream
                    log.warning("skipping malformed APRS-IS packet", exc_info=True)
                    rejected += 1
                    yield health(now)
                    continue
                if track is None:
                    # Deferred (Mic-E/telemetry/message) or junk: counted, not shown.
                    rejected += 1
                    yield health(now)
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
                        attributes={**attrs, "duplicates": duplicates},
                    )
        except (TimeoutError, ConnectionError, OSError, ValueError) as exc:
            now = _now()
            log.warning("APRS-IS connection error (%s); backing off", exc)
            yield _status(
                "degraded",
                now,
                records_received=received,
                records_rejected=rejected,
                error_code=type(exc).__name__,
                error_summary=str(exc)[:200],
                attributes={**attrs, "duplicates": duplicates},
            )
            await source.close()
            sleep_for, backoff = _backoff(backoff)
            await asyncio.sleep(sleep_for)
            continue  # re-open a fresh connection (re-login = resubscribe)
        # lines() returned without raising (clean EOF): treat like a drop, reconnect.
        # The production AprsIsSource.lines() never returns cleanly (it only raises),
        # so this is a guard for a future/test source — log it for parity with the
        # error path so a silent reconnect is never invisible.
        log.warning("APRS-IS stream ended without error; reconnecting")
        await source.close()
        sleep_for, backoff = _backoff(backoff)
        await asyncio.sleep(sleep_for)


async def run_aprs_is(
    cfg: Settings,
    ready: asyncio.Event,
    *,
    throttle_s: float | None = None,
) -> None:
    """Pump the APRS-IS stream onto the bus until cancelled (PRD §17.1).

    Waits for the subscriber to be live (avoids a startup race), then publishes the
    :func:`aprs_is_records` stream. A broker drop triggers a jittered exponential
    reconnect rather than crashing the lifespan.

    A FRESH records generator (and a fresh :class:`AprsIsSource`) is built per bus
    connection: an ``MqttError`` raised mid-publish unwinds the ``async for`` and
    (PEP 525) closes the generator, which cannot be resumed — reusing it would
    silently end the adapter after the first reconnect (the M2.1b lesson). The
    login/filter are rebuilt inside the loop so a misconfiguration (e.g. a missing
    callsign) is reported as an ``offline`` source status over the bus, then the
    task exits cleanly — a config error will not self-heal, so we do not spin.
    """
    await ready.wait()
    resolved_throttle = throttle_s if throttle_s is not None else cfg.aprs_is_throttle_s
    backoff = INITIAL_BACKOFF_S
    while True:
        try:
            async with connect(cfg, identifier="aether-aprs-is") as bus:
                backoff = INITIAL_BACKOFF_S  # reset once connected
                try:
                    filter_str = aprs_is_filter(
                        cfg.aprs_is_center_lat, cfg.aprs_is_center_lon, cfg.aprs_is_radius_nm
                    )
                    login_line = build_login(cfg.aprs_is_callsign, cfg.aprs_is_passcode, filter_str)
                except ValueError as exc:
                    log.error("APRS-IS misconfigured: %s", exc)
                    await bus.publish_record(
                        _status(
                            "offline",
                            _now(),
                            error_code="ConfigError",
                            error_summary=str(exc)[:200],
                            attributes={"connection": "aprs-is"},
                        )
                    )
                    return  # config won't self-heal; don't spin
                log.info(
                    "APRS-IS adapter -> %s:%d as %s (filter %s)",
                    cfg.aprs_is_host,
                    cfg.aprs_is_port,
                    cfg.aprs_is_callsign,
                    filter_str,
                )
                source = AprsIsSource(
                    cfg.aprs_is_host,
                    cfg.aprs_is_port,
                    login_line,
                    timeout_s=cfg.aprs_is_timeout_s,
                    stall_s=cfg.aprs_is_stall_s,
                )
                async for record in aprs_is_records(
                    source, throttle_s=resolved_throttle, filter_str=filter_str
                ):
                    await bus.publish_record(record)
                return  # generator exhausted (only on cancellation in practice)
        except aiomqtt.MqttError as exc:
            sleep_for, backoff = _backoff(backoff)
            log.warning("APRS-IS lost broker (%s); reconnecting in %.1fs", exc, sleep_for)
            await asyncio.sleep(sleep_for)

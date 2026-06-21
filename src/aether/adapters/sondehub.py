"""SondeHub radiosonde adapter (PRD §11.9, §18.6, §17.1, M5.2).

Ingests active radiosonde telemetry from `SondeHub <https://sondehub.org>`_ — the
crowd-sourced network that aggregates the balloon-borne weather sondes hobbyists
receive on ~400 MHz — and normalizes each frame to a schema-v2 ``TrackRecord``
(``track_type="radiosonde"``) carrying serial, sonde type, altitude, ground speed,
heading, vertical rate, derived ascent/descent state, and the last uploader
(SONDE-FR-001/005). Sondes outside the configured AOI are dropped at the adapter
edge (SONDE-FR-004), and an unchanged frame is emitted once (dedupe by serial +
frame number) so a static sonde is not re-upserted every poll.

**Transport (SONDE-FR-002/003).** SondeHub's *preferred* live transport is its
presigned MQTT-over-WebSocket stream; this slice implements the documented **REST
polling** path (``GET /sondes?lat&lon&distance&last``), which is the sanctioned
fallback and the natural sibling of the USGS poll loop. The streaming provider can
be added later behind the same :class:`SondeHubProvider` Protocol without touching
the normalizer or the records loop. Recency (SONDE-FR-004) is bounded server-side by
the ``last`` window.

Read-only Internet read: aether only *fetches* (no key required, within the source
cadence) and never transmits — the network sibling of the USGS / network-ADS-B
adapters, with no RF leg at all (PRD §2, §6 non-goals). The product is **not
authoritative** for sonde positions; records carry SondeHub attribution and report
telemetry verbatim, and a *predicted* landing (when added in M5.2b) will be labeled
a prediction, never an observation (SONDE-FR-006).

Responsibility split mirrors :mod:`aether.adapters.usgs`:

- :class:`SondeHubProvider` / :class:`SondeHubRestProvider` — fetch the raw
  ``{serial: {datetime: frame}}`` telemetry map (the live HTTP feed, or the
  in-process ``fake`` feeder).
- :func:`frame_to_record` — pure SondeHub frame → ``TrackRecord`` normalizer.
- :func:`sondehub_records` — the ``records()`` contract: ``starting``, then a poll
  loop with AOI filtering, serial+frame dedupe, and ``degraded``-on-failure
  isolation (a failed fetch keeps the last good sondes on the map, never crashes).
- :func:`run_sondehub` — bus connection + jittered exponential backoff on broker loss.
"""

import asyncio
import functools
import json
import logging
import math
import random
import urllib.request
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, Literal, Protocol
from urllib.parse import urlencode

import aiomqtt

from aether.alerts.geo import haversine_m
from aether.bus.client import connect
from aether.config import Settings
from aether.schema.geometry import Point
from aether.schema.provenance import Provenance
from aether.schema.records import Record, SourceStatusRecord, TrackRecord

log = logging.getLogger(__name__)

#: Stable source name + retained health-record id (PRD §23 status stream).
SOURCE = "sondehub"
STATUS_ID = f"source_status:{SOURCE}"

#: Provider name recorded in provenance (the aggregator we read).
_PROVIDER = "sondehub"

#: SondeHub publishes a plain attribution credit; carried on every record's status +
#: attributes so the UI can show provenance honestly (PRD §11.2).
ATTRIBUTION = "SondeHub (Project Horus) radiosonde network"

#: 1 nautical mile in metres (AOI radius is configured in NM, distances in metres).
_M_PER_NM = 1852.0

#: Vertical-rate magnitude (m/s) above which a sonde is called ascending/descending;
#: inside the band it is reported as ``float`` (burst/level). A coarse, honest label —
#: derived from the reported ``vel_v``, never a guess about flight phase.
_VERT_RATE_EPS = 1.0

#: Jittered exponential backoff bounds — shared shape with every other adapter so a
#: downed feed/broker is retried the same way everywhere (PRD §17.1).
INITIAL_BACKOFF_S = 1.0
MAX_BACKOFF_S = 60.0

#: Provider-name aliases selecting the in-process no-hardware feeder.
_FAKE_PROVIDER_NAMES = frozenset({"fake", "demo"})

#: Hard cap on a single feed response; a runaway body is truncated rather than read
#: unbounded into memory (PRD §17.2). The AOI sonde set is well under this.
MAX_RESPONSE_BYTES = 16 * 1024 * 1024


def _now() -> datetime:
    return datetime.now(UTC)


def _parse_iso(value: Any) -> datetime | None:
    """SondeHub ISO-8601 timestamp (``...Z``) → aware UTC datetime; bad input → ``None``."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _finite_float(value: Any) -> float | None:
    """Coerce a JSON number to a finite float; ``None``/bool/``nan``/``inf`` → ``None``."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    f = float(value)
    return f if math.isfinite(f) else None


def _backoff(delay: float) -> tuple[float, float]:
    """Full-jitter backoff: sleep a random slice of ``delay``, then double it (capped)."""
    capped = min(delay, MAX_BACKOFF_S)
    return random.uniform(0.0, capped), min(capped * 2.0, MAX_BACKOFF_S)


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
        attributes={"attribution": ATTRIBUTION, **(attributes or {})},
    )


# --- Provider (raw feed fetch) -------------------------------------------------


class SondeHubProvider(Protocol):
    """A source of SondeHub telemetry maps (``{serial: {datetime: frame}}``)."""

    name: str

    async def fetch_telemetry(self) -> dict[str, Any]: ...


def _blocking_get(url: str, timeout_s: float) -> bytes:
    if not url.startswith("https://"):  # public SondeHub API is https; never downgrade
        raise ValueError(f"refusing non-https SondeHub URL: {url!r}")
    req = urllib.request.Request(url, headers={"User-Agent": "aether-sondehub/1.0"})
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:  # noqa: S310 (https-checked)
        return bytes(resp.read(MAX_RESPONSE_BYTES + 1))


async def _default_fetch(url: str, *, timeout_s: float) -> bytes:
    # Blocking urllib off the event loop (mirrors the USGS/ADS-B providers); no extra dep.
    return await asyncio.to_thread(_blocking_get, url, timeout_s)


def telemetry_url(
    api_base: str,
    *,
    center_lat: float,
    center_lon: float,
    radius_nm: float,
    recency_s: float,
) -> str:
    """Build the ``GET /sondes`` telemetry URL for the configured AOI (SONDE-FR-004).

    SondeHub filters server-side by ``lat``/``lon``/``distance`` (metres) and bounds
    recency with ``last`` (seconds). The NM radius becomes metres; the AOI is also
    re-checked client-side in :func:`sondehub_records` (defense in depth — a provider
    that ignores the box must not leak distant sondes onto the map).
    """
    query = urlencode(
        {
            "lat": f"{center_lat:.6f}",
            "lon": f"{center_lon:.6f}",
            "distance": int(radius_nm * _M_PER_NM),
            "last": int(recency_s),
        }
    )
    return f"{api_base.rstrip('/')}/sondes?{query}"


class SondeHubRestProvider:
    """Fetch SondeHub telemetry over HTTPS via ``GET /sondes`` (SONDE-FR-003).

    ``fetch`` is injectable so tests drive canned bytes with no network. The default
    GETs the AOI-scoped telemetry URL and parses the ``{serial: {datetime: frame}}``
    map. REST is the documented fallback to the preferred MQTT-WebSocket stream
    (SONDE-FR-002); a streaming provider can later satisfy the same Protocol.
    """

    name = "sondehub-rest"

    def __init__(
        self,
        api_base: str,
        *,
        center_lat: float,
        center_lon: float,
        radius_nm: float,
        recency_s: float,
        timeout_s: float = 10.0,
        fetch: Callable[[str], Awaitable[bytes]] | None = None,
    ) -> None:
        self._url = telemetry_url(
            api_base,
            center_lat=center_lat,
            center_lon=center_lon,
            radius_nm=radius_nm,
            recency_s=recency_s,
        )
        self._fetch = (
            fetch if fetch is not None else functools.partial(_default_fetch, timeout_s=timeout_s)
        )

    async def fetch_telemetry(self) -> dict[str, Any]:
        raw = await self._fetch(self._url)
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("SondeHub /sondes did not return a JSON object")
        return data


def build_provider(cfg: Settings) -> SondeHubProvider:
    """Resolve the configured SondeHub provider (live REST, or the fake feeder)."""
    base = cfg.sondehub_api_base.strip()
    if base.lower() in _FAKE_PROVIDER_NAMES:
        from aether.adapters.sondehub_fake_feeder import FakeSondeHubProvider

        # Place canned sondes at the configured AOI center so the demo always renders.
        return FakeSondeHubProvider(
            center_lat=cfg.sondehub_center_lat, center_lon=cfg.sondehub_center_lon
        )
    return SondeHubRestProvider(
        base,
        center_lat=cfg.sondehub_center_lat,
        center_lon=cfg.sondehub_center_lon,
        radius_nm=cfg.sondehub_radius_nm,
        recency_s=cfg.sondehub_recency_s,
        timeout_s=cfg.sondehub_timeout_s,
    )


# --- Normalization (frame → TrackRecord) ---------------------------------------


def _ascent_state(vel_v: float | None) -> str | None:
    """Coarse, honest flight-phase label derived from reported vertical rate."""
    if vel_v is None:
        return None
    if vel_v > _VERT_RATE_EPS:
        return "ascending"
    if vel_v < -_VERT_RATE_EPS:
        return "descending"
    return "float"


def frame_to_record(
    frame: dict[str, Any], *, serial: str, received_at: datetime
) -> TrackRecord | None:
    """Normalize one SondeHub telemetry frame to a schema-v2 ``TrackRecord``.

    Returns ``None`` for a frame without a usable position (no/garbage lat-lon). The
    outer ``serial`` map key is authoritative for identity. Altitude/ground-speed/
    heading/vertical-rate map to the track's first-class fields; everything else
    displayable (type, manufacturer, uploader, temp/humidity/pressure, sats, batt,
    frequency) plus the derived ascent/descent state goes into attributes
    (SONDE-FR-005). ``vel_h``/``vel_v`` are already m/s — aether's canonical units.
    """
    if not serial.strip():
        return None
    lat = _finite_float(frame.get("lat"))
    lon = _finite_float(frame.get("lon"))
    if lat is None or lon is None or not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        return None

    alt_m = _finite_float(frame.get("alt"))
    vel_h = _finite_float(frame.get("vel_h"))
    vel_v = _finite_float(frame.get("vel_v"))
    heading = _finite_float(frame.get("heading"))
    if heading is not None and not (0.0 <= heading <= 360.0):
        heading = None
    observed_at = _parse_iso(frame.get("datetime")) or received_at
    sonde_type = frame.get("type") if isinstance(frame.get("type"), str) else None
    frame_no = frame.get("frame")

    attributes: dict[str, Any] = {
        "serial": serial,
        "sonde_type": sonde_type,
        "subtype": frame.get("subtype") if isinstance(frame.get("subtype"), str) else None,
        "manufacturer": (
            frame.get("manufacturer") if isinstance(frame.get("manufacturer"), str) else None
        ),
        "frame": frame_no if isinstance(frame_no, int) and not isinstance(frame_no, bool) else None,
        "ascent_state": _ascent_state(vel_v),
        "uploader_callsign": (
            frame.get("uploader_callsign")
            if isinstance(frame.get("uploader_callsign"), str)
            else None
        ),
        "temp_c": _finite_float(frame.get("temp")),
        "humidity_pct": _finite_float(frame.get("humidity")),
        "pressure_hpa": _finite_float(frame.get("pressure")),
        "sats": frame.get("sats") if isinstance(frame.get("sats"), int) else None,
        "batt_v": _finite_float(frame.get("batt")),
        "frequency_mhz": _finite_float(frame.get("frequency")),
        "attribution": ATTRIBUTION,
        "caveat": "SondeHub is authoritative for sonde telemetry; aether displays, not decides.",
    }

    record_id = f"sonde:{serial}"
    label = f"{sonde_type} {serial}" if sonde_type else serial
    return TrackRecord(
        id=record_id,
        source=SOURCE,
        observed_at=observed_at,
        received_at=received_at,
        published_at=received_at,
        # Serial is the stable identity / dedupe key (one track per sonde).
        correlation_key=record_id,
        track_type="radiosonde",
        label=label,
        geometry=Point(coordinates=[lon, lat]),
        altitude_m=alt_m,
        speed_mps=vel_h,
        heading_deg=heading,
        vertical_rate_mps=vel_v,
        locally_received=False,  # network-only Internet feed (no local RF leg)
        provenance=[
            Provenance(
                source=SOURCE,
                provider=_PROVIDER,
                observed_at=observed_at,
                received_at=received_at,
                local_rf=False,
                confidence="high",
            )
        ],
        tags=["radiosonde", "sondehub"],
        attributes=attributes,
    )


def _latest_frame(frames: Any) -> dict[str, Any] | None:
    """Pick the newest frame from one sonde's ``{datetime: frame}`` map.

    SondeHub keys a sonde's frames by ISO timestamp; the newest key is the current
    telemetry. Tolerates a bare frame dict (no time nesting) for robustness.
    """
    if not isinstance(frames, dict) or not frames:
        return None
    if "lat" in frames and "lon" in frames:  # already a single frame, not a {time: frame} map
        return frames
    latest_key = max((k for k in frames if isinstance(k, str)), default=None)
    if latest_key is None:
        return None
    frame = frames[latest_key]
    return frame if isinstance(frame, dict) else None


# --- Records stream + bus pump -------------------------------------------------


async def sondehub_records(
    provider: SondeHubProvider,
    *,
    center_lat: float,
    center_lon: float,
    radius_nm: float,
    poll_s: float = 30.0,
) -> AsyncIterator[Record]:
    """Yield the SondeHub record stream: ``starting``, then sondes + health each poll.

    Each poll fetches the telemetry map once, takes the newest frame per sonde, drops
    sondes outside the AOI disk, and yields one ``TrackRecord`` per *new* frame — an
    unchanged sonde (same frame number / observed time) is skipped so a static sonde
    is not re-upserted every poll. A fetch failure yields ``degraded`` (keeping the
    last good sondes on the map) and backs off with jitter before retrying — failure
    isolation (PRD §17.4/§37).
    """
    yield _status("starting", _now())
    radius_m = radius_nm * _M_PER_NM
    received = 0
    backoff = INITIAL_BACKOFF_S
    #: serial → last (frame number or observed time) emitted, so a re-poll yields only
    #: advanced frames. Pruned to the current in-AOI set every poll so it can't grow
    #: unbounded over a long soak (a sonde aging out is forgotten; if it returns it
    #: re-emits).
    seen: dict[str, Any] = {}

    while True:
        now = _now()
        try:
            data = await provider.fetch_telemetry()
        except Exception as exc:  # a bad fetch must not crash the adapter
            log.warning("SondeHub fetch failed (%s); degrading", exc)
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
        backoff = INITIAL_BACKOFF_S

        rejected = 0
        emitted = 0
        in_aoi = 0
        last_record_at: datetime | None = None
        #: this poll's in-AOI sondes; becomes ``seen`` at the end.
        current: dict[str, Any] = {}

        for serial, frames in data.items():
            if not isinstance(serial, str):
                rejected += 1
                continue
            frame = _latest_frame(frames)
            if frame is None:
                rejected += 1
                continue
            try:
                record = frame_to_record(frame, serial=serial, received_at=now)
            except Exception as exc:  # one malformed frame must not drop the sweep
                log.debug("SondeHub frame skipped (%s)", exc)
                rejected += 1
                continue
            if record is None:
                rejected += 1
                continue

            geom = record.geometry
            if not isinstance(geom, Point):  # we only build Points; narrows for the type checker
                continue
            lon, lat = geom.coordinates[0], geom.coordinates[1]
            if haversine_m(center_lon, center_lat, lon, lat) > radius_m:
                continue  # outside AOI — not an error, just not ours
            in_aoi += 1

            # Dedupe key: frame number when present (monotonic over a flight), else the
            # observed time. An advanced key means new telemetry; an equal key is a re-poll.
            key = record.attributes.get("frame")
            if key is None:
                key = record.observed_at.isoformat()
            current[serial] = key
            if seen.get(serial) == key:
                continue  # already emitted this frame — dedupe

            received += 1
            emitted += 1
            if last_record_at is None or record.observed_at > last_record_at:
                last_record_at = record.observed_at
            yield record

        seen = current  # forget sondes no longer in the feed (bounds memory over a soak)

        yield _status(
            "connected",
            now,
            records_received=received,
            records_rejected=rejected,
            last_record_at=last_record_at,
            attributes={
                "feed_sondes": len(data),
                "in_aoi": in_aoi,
                "emitted_this_poll": emitted,
            },
        )
        await asyncio.sleep(poll_s)


async def run_sondehub(
    cfg: Settings,
    ready: asyncio.Event,
    *,
    provider: SondeHubProvider | None = None,
    poll_s: float | None = None,
) -> None:
    """Pump the SondeHub stream onto the bus until cancelled (PRD §17.1).

    Waits for the subscriber to be live, then publishes :func:`sondehub_records`. A
    broker drop triggers a jittered exponential reconnect; a FRESH records generator
    is built per connection (the PEP 525 lesson — a generator unwound by ``MqttError``
    cannot be resumed). The provider is stateless and reused across reconnects; it is
    injectable for tests, and production resolves it from config.
    """
    await ready.wait()
    prov = provider if provider is not None else build_provider(cfg)
    resolved_poll = poll_s if poll_s is not None else cfg.sondehub_poll_s
    log.info(
        "SondeHub adapter -> %s (AOI %.0f NM @ %.4f,%.4f)",
        prov.name,
        cfg.sondehub_radius_nm,
        cfg.sondehub_center_lat,
        cfg.sondehub_center_lon,
    )
    backoff = INITIAL_BACKOFF_S
    while True:
        try:
            async with connect(cfg, identifier="aether-sondehub") as bus:
                backoff = INITIAL_BACKOFF_S
                async for record in sondehub_records(
                    prov,
                    center_lat=cfg.sondehub_center_lat,
                    center_lon=cfg.sondehub_center_lon,
                    radius_nm=cfg.sondehub_radius_nm,
                    poll_s=resolved_poll,
                ):
                    await bus.publish_record(record)
                return  # generator exhausted (only on cancellation in practice)
        except aiomqtt.MqttError as exc:
            sleep_for, backoff = _backoff(backoff)
            log.warning("SondeHub lost broker (%s); reconnecting in %.1fs", exc, sleep_for)
            await asyncio.sleep(sleep_for)

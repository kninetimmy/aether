"""NASA FIRMS active-fire adapter (PRD §11.11, §18.8, M5.3).

Polls the **NASA FIRMS Area API** (a CSV endpoint, FIRMS-FR-001), normalizes each
detection row to a schema-v2 ``GeoFeatureRecord`` (``feature_type="fire_detection"``)
carrying acquisition time, brightness, fire radiative power (FRP), confidence class,
satellite/instrument, and day/night flag (FIRMS-FR-004), filters to the configured AOI
disk and a minimum confidence, dedupes detections deterministically across overlapping
sensors and repeated queries (FIRMS-FR-006), and pumps the result onto the bus with
reconnect/backoff.

Capability-gated (FIRMS-FR-001): the Area API needs a user-supplied **map key**. With no
key the adapter degrades *visibly* — one ``offline`` source status, then it exits cleanly
(a config error will not self-heal) — it never bakes in a key and never crashes the app
(PRD §2/§37, the same stance as AISStream). The query uses a bounding box around the AOI
(FIRMS-FR-002) and near-real-time VIIRS by default, source-configurable (FIRMS-FR-003).
aether only *fetches*, no faster than ``poll_s`` and well under the FIRMS transaction
limit (FIRMS-FR-007), and never transmits.

**Honest labeling (FIRMS-FR-005):** a FIRMS record is a satellite *thermal-anomaly /
active-fire detection*, **not** a confirmed wildfire. Records carry NASA FIRMS attribution
and a caveat; the confidence class is carried as a detection-quality label, never as a
hazard ``severity`` (a bright pixel is not a graded hazard, so ``severity`` stays ``None``).

Responsibility split mirrors :mod:`aether.adapters.usgs`:

- :class:`FirmsProvider` / :class:`FirmsAreaProvider` — fetch the raw CSV text (the live
  HTTP feed, or the in-process ``fake`` feeder).
- :func:`row_to_record` — pure FIRMS CSV row → ``GeoFeatureRecord`` normalizer.
- :func:`firms_records` — the ``records()`` contract: ``starting``, then a poll loop with
  AOI + confidence filtering, detection-id dedupe, and ``degraded``-on-failure isolation.
- :func:`run_firms` — bus connection, missing-key ``offline`` gate, and jittered
  exponential backoff on broker loss.
"""

import asyncio
import csv
import functools
import io
import logging
import math
import random
import urllib.request
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

import aiomqtt

from aether.alerts.geo import haversine_m
from aether.bus.client import connect
from aether.config import Settings
from aether.schema.common import Confidence
from aether.schema.geometry import Point
from aether.schema.provenance import Provenance
from aether.schema.records import GeoFeatureRecord, Record, SourceStatusRecord

log = logging.getLogger(__name__)

#: Stable source name + retained health-record id (PRD §23 status stream).
SOURCE = "firms"
STATUS_ID = f"source_status:{SOURCE}"

#: NASA FIRMS attribution carried on every record's status + attributes (PRD §11.2).
ATTRIBUTION = "NASA FIRMS (LANCE/EOSDIS)"

#: A FIRMS detection is a thermal anomaly, never a confirmed wildfire (FIRMS-FR-005).
CAVEAT = "Satellite thermal-anomaly detection (NASA FIRMS) — not a confirmed wildfire."

#: 1 nautical mile in metres (AOI radius is configured in NM, distances in metres).
_M_PER_NM = 1852.0
#: One nautical mile is 1/60 of a degree of latitude by definition (bbox math).
_NM_PER_DEG_LAT = 60.0
#: Clamp the latitude used for longitude scaling away from the poles so ``cos`` is finite.
_MAX_ABS_LAT_FOR_SCALING = 89.9

#: Jittered exponential backoff bounds — shared shape with every other adapter (PRD §17.1).
INITIAL_BACKOFF_S = 1.0
MAX_BACKOFF_S = 60.0

#: Provider-name aliases selecting the in-process no-hardware feeder.
_FAKE_PROVIDER_NAMES = frozenset({"fake", "demo"})

#: Hard cap on a single feed response; a runaway body is truncated rather than read unbounded.
MAX_RESPONSE_BYTES = 16 * 1024 * 1024

#: Confidence class ordering for the ``min_confidence`` floor (FIRMS-FR-004 quality).
_CONF_RANK = {"low": 0, "nominal": 1, "high": 2}
#: FIRMS numeric (MODIS) confidence class breaks: low <30, nominal 30..79, high >=80.
_MODIS_NOMINAL_MIN = 30.0
_MODIS_HIGH_MIN = 80.0

#: Map a FIRMS confidence class onto the schema's provenance confidence enum.
_PROV_CONFIDENCE: dict[str, Confidence] = {"high": "high", "nominal": "medium", "low": "low"}


def _now() -> datetime:
    return datetime.now(UTC)


def _backoff(delay: float) -> tuple[float, float]:
    """Full-jitter backoff: sleep a random slice of ``delay``, then double it (capped)."""
    capped = min(delay, MAX_BACKOFF_S)
    return random.uniform(0.0, capped), min(capped * 2.0, MAX_BACKOFF_S)


def _to_float(value: Any) -> float | None:
    """Best-effort float from a CSV cell; blank/non-numeric → ``None``."""
    if value is None:
        return None
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def confidence_class(raw: Any) -> str | None:
    """Normalize a FIRMS confidence cell to ``low``/``nominal``/``high`` (or ``None``).

    VIIRS reports letter codes (``l``/``n``/``h``, or the spelled-out words); MODIS
    reports a 0–100 percentage which FIRMS itself buckets into the same three classes
    (FIRMS-FR-004). Both encodings collapse here so the rest of the adapter is uniform.
    """
    if raw is None:
        return None
    s = str(raw).strip().lower()
    if s in ("h", "high"):
        return "high"
    if s in ("n", "nominal"):
        return "nominal"
    if s in ("l", "low"):
        return "low"
    pct = _to_float(s)
    if pct is None:
        return None
    if pct >= _MODIS_HIGH_MIN:
        return "high"
    if pct >= _MODIS_NOMINAL_MIN:
        return "nominal"
    return "low"


def acq_to_dt(acq_date: str, acq_time: str) -> datetime | None:
    """FIRMS ``acq_date`` (``YYYY-MM-DD``) + ``acq_time`` (UTC ``HHMM``) → aware UTC dt.

    ``acq_time`` is an integer-of-the-day in HHMM with leading zeros stripped upstream
    (e.g. ``133`` is 01:33Z), so it is zero-padded before splitting. Any malformed part
    yields ``None`` and the caller falls back to the receipt time.
    """
    try:
        year, month, day = (int(p) for p in acq_date.strip().split("-"))
        hhmm = acq_time.strip().zfill(4)
        hour, minute = int(hhmm[:2]), int(hhmm[2:])
        return datetime(year, month, day, hour, minute, tzinfo=UTC)
    except (ValueError, AttributeError):
        return None


def aoi_bbox(
    center_lat: float, center_lon: float, radius_nm: float
) -> tuple[float, float, float, float]:
    """Bounding box ``(west, south, east, north)`` circumscribing the AOI disk (FIRMS-FR-002).

    A local flat-earth box that fully contains the radius disk; the finer haversine disk
    filter trims the returned detections to the true circle. Longitude degrees shrink with
    ``cos(lat)`` (clamped off the poles); the box is clamped to valid WGS-84 ranges.
    """
    clamped = max(-_MAX_ABS_LAT_FOR_SCALING, min(_MAX_ABS_LAT_FOR_SCALING, center_lat))
    dlat = radius_nm / _NM_PER_DEG_LAT
    dlon = radius_nm / (_NM_PER_DEG_LAT * math.cos(math.radians(clamped)))
    south = max(-90.0, center_lat - dlat)
    north = min(90.0, center_lat + dlat)
    west = max(-180.0, center_lon - dlon)
    east = min(180.0, center_lon + dlon)
    return west, south, east, north


def _redact(text: str, secret: str) -> str:
    """Strip the map key out of any string before it is logged or put on the bus."""
    s = secret.strip()
    return text.replace(s, "***") if s else text


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


class FirmsProvider(Protocol):
    """A source of FIRMS Area-API CSV payloads."""

    name: str

    async def fetch(self) -> str: ...


def _blocking_get(url: str, timeout_s: float) -> bytes:
    if not url.startswith("https://"):  # public FIRMS API is https; never downgrade
        raise ValueError("refusing non-https FIRMS feed URL")
    req = urllib.request.Request(url, headers={"User-Agent": "aether-firms/1.0"})
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:  # noqa: S310 (https-checked)
        return bytes(resp.read(MAX_RESPONSE_BYTES + 1))


async def _default_fetch(url: str, *, timeout_s: float) -> bytes:
    # Blocking urllib off the event loop (mirrors the USGS/ADS-B providers); no extra dep.
    return await asyncio.to_thread(_blocking_get, url, timeout_s)


class FirmsAreaProvider:
    """Fetch the FIRMS Area API CSV for a bounding box over HTTPS (FIRMS-FR-001/002).

    ``fetch`` is injectable so tests drive canned CSV with no network. The default GETs
    ``{base}/api/area/csv/{key}/{source}/{w,s,e,n}/{day_range}`` and returns the body as
    text. The map key lives only in the URL and is **never logged** — failures are
    redacted before they reach a log line or a status record.
    """

    name = "firms"

    def __init__(
        self,
        api_base: str,
        *,
        map_key: str,
        source: str,
        bbox: tuple[float, float, float, float],
        day_range: int,
        timeout_s: float = 15.0,
        fetch: Callable[[str], Awaitable[bytes]] | None = None,
    ) -> None:
        self._base = api_base.rstrip("/")
        self._key = map_key
        self._source = source
        self._bbox = bbox
        self._day_range = day_range
        self._fetch = (
            fetch if fetch is not None else functools.partial(_default_fetch, timeout_s=timeout_s)
        )

    def _url(self) -> str:
        west, south, east, north = self._bbox
        area = f"{west:.5f},{south:.5f},{east:.5f},{north:.5f}"
        return f"{self._base}/api/area/csv/{self._key}/{self._source}/{area}/{self._day_range}"

    async def fetch(self) -> str:
        try:
            raw = await self._fetch(self._url())
        except Exception as exc:  # redact the key out of any urllib error before it propagates
            raise RuntimeError(_redact(str(exc), self._key)) from None
        return raw.decode("utf-8", errors="replace")


def build_provider(cfg: Settings) -> FirmsProvider:
    """Resolve the configured FIRMS provider (live Area API, or the fake feeder).

    Raises ``ValueError`` when the live feed is selected with no map key — the
    capability gate (FIRMS-FR-001); :func:`run_firms` turns that into one ``offline``
    status. ``fake``/``demo`` as the base *or* the key selects the no-hardware feeder.
    """
    base = cfg.firms_api_base.strip()
    key = cfg.firms_map_key.strip()
    if base.lower() in _FAKE_PROVIDER_NAMES or key.lower() in _FAKE_PROVIDER_NAMES:
        from aether.adapters.firms_fake_feeder import FakeFirmsProvider

        # Place canned detections at the configured AOI center so the demo always renders.
        return FakeFirmsProvider(center_lat=cfg.firms_center_lat, center_lon=cfg.firms_center_lon)
    if not key:
        raise ValueError("FIRMS map key is required (set AETHER_FIRMS_MAP_KEY)")
    bbox = aoi_bbox(cfg.firms_center_lat, cfg.firms_center_lon, cfg.firms_radius_nm)
    return FirmsAreaProvider(
        base,
        map_key=key,
        source=cfg.firms_source,
        bbox=bbox,
        day_range=cfg.firms_day_range,
        timeout_s=cfg.firms_timeout_s,
    )


# --- Normalization (CSV row → GeoFeatureRecord) --------------------------------


def parse_csv(text: str) -> list[dict[str, str]]:
    """Parse FIRMS Area-API CSV into row dicts; raise on a non-CSV body.

    A bad map key returns HTTP 200 with a plain-text message (e.g. ``Invalid MAP_KEY``)
    rather than CSV — that has no ``latitude`` column, so we raise with a short snippet so
    the failure is *visible* in the degraded status (FIRMS-FR-001, fail-visibly §37).
    """
    reader = csv.DictReader(io.StringIO(text))
    fields = reader.fieldnames
    if not fields or "latitude" not in fields:
        snippet = " ".join(text.split())[:120]
        raise ValueError(f"FIRMS response is not detection CSV: {snippet!r}")
    return list(reader)


def row_to_record(row: dict[str, str], *, received_at: datetime) -> GeoFeatureRecord | None:
    """Normalize one FIRMS CSV row to a schema-v2 ``GeoFeatureRecord``.

    Handles both VIIRS (``bright_ti4``/``bright_ti5``, letter confidence) and MODIS
    (``brightness``/``bright_t31``, numeric confidence) column sets. Returns ``None`` for a
    row missing usable coordinates. FIRMS gives no stable detection id, so a deterministic
    one is composed from instrument + satellite + acquisition time + raw lat/lon — stable
    across repeated queries and overlapping sensors for dedupe (FIRMS-FR-006). The acquired
    time is the ``observed_at``; brightness/FRP/confidence are displayable attributes.
    """
    lat_raw = (row.get("latitude") or "").strip()
    lon_raw = (row.get("longitude") or "").strip()
    lat, lon = _to_float(lat_raw), _to_float(lon_raw)
    if lat is None or lon is None:
        return None

    acq_date = (row.get("acq_date") or "").strip()
    acq_time = (row.get("acq_time") or "").strip()
    observed_at = acq_to_dt(acq_date, acq_time) or received_at

    satellite = (row.get("satellite") or "").strip()
    instrument = (row.get("instrument") or "").strip()
    daynight = (row.get("daynight") or "").strip() or None
    conf_raw = (row.get("confidence") or "").strip()
    conf_class = confidence_class(conf_raw)
    frp = _to_float(row.get("frp"))
    # VIIRS uses bright_ti4/bright_ti5; MODIS uses brightness/bright_t31.
    brightness_k = _to_float(row.get("bright_ti4") or row.get("brightness"))
    brightness_secondary_k = _to_float(row.get("bright_ti5") or row.get("bright_t31"))

    # Deterministic detection id (raw lat/lon strings avoid float-format drift).
    det_id = f"{instrument or 'sat'}:{satellite or '?'}:{acq_date}T{acq_time}:{lat_raw}:{lon_raw}"
    rid = f"fire:firms:{det_id}"

    label = f"Fire {frp:.0f} MW" if frp is not None else "Fire detection"

    attributes: dict[str, Any] = {
        "detection_id": det_id,
        "confidence": conf_raw or None,  # raw cell, verbatim
        "confidence_class": conf_class,  # normalized low/nominal/high
        "frp_mw": frp,  # fire radiative power, megawatts
        "brightness_k": brightness_k,
        "brightness_secondary_k": brightness_secondary_k,
        "daynight": daynight,  # "D" / "N"
        "satellite": satellite or None,
        "instrument": instrument or None,
        "scan": _to_float(row.get("scan")),
        "track": _to_float(row.get("track")),
        "acq_date": acq_date or None,
        "acq_time": acq_time or None,
        "attribution": ATTRIBUTION,
        "caveat": CAVEAT,
    }

    return GeoFeatureRecord(
        id=rid,
        source=SOURCE,
        observed_at=observed_at,
        received_at=received_at,
        published_at=received_at,
        correlation_key=rid,  # each detection is its own feature — no cross-feed fusion
        feature_type="fire_detection",
        geometry=Point(coordinates=[lon, lat]),
        valid_from=observed_at,
        # Honest labeling: a thermal detection has no graded hazard severity (FIRMS-FR-005).
        severity=None,
        label=label,
        provenance=[
            Provenance(
                source=SOURCE,
                provider="firms",
                observed_at=observed_at,
                received_at=received_at,
                local_rf=False,
                confidence=_PROV_CONFIDENCE.get(conf_class or "", "unknown"),
            )
        ],
        tags=["fire_detection", "firms"],
        attributes=attributes,
    )


# --- Records stream + bus pump -------------------------------------------------


async def firms_records(
    provider: FirmsProvider,
    *,
    center_lat: float,
    center_lon: float,
    radius_nm: float,
    min_confidence: str = "",
    poll_s: float = 900.0,
) -> AsyncIterator[Record]:
    """Yield the FIRMS record stream: ``starting``, then detections + health each poll.

    Each poll fetches the Area-API CSV once, drops detections outside the AOI disk or below
    ``min_confidence``, and yields one ``GeoFeatureRecord`` per *newly-seen* detection — a
    detection already emitted (same deterministic id) is skipped so a stable hotspot is not
    re-upserted every poll (FIRMS-FR-006). A fetch/parse failure yields ``degraded`` (keeping
    the last good detections on the map) and backs off with jitter — failure isolation
    (PRD §17.4/§37). ``min_confidence`` is "" / "low" / "nominal" / "high".
    """
    yield _status("starting", _now())
    radius_m = radius_nm * _M_PER_NM
    floor = _CONF_RANK.get(min_confidence.strip().lower())
    received = 0
    backoff = INITIAL_BACKOFF_S
    #: detection id → seen, pruned to the in-AOI set each poll so it can't grow unbounded
    #: over a long soak (a detection aging out of the day-range window is forgotten).
    seen: set[str] = set()

    while True:
        now = _now()
        try:
            text = await provider.fetch()
            rows = parse_csv(text)
        except Exception as exc:  # a bad fetch/body must not crash the adapter
            log.warning("FIRMS feed fetch failed (%s); degrading", exc)
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
        current: set[str] = set()

        for row in rows:
            try:
                record = row_to_record(row, received_at=now)
            except Exception as exc:  # one malformed row must not drop the sweep
                log.debug("FIRMS row skipped (%s)", exc)
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

            if floor is not None:
                cls = record.attributes.get("confidence_class")
                if not isinstance(cls, str) or _CONF_RANK.get(cls, -1) < floor:
                    continue  # below the confidence floor

            det_id = record.attributes["detection_id"]
            current.add(det_id)
            if det_id in seen:
                continue  # already emitted this detection — dedupe (FIRMS-FR-006)

            received += 1
            emitted += 1
            if last_record_at is None or record.observed_at > last_record_at:
                last_record_at = record.observed_at
            yield record

        seen = current  # forget detections no longer in the window (bounds memory over a soak)

        yield _status(
            "connected",
            now,
            records_received=received,
            records_rejected=rejected,
            last_record_at=last_record_at,
            attributes={
                "feed_rows": len(rows),
                "in_aoi": in_aoi,
                "emitted_this_poll": emitted,
                "min_confidence": min_confidence or None,
            },
        )
        await asyncio.sleep(poll_s)


async def run_firms(
    cfg: Settings,
    ready: asyncio.Event,
    *,
    provider: FirmsProvider | None = None,
    poll_s: float | None = None,
) -> None:
    """Pump the FIRMS active-fire stream onto the bus until cancelled (PRD §17.1).

    Waits for the subscriber to be live, then publishes :func:`firms_records`. A missing
    map key is reported once as an ``offline`` source status and the task exits cleanly —
    a config error will not self-heal, so we do not spin (FIRMS-FR-001, mirroring AISStream).
    A broker drop triggers a jittered exponential reconnect; a FRESH records generator is
    built per connection (the PEP 525 lesson — a generator unwound by ``MqttError`` cannot
    be resumed). The provider is stateless and reused across reconnects; it is injectable
    for tests, and production resolves it from config.
    """
    await ready.wait()
    resolved_poll = poll_s if poll_s is not None else cfg.firms_poll_s
    backoff = INITIAL_BACKOFF_S
    while True:
        try:
            async with connect(cfg, identifier="aether-firms") as bus:
                backoff = INITIAL_BACKOFF_S  # reset once connected
                try:
                    prov = provider if provider is not None else build_provider(cfg)
                except ValueError as exc:
                    log.error("FIRMS misconfigured: %s", exc)
                    await bus.publish_record(
                        _status(
                            "offline",
                            _now(),
                            error_code="ConfigError",
                            error_summary=str(exc)[:200],
                            attributes={"source": cfg.firms_source},
                        )
                    )
                    return  # config won't self-heal; don't spin
                log.info(
                    "FIRMS adapter -> %s (source %s, AOI %.0f NM, day_range %d)",
                    prov.name,
                    cfg.firms_source,
                    cfg.firms_radius_nm,
                    cfg.firms_day_range,
                )
                async for record in firms_records(
                    prov,
                    center_lat=cfg.firms_center_lat,
                    center_lon=cfg.firms_center_lon,
                    radius_nm=cfg.firms_radius_nm,
                    min_confidence=cfg.firms_min_confidence,
                    poll_s=resolved_poll,
                ):
                    await bus.publish_record(record)
                return  # generator exhausted (only on cancellation in practice)
        except aiomqtt.MqttError as exc:
            sleep_for, backoff = _backoff(backoff)
            log.warning("FIRMS lost broker (%s); reconnecting in %.1fs", exc, sleep_for)
            await asyncio.sleep(sleep_for)

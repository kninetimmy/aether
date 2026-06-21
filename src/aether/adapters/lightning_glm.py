"""NOAA GOES GLM lightning adapter (PRD §11.10, §18.7, M5.6).

Ingests **NOAA GOES Geostationary Lightning Mapper Level-2 (LCFA)** flashes — the open,
benchmark-gated lightning baseline (LIGHTNING-FR-002; the Pi gate is ``docs/glm-benchmark.md``
/ ``scripts/bench_glm.py``, verdict *acceptable*). GLM L2 is a **whole-disk** NetCDF product
published every 20 s on NOAA's GOES Open Data on AWS (PRD §38; anonymous HTTPS, read-only).
There is no server-side area filter, so each file is downloaded and parsed in full, then
flashes outside the configured AOI are discarded **before** anything is published
(LIGHTNING-FR-005). Each flash normalizes to a schema-v2 ``GeoFeatureRecord``
(``feature_type="lightning_flash"``) carrying energy, area, satellite, and quality.

**Honest labeling (LIGHTNING-FR-003/004):** a GLM flash is a *total-lightning* optical
detection, **not** a confirmed cloud-to-ground strike. Records carry that caveat and NOAA
attribution; a flash has no graded hazard ``severity`` (it stays ``None``, the FIRMS stance).

**Provider abstraction (LIGHTNING-FR-001/007):** the live S3 provider is one implementation of
:class:`GlmProvider`; a future credentialed point-strike feed (or the no-hardware fake feeder)
is another, with no schema change. The ``netCDF4`` parser is an **optional dependency**
(``pip install "aether[lightning]"``) and is imported *only inside the live provider* — a
missing parser degrades *visibly* (one ``offline`` status, then a clean exit), never a crash
(the capability-gating stance of FIRMS' map key, PRD §2/§37).

Responsibility split mirrors :mod:`aether.adapters.firms`:

- :class:`GlmS3Provider` / :class:`FakeGlmProvider` — list newest file keys and fetch a parsed
  :class:`GlmFile` (the live one downloads + parses NetCDF; the fake returns canned flashes).
- :func:`flash_to_record` — pure :class:`GlmFlash` → ``GeoFeatureRecord`` normalizer.
- :func:`glm_records` — the ``records()`` contract: ``starting``, then a poll loop that fetches
  only *new* files (bounded backfill), AOI-filters, stamps a TTL, and isolates failures.
- :func:`run_glm` — bus pump, missing-parser/-config ``offline`` gate, jittered backoff.
"""

import asyncio
import logging
import math
import random
import urllib.request
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Protocol

import aiomqtt

from aether.alerts.geo import haversine_m
from aether.bus.client import connect
from aether.config import Settings
from aether.schema.geometry import Point
from aether.schema.provenance import Provenance
from aether.schema.records import GeoFeatureRecord, Record, SourceStatusRecord

log = logging.getLogger(__name__)

#: Stable source name + retained health-record id (PRD §23 status stream).
SOURCE = "glm"
STATUS_ID = f"source_status:{SOURCE}"

#: NOAA attribution carried on every record's status + attributes (PRD §11.2).
ATTRIBUTION = "NOAA GOES-R GLM (NOAA Open Data on AWS)"

#: A GLM flash is total-lightning, never a confirmed ground strike (LIGHTNING-FR-003/004).
CAVEAT = "GOES GLM total-lightning flash detection — not a confirmed cloud-to-ground strike."

#: GLM L2 LCFA product prefix on the public NOAA GOES buckets.
PRODUCT = "GLM-L2-LCFA"
#: GLM L2 files are published every 20 s per satellite (the real-time budget; see benchmark).
FILE_CADENCE_S = 20.0

_M_PER_NM = 1852.0
_NM_PER_DEG_LAT = 60.0
_MAX_ABS_LAT_FOR_SCALING = 89.9

#: Jittered exponential backoff bounds — shared shape with every other adapter (PRD §17.1).
INITIAL_BACKOFF_S = 1.0
MAX_BACKOFF_S = 60.0

#: Provider-name aliases selecting the in-process no-hardware feeder.
_FAKE_PROVIDER_NAMES = frozenset({"fake", "demo"})

#: Hard cap on a single GLM file body; a runaway object is rejected rather than read unbounded.
MAX_FILE_BYTES = 32 * 1024 * 1024


def _now() -> datetime:
    return datetime.now(UTC)


def _backoff(delay: float) -> tuple[float, float]:
    """Full-jitter backoff: sleep a random slice of ``delay``, then double it (capped)."""
    capped = min(delay, MAX_BACKOFF_S)
    return random.uniform(0.0, capped), min(capped * 2.0, MAX_BACKOFF_S)


def sat_bucket(satellite: str) -> str:
    """Map a GOES id (``G19``/``GOES-19``/``19``) to its NOAA Open Data bucket host."""
    num = satellite.strip().upper().removeprefix("GOES-").removeprefix("GOES").removeprefix("G")
    return f"https://noaa-goes{num}.s3.amazonaws.com"


# --- Parsed flash model (provider output; netCDF4 stays inside the live provider) ----------


@dataclass(frozen=True)
class GlmFlash:
    """One normalized GLM flash: position, time, and displayable physics."""

    flash_id: int
    lat: float
    lon: float
    observed_at: datetime
    energy_j: float | None
    area_m2: float | None
    quality_flag: int | None


@dataclass(frozen=True)
class GlmFile:
    """A parsed GLM L2 file: its key, satellite, window start, and flashes."""

    key: str
    satellite: str
    time_coverage_start: datetime
    flashes: list[GlmFlash] = field(default_factory=list)


# --- Provider (file listing + parsed fetch) -----------------------------------------------


class GlmProvider(Protocol):
    """A source of GLM L2 files: list newest keys, fetch one parsed file."""

    @property
    def name(self) -> str: ...

    async def list_keys(self) -> list[str]: ...

    async def fetch(self, key: str) -> GlmFile: ...


def _blocking_get(url: str, timeout_s: float) -> bytes:
    if not url.startswith("https://"):  # public NOAA bucket is https; never downgrade
        raise ValueError("refusing non-https GLM feed URL")
    req = urllib.request.Request(url, headers={"User-Agent": "aether-glm/1.0"})
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:  # noqa: S310 (https-checked)
        data = bytes(resp.read(MAX_FILE_BYTES + 1))
    if len(data) > MAX_FILE_BYTES:
        raise ValueError(f"GLM file exceeds {MAX_FILE_BYTES} bytes; refusing")
    return data


async def _default_get(url: str, *, timeout_s: float) -> bytes:
    return await asyncio.to_thread(_blocking_get, url, timeout_s)


def _parse_iso_z(text: str) -> datetime | None:
    """Parse a GLM ``time_coverage_start`` like ``2026-06-21T20:00:00.0Z`` to aware UTC."""
    try:
        dt = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def start_time_from_key(key: str) -> datetime | None:
    """Recover the window start from a GLM key token ``_s<YYYYDDDHHMMSSt>`` (filename fallback).

    The ``s`` token is year, day-of-year, hour, minute, second, and a tenths digit, e.g.
    ``s20261722000000`` → 2026 DOY 172 20:00:00.0Z. Used only if the in-file global attribute
    cannot be parsed, so a flash always has an authoritative window time.
    """
    marker = "_s"
    i = key.find(marker)
    if i < 0:
        return None
    token = key[i + len(marker) : i + len(marker) + 14]
    if len(token) < 13 or not token[:13].isdigit():
        return None
    try:
        year = int(token[0:4])
        doy = int(token[4:7])
        hour, minute, second = int(token[7:9]), int(token[9:11]), int(token[11:13])
        tenths = int(token[13]) if len(token) >= 14 and token[13].isdigit() else 0
    except ValueError:
        return None
    base = datetime(year, 1, 1, tzinfo=UTC) + timedelta(days=doy - 1)
    return base.replace(hour=hour, minute=minute, second=second, microsecond=tenths * 100_000)


def parse_glm_netcdf(raw: bytes, key: str) -> GlmFile:
    """Parse GLM L2 LCFA NetCDF bytes (in-memory) into a :class:`GlmFile`.

    Imports ``netCDF4`` lazily — it is the optional ``[lightning]`` dependency, and keeping
    the import here is what lets :func:`run_glm` degrade *visibly* when the parser is absent
    rather than failing at module import. ``flash_lat``/``flash_lon`` are plain float32;
    ``flash_energy``/``flash_area`` and the time offsets are scaled int16 that netCDF4
    auto-decodes (verified against the live product). Per-flash time is the file's
    ``time_coverage_start`` plus ``flash_time_offset_of_first_event`` seconds.
    """
    try:
        import netCDF4
        import numpy as np
    except ImportError as exc:  # surfaced by run_glm as an offline status (capability gate)
        raise GlmParserUnavailable(str(exc)) from exc

    ds = netCDF4.Dataset("inmem", memory=raw)
    try:
        satellite = str(getattr(ds, "platform_ID", "")) or "?"
        base = _parse_iso_z(str(getattr(ds, "time_coverage_start", ""))) or start_time_from_key(key)
        if base is None:
            raise ValueError("GLM file has no parseable time_coverage_start")

        variables = ds.variables
        if "flash_lat" not in variables or "flash_lon" not in variables:
            raise ValueError("GLM file missing flash_lat/flash_lon (not an LCFA product?)")

        lat = np.asarray(variables["flash_lat"][:], dtype="float64")
        lon = np.asarray(variables["flash_lon"][:], dtype="float64")
        n = int(lat.shape[0])
        ids = _col_int(variables, "flash_id", n)
        energy = _col_float(variables, "flash_energy", n)
        area = _col_float(variables, "flash_area", n)
        quality = _col_int(variables, "flash_quality_flag", n)
        offsets = _col_float(variables, "flash_time_offset_of_first_event", n)

        flashes: list[GlmFlash] = []
        for i in range(n):
            la, lo = float(lat[i]), float(lon[i])
            if not (math.isfinite(la) and math.isfinite(lo)):
                continue  # a fill/masked position is not a usable flash
            off = offsets[i] if offsets is not None else 0.0
            observed = base + timedelta(seconds=off if math.isfinite(off) else 0.0)
            flashes.append(
                GlmFlash(
                    flash_id=int(ids[i]) if ids is not None else i,
                    lat=la,
                    lon=lo,
                    observed_at=observed,
                    energy_j=_finite_or_none(energy[i]) if energy is not None else None,
                    area_m2=_finite_or_none(area[i]) if area is not None else None,
                    quality_flag=int(quality[i]) if quality is not None else None,
                )
            )
        return GlmFile(key=key, satellite=satellite, time_coverage_start=base, flashes=flashes)
    finally:
        ds.close()


def _finite_or_none(value: float) -> float | None:
    f = float(value)
    return f if math.isfinite(f) else None


def _col_float(variables: Any, name: str, n: int) -> Any | None:
    import numpy as np

    if name not in variables:
        return None
    arr = np.asarray(variables[name][:], dtype="float64")
    return arr if arr.shape[0] == n else None


def _col_int(variables: Any, name: str, n: int) -> Any | None:
    import numpy as np

    if name not in variables:
        return None
    arr = np.asarray(variables[name][:])
    return arr if arr.shape[0] == n else None


class GlmParserUnavailable(RuntimeError):
    """Raised when ``netCDF4`` (the optional ``[lightning]`` dep) is not installed."""


class GlmS3Provider:
    """List + fetch GLM L2 files from a NOAA GOES Open Data bucket over HTTPS.

    ``get`` is injectable so tests drive canned bytes with no network. :meth:`list_keys`
    scrapes the current and previous UTC hour via the S3 list-objects-v2 XML API (two cheap
    calls that straddle the hour boundary so no file is missed); :meth:`fetch` downloads one
    object and parses it in-memory (no disk — LIGHTNING-FR-005).
    """

    def __init__(
        self,
        *,
        satellite: str,
        timeout_s: float = 30.0,
        get: Callable[[str], Awaitable[bytes]] | None = None,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self._sat = satellite
        self._host = sat_bucket(satellite)
        self._get = get if get is not None else lambda u: _default_get(u, timeout_s=timeout_s)
        self._now = now_fn or _now

    @property
    def name(self) -> str:
        return f"glm-s3:{self._sat}"

    def _hour_prefix(self, t: datetime) -> str:
        return f"{PRODUCT}/{t.year}/{t.timetuple().tm_yday:03d}/{t.hour:02d}/"

    async def _list_prefix(self, prefix: str) -> list[str]:
        url = f"{self._host}/?list-type=2&prefix={prefix}&max-keys=400"
        body = (await self._get(url)).decode("utf-8", "replace")
        keys: list[str] = []
        for chunk in body.split("<Contents>")[1:]:
            try:
                keys.append(chunk.split("<Key>")[1].split("</Key>")[0])
            except IndexError:
                continue
        return keys

    async def list_keys(self) -> list[str]:
        now = self._now()
        prev = now - timedelta(hours=1)
        prev_keys = await self._list_prefix(self._hour_prefix(prev))
        cur_keys = await self._list_prefix(self._hour_prefix(now))
        return sorted(prev_keys + cur_keys)  # GLM keys sort chronologically by start time

    async def fetch(self, key: str) -> GlmFile:
        raw = await self._get(f"{self._host}/{key}")
        return parse_glm_netcdf(raw, key)


def build_provider(cfg: Settings) -> GlmProvider:
    """Resolve the configured GLM provider (live S3 bucket, or the fake feeder).

    ``fake``/``demo`` as the satellite *or* the S3 base selects the no-hardware feeder, which
    needs no ``netCDF4`` (it returns canned flashes). The live provider's parser dependency is
    checked lazily on first fetch and surfaced by :func:`run_glm` as an ``offline`` status.
    """
    sat = cfg.glm_satellite.strip()
    base = cfg.glm_s3_base.strip()
    if sat.lower() in _FAKE_PROVIDER_NAMES or base.lower() in _FAKE_PROVIDER_NAMES:
        from aether.adapters.lightning_glm_fake_feeder import FakeGlmProvider

        return FakeGlmProvider(center_lat=cfg.glm_center_lat, center_lon=cfg.glm_center_lon)
    return GlmS3Provider(satellite=sat or "G19", timeout_s=cfg.glm_timeout_s)


# --- Normalization (GlmFlash → GeoFeatureRecord) ------------------------------------------


def flash_to_record(
    flash: GlmFlash, *, satellite: str, received_at: datetime, ttl_s: float
) -> GeoFeatureRecord:
    """Normalize one GLM flash to a schema-v2 ``GeoFeatureRecord``.

    The id follows the PRD §23 correlation form ``lightning:glm:<satellite>:<flash-id>:<start>``;
    the flash's absolute start time makes it globally unique even though ``flash_id`` only
    repeats across files. ``valid_until`` is ``observed_at + ttl_s`` so the transient,
    high-volume flash ages off the map via the live-state expiry sweep (bounded memory).
    Honest labeling: total-lightning flash, no graded ``severity`` (LIGHTNING-FR-003/004).
    """
    rid = f"lightning:glm:{satellite}:{flash.flash_id}:{flash.observed_at.isoformat()}"
    good = flash.quality_flag == 0
    energy_fj = flash.energy_j * 1e15 if flash.energy_j is not None else None
    area_km2 = flash.area_m2 / 1e6 if flash.area_m2 is not None else None
    label = f"Lightning {energy_fj:.0f} fJ" if energy_fj is not None else "Lightning flash"

    return GeoFeatureRecord(
        id=rid,
        source=SOURCE,
        observed_at=flash.observed_at,
        received_at=received_at,
        published_at=received_at,
        correlation_key=rid,  # each flash is its own feature — no cross-feed fusion
        feature_type="lightning_flash",
        geometry=Point(coordinates=[flash.lon, flash.lat]),
        valid_from=flash.observed_at,
        valid_until=flash.observed_at + timedelta(seconds=ttl_s),
        severity=None,  # a flash is a detection, not a graded hazard (LIGHTNING-FR-004)
        label=label,
        provenance=[
            Provenance(
                source=SOURCE,
                provider="glm",
                observed_at=flash.observed_at,
                received_at=received_at,
                local_rf=False,
                confidence="high" if good else "medium",
            )
        ],
        tags=["lightning", "glm", satellite],
        attributes={
            "flash_id": flash.flash_id,
            "satellite": satellite,
            "energy_fj": energy_fj,  # femtojoules, display-friendly (raw is ~1e-13 J)
            "area_km2": area_km2,
            "quality_flag": flash.quality_flag,
            "good_quality": good,
            "attribution": ATTRIBUTION,
            "caveat": CAVEAT,
        },
    )


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


# --- Records stream + bus pump -------------------------------------------------------------


def aoi_keep(center_lat: float, center_lon: float, radius_m: float, flash: GlmFlash) -> bool:
    """True if a flash is within the AOI disk (LIGHTNING-FR-005 pre-publish filter)."""
    return haversine_m(center_lon, center_lat, flash.lon, flash.lat) <= radius_m


async def glm_records(
    provider: GlmProvider,
    *,
    center_lat: float,
    center_lon: float,
    radius_nm: float,
    poll_s: float = 60.0,
    max_files_per_poll: int = 12,
    flash_ttl_s: float = 600.0,
    good_quality_only: bool = False,
) -> AsyncIterator[Record]:
    """Yield the GLM record stream: ``starting``, then flashes + health each poll.

    Each poll lists the newest file keys and fetches only those not already processed (dedup by
    file key — "track file and satellite identity to prevent duplicate ingestion", §18.7),
    capped at the newest ``max_files_per_poll`` so neither a cold start nor a reconnect after an
    outage replays hours of backlog — it catches up to *live* (LIGHTNING-FR-005). Every key seen
    this poll (fetched or dropped) is remembered, so skipped backlog is never retried. Flashes
    outside the AOI disk (or below quality, if required) are dropped *before* publish; each
    survivor is yielded once with a ``flash_ttl_s`` ``valid_until``. A fetch/parse failure yields
    ``degraded`` and backs off with jitter — failure isolation (PRD §17.4/§37).
    """
    yield _status("starting", _now())
    radius_m = radius_nm * _M_PER_NM
    received = 0
    rejected_total = 0
    backoff = INITIAL_BACKOFF_S
    seen: set[str] = set()

    while True:
        now = _now()
        try:
            keys = await provider.list_keys()
        except Exception as exc:  # a listing failure must not crash the adapter
            log.warning("GLM list failed (%s); degrading", exc)
            yield _status(
                "degraded",
                now,
                records_received=received,
                records_rejected=rejected_total,
                error_code=type(exc).__name__,
                error_summary=str(exc)[:200],
            )
            sleep_for, backoff = _backoff(backoff)
            await asyncio.sleep(sleep_for)
            continue

        listed = set(keys)
        new_keys = sorted(k for k in keys if k not in seen)
        dropped_backlog = max(0, len(new_keys) - max_files_per_poll)
        to_fetch = new_keys[-max_files_per_poll:]  # newest cap; older backlog skipped to stay live
        if dropped_backlog:
            log.info("GLM catching up to live: skipping %d backlog files", dropped_backlog)

        emitted = 0
        in_aoi = 0
        flashes_seen = 0
        rejected = 0
        last_record_at: datetime | None = None
        fetch_failed = False

        for key in to_fetch:
            try:
                glm_file = await provider.fetch(key)
            except GlmParserUnavailable:
                raise  # propagate to run_glm → one offline status (capability gate)
            except Exception as exc:  # one bad file must not drop the whole poll
                log.debug("GLM file fetch/parse skipped %s (%s)", key, exc)
                rejected += 1
                fetch_failed = True
                continue

            flashes_seen += len(glm_file.flashes)
            for flash in glm_file.flashes:
                if good_quality_only and flash.quality_flag not in (0, None):
                    continue
                if not aoi_keep(center_lat, center_lon, radius_m, flash):
                    continue  # outside AOI — not an error, just not ours
                in_aoi += 1
                record = flash_to_record(
                    flash,
                    satellite=glm_file.satellite,
                    received_at=now,
                    ttl_s=flash_ttl_s,
                )
                received += 1
                emitted += 1
                if last_record_at is None or record.observed_at > last_record_at:
                    last_record_at = record.observed_at
                yield record

        seen = (seen & listed) | set(new_keys)  # forget keys aged out of the listing window
        rejected_total += rejected
        backoff = INITIAL_BACKOFF_S if not fetch_failed else backoff

        yield _status(
            "degraded" if fetch_failed and emitted == 0 else "connected",
            now,
            records_received=received,
            records_rejected=rejected_total,
            last_record_at=last_record_at,
            attributes={
                "files_fetched": len(to_fetch),
                "flashes_seen": flashes_seen,
                "in_aoi": in_aoi,
                "emitted_this_poll": emitted,
                "backlog_skipped": dropped_backlog,
                "satellite": cfg_satellite(provider),
            },
        )
        await asyncio.sleep(poll_s)


def cfg_satellite(provider: GlmProvider) -> str | None:
    """Best-effort satellite label for the status record (live provider exposes it in its name)."""
    name = getattr(provider, "name", "")
    return name.split(":", 1)[1] if ":" in name else None


async def run_glm(
    cfg: Settings,
    ready: asyncio.Event,
    *,
    provider: GlmProvider | None = None,
    poll_s: float | None = None,
) -> None:
    """Pump the GLM lightning stream onto the bus until cancelled (PRD §17.1).

    Waits for the subscriber, then publishes :func:`glm_records`. A missing ``netCDF4`` parser
    (the optional ``[lightning]`` dep) is reported once as an ``offline`` source status and the
    task exits cleanly — a missing dependency will not self-heal, so we do not spin (the FIRMS
    map-key stance, LIGHTNING-FR-002 capability gate). A broker drop triggers a jittered
    reconnect with a FRESH records generator per connection (the PEP 525 lesson). The provider
    is stateless across reconnects and injectable for tests.
    """
    await ready.wait()
    resolved_poll = poll_s if poll_s is not None else cfg.glm_poll_s
    backoff = INITIAL_BACKOFF_S
    while True:
        try:
            async with connect(cfg, identifier="aether-glm") as bus:
                backoff = INITIAL_BACKOFF_S  # reset once connected
                prov = provider if provider is not None else build_provider(cfg)
                log.info(
                    "GLM adapter -> %s (AOI %.0f NM, poll %.0fs, TTL %.0fs)",
                    prov.name,
                    cfg.glm_radius_nm,
                    resolved_poll,
                    cfg.glm_flash_ttl_s,
                )
                try:
                    async for record in glm_records(
                        prov,
                        center_lat=cfg.glm_center_lat,
                        center_lon=cfg.glm_center_lon,
                        radius_nm=cfg.glm_radius_nm,
                        poll_s=resolved_poll,
                        max_files_per_poll=cfg.glm_max_files_per_poll,
                        flash_ttl_s=cfg.glm_flash_ttl_s,
                        good_quality_only=cfg.glm_good_quality_only,
                    ):
                        await bus.publish_record(record)
                except GlmParserUnavailable as exc:
                    log.error("GLM parser unavailable: %s", exc)
                    await bus.publish_record(
                        _status(
                            "offline",
                            _now(),
                            error_code="ParserUnavailable",
                            error_summary=(
                                'netCDF4 not installed — `pip install "aether[lightning]"`'
                            ),
                            attributes={"detail": str(exc)[:160]},
                        )
                    )
                    return  # dependency won't self-heal; don't spin
                return  # generator exhausted (only on cancellation in practice)
        except aiomqtt.MqttError as exc:
            sleep_for, backoff = _backoff(backoff)
            log.warning("GLM lost broker (%s); reconnecting in %.1fs", exc, sleep_for)
            await asyncio.sleep(sleep_for)

"""CelesTrak orbital-object adapter (PRD §11.14, §18.12, M6.5).

Syncs **General Perturbations (GP) element sets** from CelesTrak's public OMM service and
propagates them with **SGP4** to plot overhead objects on the COP as ``orbital_object``
tracks. This is *tracking* (propagating published elements to a position), never satellite
reception (PRD §5 settled decision).

Two cadences (one sync, one propagate) over one provider:

1. **Sync** (default every 6 h, no faster than CelesTrak's ~2 h refresh — §38 rate limit):
   ``GET <base>/NORAD/elements/gp.php?GROUP=<slug>&FORMAT=json`` per configured group.
   ``FORMAT=json`` is passed **explicitly** — the service default changed to CSV on
   2026-05-09. The JSON is an array of OMM objects; each becomes a cached ``Satrec`` keyed
   by NORAD id, with its element-set epoch retained for an age label. On HTTP 301/403/404 the
   request is **abandoned** (the response will not change; 50 such errors in 2 h firewalls the
   IP) and the last-good cache is served — never a tight retry loop.
2. **Propagate** (default every 15 s): propagate the whole synced set to *now*, keep only
   objects currently above ``min_elevation_deg`` (ORBIT-FR-007, default 10°), and emit one
   ``orbital_object`` ``TrackRecord`` each — positions labelled ``predicted=True`` with the
   element-set epoch age in attributes. A propagation/NaN error skips that object (the orbit
   is never plotted at a bad position — fail-visibly, §37).

Capability-gated on the optional ``sgp4`` parser (the ``[orbital]`` extra), imported lazily
inside :mod:`aether.orbital.sgp4_propagate`: a missing dep degrades to one ``offline`` status
then a clean exit (the GLM/FIRMS stance, PRD §2/§37). The fake feeder ships canned OMM and
drives the **real** propagate path, so the full chain runs with no network and no ``sgp4``.

Every record carries CelesTrak attribution and a "predicted; not for navigation/operational
use" caveat (PRD §11.2/§37). Responsibility split mirrors :mod:`aether.adapters.firms`:

- :class:`CelestrakProvider` / :class:`CelestrakHttpProvider` — fetch raw OMM JSON per group
  (the live HTTPS service, or the in-process ``fake`` feeder).
- :func:`build_satrecs` — pure OMM-list → cached :class:`OrbitalElement` set (bad rows skipped).
- :func:`celestrak_records` — the ``records()`` contract: ``starting``, a sync-then-propagate
  loop with elevation filtering, last-good cache, and ``degraded``-on-failure isolation.
- :func:`run_celestrak` — bus pump + missing-``sgp4`` ``offline`` gate + jittered backoff.
"""

import asyncio
import functools
import json
import logging
import random
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Protocol

import aiomqtt

from aether.bus.client import connect
from aether.config import Settings
from aether.orbital.sgp4_propagate import (
    OmmInitError,
    Sgp4Unavailable,
    build_satrec,
    propagate,
)
from aether.schema.geometry import Point
from aether.schema.provenance import Provenance
from aether.schema.records import Record, SourceStatusRecord, TrackRecord

log = logging.getLogger(__name__)

#: Stable source name + retained health-record id (PRD §23 status stream). Records
#: publish to ``aether/v2/records/celestrak`` (derived from this name).
SOURCE = "celestrak"
STATUS_ID = f"source_status:{SOURCE}"

#: Attribution carried on every record + status so provenance stays honest (§11.2).
ATTRIBUTION = "Orbital data: CelesTrak (celestrak.org)"
#: The non-negotiable honesty caveat for this layer (predicted positions, PRD §37).
CAVEAT = "Predicted SGP4 position; not for navigation or operational use."

#: Default GP service host; ``/NORAD/elements/gp.php`` hangs off it. ``fake``/``demo``
#: selects the no-hardware feeder instead.
DEFAULT_BASE_URL = "https://celestrak.org"

#: Jittered exponential backoff bounds — shared shape with every other adapter (§17.1).
INITIAL_BACKOFF_S = 1.0
MAX_BACKOFF_S = 60.0

#: Provider-name aliases selecting the in-process no-hardware feeder.
_FAKE_PROVIDER_NAMES = frozenset({"fake", "demo"})

#: Hard cap on a single GP response body; a runaway is rejected rather than read unbounded.
MAX_RESPONSE_BYTES = 16 * 1024 * 1024

#: HTTP statuses that will NOT change on retry (gone/forbidden/not-found): abandon the
#: request and serve last-good, never tight-loop (§38 — 50 such errors in 2 h firewalls us).
_NO_RETRY_STATUSES = frozenset({301, 403, 404})


def _now() -> datetime:
    return datetime.now(UTC)


def _backoff(delay: float) -> tuple[float, float]:
    """Full-jitter backoff: sleep a random slice of ``delay``, then double it (capped)."""
    capped = min(delay, MAX_BACKOFF_S)
    return random.uniform(0.0, capped), min(capped * 2.0, MAX_BACKOFF_S)


# --- Parsed element model (provider output) ------------------------------------


@dataclass(frozen=True)
class OrbitalElement:
    """One CelesTrak object: its identity, group, epoch, and a built ``Satrec``."""

    norad_id: int
    object_id: str
    object_name: str
    group: str
    epoch: datetime
    satrec: Any  # sgp4.api.Satrec (untyped optional dep)


def parse_epoch(raw: str) -> datetime | None:
    """OMM ``EPOCH`` ISO-8601 (UTC) → aware UTC datetime; ``None`` if unparseable.

    CelesTrak emits e.g. ``2026-06-21T03:14:15.926784``; a bare (naive) value is read as
    UTC (the OMM epoch is UTC by definition), an offset-bearing value is converted.
    """
    try:
        dt = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


class CelestrakNoRetry(RuntimeError):
    """A GP fetch hit a non-retryable HTTP status (301/403/404): abandon, serve last-good."""


# --- Provider (raw OMM JSON fetch) ---------------------------------------------


class CelestrakProvider(Protocol):
    """A source of CelesTrak GP OMM JSON for a group slug."""

    name: str

    async def fetch_group(self, group: str) -> list[dict[str, Any]]: ...


def _blocking_get(url: str, timeout_s: float) -> bytes:
    if not url.startswith("https://"):  # public CelesTrak service is https; never downgrade
        raise ValueError(f"refusing non-https CelesTrak URL: {url!r}")
    req = urllib.request.Request(url, headers={"User-Agent": "aether-celestrak/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:  # noqa: S310 (https-checked)
            # Read one byte past the cap so an over-limit body is *detected*, not silently
            # truncated and parsed as valid OMM (a truncated body would mis-parse).
            raw = bytes(resp.read(MAX_RESPONSE_BYTES + 1))
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ValueError(f"CelesTrak response exceeded {MAX_RESPONSE_BYTES} bytes for {url!r}")
        return raw
    except urllib.error.HTTPError as exc:
        # 301/403/404 will not change on retry — abandon this request (§38 rate-limit guard).
        if exc.code in _NO_RETRY_STATUSES:
            raise CelestrakNoRetry(f"HTTP {exc.code} for {url!r}; not retrying") from None
        raise


async def _default_fetch(url: str, *, timeout_s: float) -> bytes:
    # Blocking urllib off the event loop (mirrors the FAA/USGS providers); no extra dep.
    return await asyncio.to_thread(_blocking_get, url, timeout_s)


class CelestrakHttpProvider:
    """Fetch CelesTrak GP OMM JSON over HTTPS (PRD §18.12, §38 rate limits).

    ``fetch`` is injectable so tests drive canned JSON with no network. The default GETs
    ``<base>/NORAD/elements/gp.php?GROUP=<slug>&FORMAT=json`` — ``FORMAT=json`` is **always**
    sent explicitly (the service default became CSV on 2026-05-09). ``urllib`` follows the
    301 redirect to the canonical host transparently; a *terminal* 301/403/404 raises
    :class:`CelestrakNoRetry` so the caller abandons the request and serves last-good.
    """

    name = "celestrak"

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        *,
        timeout_s: float = 15.0,
        fetch: Callable[[str], Awaitable[bytes]] | None = None,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._fetch = (
            fetch if fetch is not None else functools.partial(_default_fetch, timeout_s=timeout_s)
        )

    async def fetch_group(self, group: str) -> list[dict[str, Any]]:
        # URL-encode the operator-configurable group so a stray '&'/'='/'#' cannot inject
        # extra query parameters into the GP request. FORMAT=json stays explicit (the
        # service default became CSV 2026-05-09).
        group_q = urllib.parse.quote(group, safe="")
        url = f"{self._base}/NORAD/elements/gp.php?GROUP={group_q}&FORMAT=json"
        raw = await self._fetch(url)
        data = json.loads(raw)
        if not isinstance(data, list):
            raise ValueError("CelesTrak GP response did not return a JSON array")
        return [row for row in data if isinstance(row, dict)]


def build_provider(cfg: Settings) -> CelestrakProvider:
    """Resolve the configured CelesTrak provider (live host, or the fake feeder)."""
    base = cfg.celestrak_base_url.strip()
    if base.lower() in _FAKE_PROVIDER_NAMES:
        from aether.adapters.celestrak_fake_feeder import FakeCelestrakProvider

        # Solve the canned roster relative to the configured observer so the demo always has
        # an above-horizon object regardless of where the operator points the station.
        return FakeCelestrakProvider(
            observer_lat=cfg.celestrak_observer_lat, observer_lon=cfg.celestrak_observer_lon
        )
    return CelestrakHttpProvider(base, timeout_s=cfg.celestrak_timeout_s)


# --- Normalization (OMM list → element cache; element → TrackRecord) -----------


def build_satrecs(
    rows: Sequence[dict[str, Any]], *, group: str
) -> tuple[list[OrbitalElement], int]:
    """Pure OMM-list → ``(elements, skipped)``: build a ``Satrec`` per valid object.

    Each row is run through :func:`aether.orbital.sgp4_propagate.build_satrec`. A row that
    is missing required fields, has an unparseable epoch, or fails OMM ``initialize`` is
    **skipped** (counted), never half-built into a bad orbit (fail-visibly, §37). A missing
    ``sgp4`` dependency propagates as :class:`Sgp4Unavailable` for the capability gate.
    """
    elements: list[OrbitalElement] = []
    skipped = 0
    for row in rows:
        epoch = parse_epoch(str(row.get("EPOCH", "")))
        if epoch is None:
            skipped += 1
            continue
        try:
            satrec = build_satrec(row)
        except OmmInitError as exc:
            log.debug("CelesTrak object skipped (%s)", exc)
            skipped += 1
            continue
        norad_raw = row.get("NORAD_CAT_ID")
        try:
            norad_id = int(str(norad_raw).strip())  # JSON number or numeric string
        except (TypeError, ValueError):
            skipped += 1
            continue
        elements.append(
            OrbitalElement(
                norad_id=norad_id,
                object_id=str(row.get("OBJECT_ID", "")) or "?",
                object_name=str(row.get("OBJECT_NAME", "")) or f"NORAD {norad_id}",
                group=group,
                epoch=epoch,
                satrec=satrec,
            )
        )
    return elements, skipped


def element_to_record(
    element: OrbitalElement,
    *,
    observer_lat: float,
    observer_lon: float,
    observer_alt_m: float,
    at: datetime,
    valid_s: float,
) -> TrackRecord | None:
    """Propagate one element to ``at`` and normalize to an ``orbital_object`` ``TrackRecord``.

    Returns ``None`` when SGP4 cannot produce a finite position (the object is skipped,
    never plotted — fail-visibly, §37). ``predicted=True`` and ``locally_received=False``;
    az/el/range/epoch/age live in ``attributes`` because the schema is ``extra="forbid"`` (no
    new top-level fields, no ``SCHEMA_VERSION`` bump — the maintainer-approved approach).
    """
    state = propagate(
        element.satrec,
        at,
        observer_lat_deg=observer_lat,
        observer_lon_deg=observer_lon,
        observer_alt_m=observer_alt_m,
    )
    if state is None:
        return None

    rid = f"orbital:celestrak:{element.norad_id}"
    age_s = (at - element.epoch).total_seconds()
    return TrackRecord(
        id=rid,
        source=SOURCE,
        observed_at=at,  # propagation time — this is a predicted position AT `at`
        received_at=at,
        published_at=at,
        correlation_key=rid,
        track_type="orbital_object",
        label=element.object_name,
        geometry=Point(coordinates=[state.sub_lon_deg, state.sub_lat_deg]),
        altitude_m=state.altitude_m,
        locally_received=False,  # network-derived element set, propagated locally
        predicted=True,  # honest labeling: this is a propagated, not observed, position
        valid_until=at + timedelta(seconds=valid_s),
        provenance=[
            Provenance(
                source=SOURCE,
                provider="celestrak",
                observed_at=at,
                received_at=at,
                local_rf=False,
                derived=True,  # position derived by propagation, not observed
                confidence="medium",
            )
        ],
        tags=["orbital", "celestrak", element.group],
        attributes={
            "norad_id": element.norad_id,
            "object_id": element.object_id,
            "object_name": element.object_name,
            "group": element.group,
            "element_epoch_utc": element.epoch.isoformat(),
            "element_age_s": age_s,
            "azimuth_deg": state.azimuth_deg,
            "elevation_deg": state.elevation_deg,
            "slant_range_m": state.slant_range_m,
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


# --- Records stream + bus pump -------------------------------------------------


async def celestrak_records(
    provider: CelestrakProvider,
    *,
    groups: Sequence[str],
    observer_lat: float,
    observer_lon: float,
    observer_alt_m: float,
    min_elevation_deg: float = 10.0,
    sync_s: float = 21600.0,
    propagate_s: float = 15.0,
    valid_s: float = 30.0,
    now_fn: Callable[[], datetime] | None = None,
) -> AsyncIterator[Record]:
    """Yield the CelesTrak record stream: ``starting``, sync GP, then propagate each tick.

    The sync fetches each group's OMM JSON no faster than ``sync_s`` (default 6 h, well above
    CelesTrak's 2 h refresh — §38), rebuilding the cached element set; on a non-retryable
    301/403/404 (:class:`CelestrakNoRetry`) or any fetch/parse error the **last-good** cache
    is kept and the source reports ``degraded`` rather than going dark. Between syncs each
    ``propagate_s`` tick propagates the full set to *now* and emits one ``orbital_object``
    ``TrackRecord`` per object currently above ``min_elevation_deg`` (ORBIT-FR-007). A missing
    ``sgp4`` parser propagates as :class:`Sgp4Unavailable` to :func:`run_celestrak` (the
    capability gate). Failure isolation throughout (PRD §17.4/§37).
    """
    now = now_fn or _now
    yield _status("starting", now())
    backoff = INITIAL_BACKOFF_S
    elements: list[OrbitalElement] = []
    received = 0
    rejected_total = 0
    next_sync = 0.0  # force a sync on the first iteration (monotonic clock)

    while True:
        loop_now = asyncio.get_event_loop().time()
        # --- Sync the element set when due (or on first pass) ---
        if loop_now >= next_sync:
            wall = now()
            new_elements: list[OrbitalElement] = []
            sync_skipped = 0
            sync_failed = False
            for group in groups:
                try:
                    rows = await provider.fetch_group(group)
                except Sgp4Unavailable:
                    raise  # propagate to run_celestrak → one offline status (capability gate)
                except CelestrakNoRetry as exc:
                    log.warning("CelesTrak group %s abandoned (%s); keeping last-good", group, exc)
                    sync_failed = True
                    continue
                except Exception as exc:  # a bad fetch/body must not crash the adapter
                    log.warning("CelesTrak group %s fetch failed (%s); degrading", group, exc)
                    sync_failed = True
                    continue
                try:
                    built, skipped = build_satrecs(rows, group=group)
                except Sgp4Unavailable:
                    raise
                new_elements.extend(built)
                sync_skipped += skipped

            if new_elements or not sync_failed:
                # A successful sync (even of an empty group) replaces the cache; a fully
                # failed sync keeps the prior last-good set so the map does not go dark.
                elements = new_elements
            rejected_total += sync_skipped
            next_sync = asyncio.get_event_loop().time() + sync_s
            if sync_failed and not elements:
                yield _status(
                    "degraded",
                    wall,
                    records_received=received,
                    records_rejected=rejected_total,
                    error_code="SyncFailed",
                    error_summary="CelesTrak GP sync failed and no last-good cache",
                )
                sleep_for, backoff = _backoff(backoff)
                await asyncio.sleep(sleep_for)
                continue
            backoff = INITIAL_BACKOFF_S

        # --- Propagate the current set to now and emit above-horizon objects ---
        wall = now()
        emitted = 0
        skipped_prop = 0
        above = 0
        last_record_at: datetime | None = None
        for element in elements:
            record = element_to_record(
                element,
                observer_lat=observer_lat,
                observer_lon=observer_lon,
                observer_alt_m=observer_alt_m,
                at=wall,
                valid_s=valid_s,
            )
            if record is None:
                skipped_prop += 1
                continue
            elevation = record.attributes["elevation_deg"]
            if not isinstance(elevation, (int, float)) or elevation < min_elevation_deg:
                continue  # below the horizon floor — not emitted (ORBIT-FR-007)
            above += 1
            received += 1
            emitted += 1
            last_record_at = wall
            yield record

        yield _status(
            "connected",
            wall,
            records_received=received,
            records_rejected=rejected_total,
            last_record_at=last_record_at,
            attributes={
                "tracked_objects": len(elements),
                "above_horizon": above,
                "emitted_this_tick": emitted,
                "prop_skipped": skipped_prop,
                "min_elevation_deg": min_elevation_deg,
                "groups": list(groups),
            },
        )
        await asyncio.sleep(propagate_s)


async def run_celestrak(
    cfg: Settings,
    ready: asyncio.Event,
    *,
    provider: CelestrakProvider | None = None,
    propagate_s: float | None = None,
) -> None:
    """Pump the CelesTrak orbital stream onto the bus until cancelled (PRD §17.1).

    Waits for the subscriber, then publishes :func:`celestrak_records`. A missing ``sgp4``
    parser (the optional ``[orbital]`` dep) is reported once as an ``offline`` source status
    and the task exits cleanly — a missing dependency will not self-heal, so we do not spin
    (the GLM/FIRMS stance, PRD §2/§37). A broker drop triggers a jittered reconnect with a
    FRESH records generator per connection (the PEP 525 lesson). The provider is stateless
    across reconnects and injectable for tests.
    """
    await ready.wait()
    resolved_prop = propagate_s if propagate_s is not None else cfg.celestrak_propagate_s
    backoff = INITIAL_BACKOFF_S
    while True:
        try:
            async with connect(cfg, identifier="aether-celestrak") as bus:
                backoff = INITIAL_BACKOFF_S  # reset once connected
                prov = provider if provider is not None else build_provider(cfg)
                log.info(
                    "CelesTrak adapter -> %s (groups %s, sync %.0fs, propagate %.0fs, min el %.1f)",
                    prov.name,
                    ",".join(cfg.celestrak_groups),
                    cfg.celestrak_sync_s,
                    resolved_prop,
                    cfg.celestrak_min_elevation_deg,
                )
                try:
                    async for record in celestrak_records(
                        prov,
                        groups=cfg.celestrak_groups,
                        observer_lat=cfg.celestrak_observer_lat,
                        observer_lon=cfg.celestrak_observer_lon,
                        observer_alt_m=cfg.celestrak_observer_alt_m,
                        min_elevation_deg=cfg.celestrak_min_elevation_deg,
                        sync_s=cfg.celestrak_sync_s,
                        propagate_s=resolved_prop,
                        valid_s=cfg.celestrak_valid_s,
                    ):
                        await bus.publish_record(record)
                except Sgp4Unavailable as exc:
                    log.error("CelesTrak SGP4 unavailable: %s", exc)
                    await bus.publish_record(
                        _status(
                            "offline",
                            _now(),
                            error_code="Sgp4Unavailable",
                            error_summary='sgp4 not installed — `pip install "aether[orbital]"`',
                            attributes={"detail": str(exc)[:160]},
                        )
                    )
                    return  # dependency won't self-heal; don't spin
                return  # generator exhausted (only on cancellation in practice)
        except aiomqtt.MqttError as exc:
            sleep_for, backoff = _backoff(backoff)
            log.warning("CelesTrak lost broker (%s); reconnecting in %.1fs", exc, sleep_for)
            await asyncio.sleep(sleep_for)

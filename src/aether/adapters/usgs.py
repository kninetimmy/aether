"""USGS earthquake adapter (PRD §11.12, §17.1, M5.1).

Polls a USGS earthquake **GeoJSON feed** (USGS-FR-001), normalizes each feature to
a schema-v2 ``GeoFeatureRecord`` (``feature_type="earthquake"``) carrying magnitude,
depth, time, review status, significance, felt count, tsunami flag, and PAGER alert
level (USGS-FR-003), filters to the configured AOI and a minimum magnitude, dedupes
by USGS **event id** so an unchanged quake is emitted once and a revised one re-emits
on its ``updated`` bump (USGS-FR-004), and pumps the result onto the bus with
reconnect/backoff.

Read-only public-domain data: USGS earthquake feeds need no key and no terms gate.
aether only *fetches* — at no more than the feed's regeneration cadence (USGS-FR-002,
the default ``poll_s``) — and never transmits. The product is **not authoritative**
for earthquakes; USGS is (PRD §11.2/§37). Records carry USGS attribution and report
magnitude/depth/review-status verbatim — never a derived "confirmed" claim, and an
``automatic`` (un-reviewed) solution stays labeled as such.

Responsibility split mirrors :mod:`aether.adapters.network_adsb`:

- :class:`UsgsProvider` / :class:`UsgsGeoJsonProvider` — fetch the raw GeoJSON
  ``FeatureCollection`` (the live HTTP feed, or the in-process ``fake`` feeder).
- :func:`feature_to_record` — pure USGS ``Feature`` → ``GeoFeatureRecord`` normalizer.
- :func:`usgs_records` — the ``records()`` contract: ``starting``, then a poll loop
  with AOI + magnitude filtering, event-id dedupe, and ``degraded``-on-failure
  isolation (a failed fetch keeps the last good quakes on the map, never crashes).
- :func:`run_usgs` — bus connection + jittered exponential backoff on broker loss.
"""

import asyncio
import functools
import json
import logging
import random
import urllib.request
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
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
SOURCE = "usgs"
STATUS_ID = f"source_status:{SOURCE}"

#: USGS publishes the public attribution as a plain credit line; carried on every
#: record's status + attributes so the UI can show provenance honestly (PRD §11.2).
ATTRIBUTION = "USGS Earthquake Hazards Program"

#: 1 nautical mile in metres (AOI radius is configured in NM, distances in metres).
_M_PER_NM = 1852.0

#: Jittered exponential backoff bounds — shared shape with every other adapter so a
#: downed feed/broker is retried the same way everywhere (PRD §17.1).
INITIAL_BACKOFF_S = 1.0
MAX_BACKOFF_S = 60.0

#: Provider-name aliases selecting the in-process no-hardware feeder.
_FAKE_PROVIDER_NAMES = frozenset({"fake", "demo"})

#: Hard cap on a single feed response (USGS summary feeds are well under this); a
#: runaway body is truncated rather than read unbounded into memory.
MAX_RESPONSE_BYTES = 16 * 1024 * 1024


def _now() -> datetime:
    return datetime.now(UTC)


def _ms_to_dt(ms: Any) -> datetime | None:
    """USGS epoch-milliseconds → aware UTC datetime; ``None``/non-numeric → ``None``."""
    if not isinstance(ms, (int, float)):
        return None
    return datetime.fromtimestamp(ms / 1000.0, tz=UTC)


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


class UsgsProvider(Protocol):
    """A source of USGS GeoJSON ``FeatureCollection`` payloads."""

    name: str

    async def fetch(self) -> dict[str, Any]: ...


def _blocking_get(url: str, timeout_s: float) -> bytes:
    if not url.startswith("https://"):  # public USGS feed is https; never downgrade
        raise ValueError(f"refusing non-https USGS feed URL: {url!r}")
    req = urllib.request.Request(url, headers={"User-Agent": "aether-usgs/1.0"})
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:  # noqa: S310 (https-checked)
        return bytes(resp.read(MAX_RESPONSE_BYTES + 1))


async def _default_fetch(url: str, *, timeout_s: float) -> bytes:
    # Blocking urllib off the event loop (mirrors the ADS-B provider); no extra dep.
    return await asyncio.to_thread(_blocking_get, url, timeout_s)


class UsgsGeoJsonProvider:
    """Fetch a USGS earthquake GeoJSON feed over HTTPS (USGS-FR-001).

    ``fetch`` is injectable so tests drive canned bytes with no network. The default
    GETs the configured feed URL (one of the USGS summary feeds, e.g. ``all_hour`` /
    ``all_day`` / ``2.5_day``) and parses it as a GeoJSON object.
    """

    name = "usgs"

    def __init__(
        self,
        feed_url: str,
        *,
        timeout_s: float = 10.0,
        fetch: Callable[[str], Awaitable[bytes]] | None = None,
    ) -> None:
        self._url = feed_url
        self._fetch = (
            fetch if fetch is not None else functools.partial(_default_fetch, timeout_s=timeout_s)
        )

    async def fetch(self) -> dict[str, Any]:
        raw = await self._fetch(self._url)
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("USGS feed did not return a JSON object")
        return data


def build_provider(cfg: Settings) -> UsgsProvider:
    """Resolve the configured USGS provider (live feed URL, or the fake feeder)."""
    feed = cfg.usgs_feed_url.strip()
    if feed.lower() in _FAKE_PROVIDER_NAMES:
        from aether.adapters.usgs_fake_feeder import FakeUsgsProvider

        # Place canned quakes at the configured AOI center so the demo always renders.
        return FakeUsgsProvider(center_lat=cfg.usgs_center_lat, center_lon=cfg.usgs_center_lon)
    return UsgsGeoJsonProvider(feed, timeout_s=cfg.usgs_timeout_s)


# --- Normalization (Feature → GeoFeatureRecord) --------------------------------


def feature_to_record(feature: dict[str, Any], *, received_at: datetime) -> GeoFeatureRecord | None:
    """Normalize one USGS GeoJSON ``Feature`` to a schema-v2 ``GeoFeatureRecord``.

    Returns ``None`` for a feature that is unusable (no id/geometry/coordinates) or
    not an earthquake (the feed also carries quarry blasts / explosions / ice quakes,
    which would be dishonest to plot on the earthquake layer — PRD §37). The geometry
    is stored 2-D ``[lon, lat]``; depth (the GeoJSON 3rd ordinate, in km) goes into
    attributes so it is *displayed* (USGS-FR-003) without being mistaken for an
    altitude. Magnitude/place/status/sig/felt/tsunami/alert are carried verbatim.
    """
    eqid = feature.get("id")
    props = feature.get("properties")
    geom = feature.get("geometry")
    if not isinstance(eqid, str) or not isinstance(props, dict) or not isinstance(geom, dict):
        return None

    # Honest layering: only true earthquakes belong on the earthquake layer.
    quake_type = props.get("type")
    if quake_type is not None and quake_type != "earthquake":
        return None

    coords = geom.get("coordinates")
    if not isinstance(coords, (list, tuple)) or len(coords) < 2:
        return None
    try:
        lon, lat = float(coords[0]), float(coords[1])
    except (TypeError, ValueError):
        return None
    depth_km: float | None = None
    if len(coords) >= 3 and isinstance(coords[2], (int, float)):
        depth_km = float(coords[2])

    mag = props.get("mag")
    magnitude = float(mag) if isinstance(mag, (int, float)) else None
    observed_at = _ms_to_dt(props.get("time")) or received_at
    updated_at = _ms_to_dt(props.get("updated"))
    tsunami = bool(props.get("tsunami"))
    alert = props.get("alert") if isinstance(props.get("alert"), str) else None
    status = props.get("status") if isinstance(props.get("status"), str) else None
    title = props.get("title") if isinstance(props.get("title"), str) else None

    # Attributes hold every displayable field (USGS-FR-003) plus provenance/caveat.
    attributes: dict[str, Any] = {
        "event_id": eqid,
        "magnitude": magnitude,
        "mag_type": props.get("magType"),
        "depth_km": depth_km,
        "place": props.get("place"),
        "review_status": status,  # "automatic" (un-reviewed) vs "reviewed"
        "significance": props.get("sig"),
        "felt": props.get("felt"),
        "tsunami": tsunami,
        "pager_alert": alert,  # USGS PAGER impact level: green/yellow/orange/red
        "url": props.get("url"),
        "updated_at": updated_at.isoformat() if updated_at else None,
        "attribution": ATTRIBUTION,
        "caveat": "USGS is authoritative for earthquakes; aether displays, not adjudicates.",
    }

    return GeoFeatureRecord(
        id=f"earthquake:usgs:{eqid}",
        source=SOURCE,
        observed_at=observed_at,
        received_at=received_at,
        published_at=received_at,
        # Event id is the dedupe/update key (USGS-FR-004).
        correlation_key=f"earthquake:usgs:{eqid}",
        feature_type="earthquake",
        geometry=Point(coordinates=[lon, lat]),
        valid_from=observed_at,
        # PAGER alert (USGS's own impact level) is the closest honest "severity"; a bare
        # magnitude is not a severity, so it stays in attributes, not here.
        severity=alert,
        label=title or (f"M{magnitude:.1f}" if magnitude is not None else "earthquake"),
        provenance=[
            Provenance(
                source=SOURCE,
                provider="usgs",
                observed_at=observed_at,
                received_at=received_at,
                local_rf=False,
                confidence="high",
            )
        ],
        tags=["earthquake", "usgs"],
        attributes=attributes,
    )


# --- Records stream + bus pump -------------------------------------------------


async def usgs_records(
    provider: UsgsProvider,
    *,
    center_lat: float,
    center_lon: float,
    radius_nm: float,
    min_magnitude: float = 0.0,
    poll_s: float = 60.0,
) -> AsyncIterator[Record]:
    """Yield the USGS record stream: ``starting``, then quakes + health each poll.

    Each poll fetches the feed once, drops features outside the AOI disk or below
    ``min_magnitude``, and yields one ``GeoFeatureRecord`` per *new or revised*
    earthquake — an unchanged quake (same ``updated`` timestamp) is skipped so a
    static event is not re-upserted every minute (USGS-FR-004 dedupe/updates). A
    fetch failure yields ``degraded`` (keeping the last good quakes on the map) and
    backs off with jitter before retrying — failure isolation (PRD §17.4/§37). The
    default ``poll_s`` is ≥ the feed regeneration cadence (USGS-FR-002).
    """
    yield _status("starting", _now())
    radius_m = radius_nm * _M_PER_NM
    received = 0
    backoff = INITIAL_BACKOFF_S
    #: event id → last ``updated`` seen, so a re-poll emits only new/changed quakes.
    #: Pruned to the current in-AOI set every poll so it can't grow unbounded over a
    #: long soak (a quake aging out of the feed is forgotten; if it returns it re-emits).
    seen: dict[str, Any] = {}

    while True:
        now = _now()
        try:
            data = await provider.fetch()
        except Exception as exc:  # a bad fetch must not crash the adapter
            log.warning("USGS feed fetch failed (%s); degrading", exc)
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

        features = data.get("features")
        if not isinstance(features, list):
            features = []
        rejected = 0
        emitted = 0
        in_aoi = 0
        last_record_at: datetime | None = None
        #: this poll's in-AOI, magnitude-passing quakes; becomes ``seen`` at the end.
        current: dict[str, Any] = {}

        for feat in features:
            if not isinstance(feat, dict):
                rejected += 1
                continue
            try:
                record = feature_to_record(feat, received_at=now)
            except Exception as exc:  # one malformed feature must not drop the sweep
                log.debug("USGS feature skipped (%s)", exc)
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

            mag = record.attributes.get("magnitude")
            if min_magnitude > 0.0 and (not isinstance(mag, (int, float)) or mag < min_magnitude):
                continue

            eqid = record.attributes["event_id"]
            updated = record.attributes.get("updated_at")
            current[eqid] = updated
            if seen.get(eqid) == updated:
                continue  # already emitted this revision — dedupe (USGS-FR-004)

            received += 1
            emitted += 1
            if last_record_at is None or record.observed_at > last_record_at:
                last_record_at = record.observed_at
            yield record

        seen = current  # forget quakes no longer in the feed (bounds memory over a soak)

        yield _status(
            "connected",
            now,
            records_received=received,
            records_rejected=rejected,
            last_record_at=last_record_at,
            attributes={
                "feed_features": len(features),
                "in_aoi": in_aoi,
                "emitted_this_poll": emitted,
                "min_magnitude": min_magnitude,
            },
        )
        await asyncio.sleep(poll_s)


async def run_usgs(
    cfg: Settings,
    ready: asyncio.Event,
    *,
    provider: UsgsProvider | None = None,
    poll_s: float | None = None,
) -> None:
    """Pump the USGS earthquake stream onto the bus until cancelled (PRD §17.1).

    Waits for the subscriber to be live, then publishes :func:`usgs_records`. A broker
    drop triggers a jittered exponential reconnect; a FRESH records generator is built
    per connection (the PEP 525 lesson — a generator unwound by ``MqttError`` cannot be
    resumed). The provider is stateless and reused across reconnects; it is injectable
    for tests, and production resolves it from config.
    """
    await ready.wait()
    prov = provider if provider is not None else build_provider(cfg)
    resolved_poll = poll_s if poll_s is not None else cfg.usgs_poll_s
    log.info(
        "USGS adapter -> %s (AOI %.0f NM, min mag %.1f)",
        prov.name,
        cfg.usgs_radius_nm,
        cfg.usgs_min_magnitude,
    )
    backoff = INITIAL_BACKOFF_S
    while True:
        try:
            async with connect(cfg, identifier="aether-usgs") as bus:
                backoff = INITIAL_BACKOFF_S
                async for record in usgs_records(
                    prov,
                    center_lat=cfg.usgs_center_lat,
                    center_lon=cfg.usgs_center_lon,
                    radius_nm=cfg.usgs_radius_nm,
                    min_magnitude=cfg.usgs_min_magnitude,
                    poll_s=resolved_poll,
                ):
                    await bus.publish_record(record)
                return  # generator exhausted (only on cancellation in practice)
        except aiomqtt.MqttError as exc:
            sleep_for, backoff = _backoff(backoff)
            log.warning("USGS lost broker (%s); reconnecting in %.1fs", exc, sleep_for)
            await asyncio.sleep(sleep_for)

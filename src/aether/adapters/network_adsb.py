"""Network ADS-B adapter runner (PRD §18.2, §16.4, §17.1).

The runtime around the pure :mod:`aether.adapters.adsb_provider` (provider + wire
parser) and :mod:`aether.adapters.aoi` (tiling): a poll loop that sweeps the AOI
as a set of provider-compliant tiles, deduplicates the airframes seen in
overlapping tiles, normalizes each to a schema-v2 network ``TrackRecord``, and
pumps them onto the bus with reconnect. Those records carry ``local_rf=False``
provenance under the shared ``aircraft:icao:<hex>`` identity, so the backend's
fusion engine collapses a local+network pair into one track (PRD §11.4) — the M3
exit criterion.

Responsibility split mirrors :mod:`aether.adapters.local_adsb`:

- :func:`build_provider` — resolve the configured :class:`AircraftProvider`
  (``adsb.fi`` live, or the in-process ``fake`` no-hardware feeder).
- :func:`network_adsb_records` — the ``records()`` contract: ``starting``, then a
  forever poll loop. Each sweep tiles the AOI once (the grid is a pure function of
  the AOI), fetches every tile with a polite inter-tile rate limit, dedupes across
  tiles, and yields one track per airframe plus a ``connected`` status. A tile that
  fails (429/5xx/timeout) is logged and skipped, not fatal; a sweep where *every*
  tile fails yields ``degraded`` and backs off — last good tracks stay on the map
  and age out via fusion freshness (PRD §17.4, §37 failure isolation).
- :func:`run_network_adsb` — bus connection + jittered exponential backoff on
  broker loss, building a FRESH records generator per reconnect (the PEP 525 /
  M2.1b lesson) while reusing the stateless provider across reconnects.

Read-only and rate-limited: this only *fetches* a public feed within the
provider's stated limits; it never transmits and never bypasses a rate cap
(PRD §2, §38).
"""

import asyncio
import logging
import random
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime
from typing import Any, Literal

import aiomqtt

from aether.adapters.adsb_provider import (
    SOURCE,
    AdsbFiProvider,
    AircraftObservation,
    AircraftProvider,
    dedupe_observations,
    observation_to_track,
)
from aether.adapters.aoi import GeoRegion, tile_region
from aether.adapters.mil_classify import IcaoRange, parse_ranges
from aether.bus.client import connect
from aether.config import Settings
from aether.schema.records import Record, SourceStatusRecord

log = logging.getLogger(__name__)

#: Jittered exponential backoff bounds for sweep/bus retries (PRD §17.1). Shared
#: shape with the local adapters so every source reconnects the same way.
INITIAL_BACKOFF_S = 1.0
MAX_BACKOFF_S = 30.0

#: Stable id for this source's retained health record (PRD §23 status stream).
STATUS_ID = f"source_status:{SOURCE}"

#: Provider-name aliases accepted from config (case-insensitive). ``fake``/``demo``
#: select the in-process no-hardware feeder; everything else maps to adsb.fi.
_FAKE_PROVIDER_NAMES = frozenset({"fake", "demo"})
_ADSBFI_PROVIDER_NAMES = frozenset({"adsb.fi", "adsbfi", ""})


def _now() -> datetime:
    return datetime.now(UTC)


def _backoff(delay: float) -> tuple[float, float]:
    """Full-jitter backoff: sleep a random slice of ``delay``, then double it.

    Returns ``(sleep_for, next_delay)``. Identical to the local adapters' backoff
    so a downed provider/broker is retried the same way everywhere (PRD §17.1).
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


def build_provider(cfg: Settings) -> AircraftProvider:
    """Resolve the configured network ADS-B provider (PRD §18.2).

    ``adsb.fi`` is the default open provider; ``fake``/``demo`` selects the
    in-process :class:`~aether.adapters.network_adsb_fake_feeder.FakeAircraftProvider`
    for the no-hardware path. An unknown name is a config error, raised loudly
    rather than silently falling back to a live call.
    """
    name = cfg.network_adsb_provider.strip().lower()
    if name in _FAKE_PROVIDER_NAMES:
        # Imported lazily so the test/fake feeder is only pulled in when selected.
        from aether.adapters.network_adsb_fake_feeder import FakeAircraftProvider

        return FakeAircraftProvider()
    if name in _ADSBFI_PROVIDER_NAMES:
        return AdsbFiProvider(timeout_s=cfg.network_adsb_timeout_s)
    raise ValueError(f"unknown network ADS-B provider {cfg.network_adsb_provider!r}")


def aoi_from_settings(cfg: Settings) -> GeoRegion:
    """Build the AOI query disk from config (center + radius, PRD §16.2)."""
    return GeoRegion(
        center_lat=cfg.network_adsb_center_lat,
        center_lon=cfg.network_adsb_center_lon,
        radius_nm=cfg.network_adsb_radius_nm,
    )


async def network_adsb_records(
    provider: AircraftProvider,
    aoi: GeoRegion,
    *,
    poll_s: float = 5.0,
    rate_limit_s: float = 1.0,
    mil_ranges: Sequence[IcaoRange] = (),
) -> AsyncIterator[Record]:
    """Yield the network ADS-B record stream: status, then deduped tracks + health.

    Emits ``starting`` immediately, then sweeps forever. The AOI is tiled once into
    provider-compliant disks (deterministic; the grid never changes for a fixed
    AOI). Each sweep fetches every tile — pausing ``rate_limit_s`` between requests
    for provider politeness (PRD §38) — dedupes the airframes seen across
    overlapping tiles to one per ICAO (NETADSB-FR-005), and yields one network
    ``TrackRecord`` per airframe followed by a status carrying tile/airframe health.

    Failure isolation (PRD §17.4, §37): a single tile that errors is logged and
    skipped (the sweep keeps the tiles that succeeded, and reports ``degraded`` so
    the partial coverage is visible). A sweep in which *every* tile fails yields a
    ``degraded`` status — keeping the last good tracks on the map, to age out via
    fusion freshness — then backs off with jitter before retrying. No per-airframe
    throttle is needed: the poll interval is the update cadence, and one upsert per
    airframe per sweep is already at most one update per ``poll_s``.
    """
    yield _status("starting", _now())
    tiles = tile_region(aoi, max_radius_nm=provider.max_radius_nm)
    received = 0
    backoff = INITIAL_BACKOFF_S
    while True:
        now = _now()
        observations: list[AircraftObservation] = []
        tiles_ok = 0
        last_error: Exception | None = None
        for index, tile in enumerate(tiles):
            if index > 0 and rate_limit_s > 0.0:
                await asyncio.sleep(rate_limit_s)  # politeness between tile requests
            try:
                observations.extend(await provider.fetch_region(tile))
            except Exception as exc:  # one bad tile must not drop the whole sweep
                last_error = exc
                log.warning("network ADS-B tile fetch failed (%s); skipping tile", exc)
                continue
            tiles_ok += 1

        if tiles_ok == 0:  # whole sweep failed: degrade, keep last good tracks, back off
            yield _status(
                "degraded",
                now,
                records_received=received,
                error_code=type(last_error).__name__ if last_error else "SweepFailed",
                error_summary=(str(last_error)[:200] if last_error else "all tiles failed"),
                attributes={"tiles": len(tiles), "tiles_ok": 0},
            )
            sleep_for, backoff = _backoff(backoff)
            await asyncio.sleep(sleep_for)
            continue
        backoff = INITIAL_BACKOFF_S  # reset after any tile succeeded

        deduped = dedupe_observations(observations)
        last_record_at: datetime | None = None
        for obs in deduped:
            track = observation_to_track(obs, provider=provider.name, mil_ranges=mil_ranges)
            received += 1
            if last_record_at is None or obs.observed_at > last_record_at:
                last_record_at = obs.observed_at
            yield track

        tiles_failed = len(tiles) - tiles_ok
        yield _status(
            # Partial coverage (some tiles failed) is honestly reported as degraded.
            "degraded" if tiles_failed else "connected",
            now,
            records_received=received,
            last_record_at=last_record_at,
            error_code=("PartialSweep" if tiles_failed else None),
            error_summary=(f"{tiles_failed}/{len(tiles)} tiles failed" if tiles_failed else None),
            attributes={
                "provider": provider.name,
                "tiles": len(tiles),
                "tiles_ok": tiles_ok,
                "aircraft_visible": len(deduped),
            },
        )
        await asyncio.sleep(poll_s)


async def run_network_adsb(
    cfg: Settings,
    ready: asyncio.Event,
    *,
    provider: AircraftProvider | None = None,
    poll_s: float | None = None,
    rate_limit_s: float | None = None,
) -> None:
    """Pump the network ADS-B stream onto the bus until cancelled (PRD §17.1).

    Waits for the subscriber to be live (avoids a startup race), then publishes the
    :func:`network_adsb_records` stream. A broker drop triggers a jittered
    exponential reconnect rather than crashing the lifespan.

    The provider and AOI are resolved once (the provider is stateless and safe to
    reuse across reconnects), but a FRESH records generator is built per bus
    connection: an ``MqttError`` raised mid-publish unwinds the ``async for`` and
    (PEP 525) closes the generator, which cannot be resumed — reusing it would
    silently end the adapter after the first reconnect (the M2.1b lesson). ``provider``
    is injectable for tests; production resolves it from config.
    """
    await ready.wait()
    prov = provider if provider is not None else build_provider(cfg)
    aoi = aoi_from_settings(cfg)
    resolved_poll = poll_s if poll_s is not None else cfg.network_adsb_poll_s
    resolved_rate = rate_limit_s if rate_limit_s is not None else cfg.network_adsb_rate_limit_s
    mil_ranges = parse_ranges(cfg.mil_icao_blocks)
    log.info("network ADS-B adapter -> %s (AOI %.0f NM)", prov.name, aoi.radius_nm)
    backoff = INITIAL_BACKOFF_S
    while True:
        try:
            async with connect(cfg, identifier="aether-network-adsb") as bus:
                backoff = INITIAL_BACKOFF_S  # reset once connected
                async for record in network_adsb_records(
                    prov,
                    aoi,
                    poll_s=resolved_poll,
                    rate_limit_s=resolved_rate,
                    mil_ranges=mil_ranges,
                ):
                    await bus.publish_record(record)
                return  # generator exhausted (only on cancellation in practice)
        except aiomqtt.MqttError as exc:
            sleep_for, backoff = _backoff(backoff)
            log.warning("network ADS-B lost broker (%s); reconnecting in %.1fs", exc, sleep_for)
            await asyncio.sleep(sleep_for)

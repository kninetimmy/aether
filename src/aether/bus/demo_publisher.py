"""No-hardware demo source: simulated mixed records published onto the bus.

Stands in for the M2/M3 feeds so the COP renders tracks, features, events,
alerts, and source status from simulated data (the M1 exit criterion) — now over
real MQTT topics. As of M3.1 it also exercises the *fusion* engine: a LOCAL leg
(``demo``, ``local_rf=True``) and a NETWORK leg (``demo-net``, ``local_rf=False``)
publish the same aircraft under a shared ``correlation_key``, so the backend
fuses them into one track. :func:`demo_records` is the pure generator
(sink-agnostic, reused by tests); :func:`run_demo_publisher` pumps it onto the
bus. Nothing here transmits or touches hardware.

Four scenario aircraft cover the fusion contract (notional coords near (-95, 40);
nothing transmits):

* ``demo01`` — both legs every tick (local lacks speed/label, network supplies
  them): one fused track, two contributors (FUSION-FR-001/002/003/005).
* ``demo02`` — local for the first few ticks then silent while network continues:
  the fused track keeps moving and flips to network provenance (FUSION-FR-004).
* ``demo03`` — local only (``locally_received=True``), classified military on a
  provider-DB basis (PRD §31.4 "military classification examples"; §11.5).
* ``demo04`` — network only (``locally_received=False``), classified military on an
  ICAO address-block basis — the two §11.5 classification bases side by side.

Run standalone against a broker::

    python -m aether.bus.demo_publisher
"""

import asyncio
import logging
import math
from collections.abc import AsyncIterator
from datetime import UTC, datetime

from aether.bus.client import Bus, connect
from aether.config import Settings
from aether.schema.geometry import Point, Polygon
from aether.schema.provenance import Provenance
from aether.schema.records import (
    AlertRecord,
    Classification,
    EventRecord,
    GeoFeatureRecord,
    Record,
    SourceStatusRecord,
    TrackRecord,
)

log = logging.getLogger(__name__)

#: The demo's local-RF leg (an ADS-B-local freshness window) and its network leg.
_SOURCE = "demo"
_SOURCE_NET = "demo-net"

#: Notional orbit centers for the four scenario aircraft, near a home station.
_AIRCRAFT_CENTERS: dict[str, tuple[float, float]] = {
    "demo01": (-95.2, 40.7),
    "demo02": (-94.8, 40.9),
    "demo03": (-95.4, 40.5),
    "demo04": (-94.6, 41.1),
}

#: After this many ticks demo02's local leg goes silent so the network leg carries
#: the track on its own (the FUSION-FR-004 LOCAL→NET handoff). Note: at fast test
#: ticks the elapsed wall-clock can't exceed the 60s local-expire window, so the
#: *handoff itself* is asserted at the engine-unit level, not over the live demo.
_DEMO02_LOCAL_TICKS = 3


def _now() -> datetime:
    return datetime.now(UTC)


def _orbit(center: tuple[float, float], heading: float, radius: float = 0.1) -> Point:
    angle = math.radians(heading)
    lon = center[0] + radius * math.cos(angle)
    lat = center[1] + radius * math.sin(angle)
    return Point(coordinates=[lon, lat, 3000.0])


def _local_leg(
    key: str,
    now: datetime,
    heading: float,
    *,
    classification: Classification | None = None,
) -> TrackRecord:
    """A LOCAL (own-antenna) observation: position + heading, but NO speed/label.

    Omitting speed and label lets the network leg *fill* those fields while the
    local leg still wins position (FUSION-FR-002/003). ``classification`` is set for
    the military-example aircraft (PRD §31.4).
    """
    return TrackRecord(
        id=f"{_SOURCE}:{key}",
        source=_SOURCE,
        observed_at=now,
        received_at=now,
        published_at=now,
        correlation_key=f"aircraft:icao:{key}",
        track_type="aircraft",
        geometry=_orbit(_AIRCRAFT_CENTERS[key], heading),
        altitude_m=3000.0,
        heading_deg=heading,
        locally_received=True,
        classification=classification,
        tags=["military"] if classification is not None else [],
        provenance=[Provenance(source=_SOURCE, observed_at=now, received_at=now, local_rf=True)],
    )


def _net_leg(
    key: str,
    now: datetime,
    heading: float,
    *,
    classification: Classification | None = None,
) -> TrackRecord:
    """A NETWORK (Internet feed) observation: slightly offset position, WITH speed + label."""
    net_geom = _orbit(_AIRCRAFT_CENTERS[key], heading, radius=0.1005)
    return TrackRecord(
        id=f"{_SOURCE_NET}:{key}",
        source=_SOURCE_NET,
        observed_at=now,
        received_at=now,
        published_at=now,
        correlation_key=f"aircraft:icao:{key}",
        track_type="aircraft",
        label="DEMO-FUSE",
        geometry=net_geom,
        altitude_m=3000.0,
        speed_mps=120.0,
        heading_deg=heading,
        locally_received=False,
        classification=classification,
        tags=["military"] if classification is not None else [],
        provenance=[
            Provenance(source=_SOURCE_NET, observed_at=now, received_at=now, local_rf=False)
        ],
    )


#: Military-classification examples for the demo (PRD §31.4): demo03 reported by a
#: provider DB flag, demo04 inferred from an ICAO address block. Confidence stays
#: below "high" and language hedged — classification is never authoritative (§11.5).
_DEMO03_CLASSIFICATION = Classification(
    military=True, basis="provider", confidence="medium", note="provider database flag"
)
_DEMO04_CLASSIFICATION = Classification(
    military=True, basis="address_block", confidence="low", note="ICAO address block"
)


async def demo_records(*, interval_s: float = 1.0) -> AsyncIterator[Record]:
    """Yield startup status/feature/alert, then move the scenario aircraft each tick."""
    started = _now()
    yield SourceStatusRecord(
        id="source_status:demo",
        source=_SOURCE,
        observed_at=started,
        received_at=started,
        published_at=started,
        status="connected",
    )
    yield SourceStatusRecord(
        id="source_status:demo-net",
        source=_SOURCE_NET,
        observed_at=started,
        received_at=started,
        published_at=started,
        status="connected",
    )
    yield GeoFeatureRecord(
        id="tfr:demo",
        source=_SOURCE,
        observed_at=started,
        received_at=started,
        published_at=started,
        feature_type="tfr",
        label="Demo TFR",
        geometry=Polygon(
            coordinates=[
                [
                    [-95.5, 40.5],
                    [-95.0, 40.5],
                    [-95.0, 41.0],
                    [-95.5, 41.0],
                    [-95.5, 40.5],
                ]
            ]
        ),
    )

    yield AlertRecord(
        id="alert:demo",
        source=_SOURCE,
        observed_at=started,
        received_at=started,
        published_at=started,
        rule_id="demo-proximity",
        subject_id="aircraft:icao:demo01",
        state="open",
        severity="medium",
        title="Demo aircraft in TFR",
        summary="DEMO-FUSE is loitering inside the demo TFR (simulated alert).",
        triggered_at=started,
    )

    tick = 0
    while True:
        await asyncio.sleep(interval_s)
        tick += 1
        now = _now()
        heading = float((tick * 10) % 360)

        # demo01: both legs every tick → one fused track, two contributors.
        yield _local_leg("demo01", now, heading)
        yield _net_leg("demo01", now, heading)

        # demo02: local for the first few ticks, then network-only (handoff).
        if tick <= _DEMO02_LOCAL_TICKS:
            yield _local_leg("demo02", now, heading)
        yield _net_leg("demo02", now, heading)

        # demo03: local only, military (provider basis). demo04: network only,
        # military (address-block basis) — the two §11.5 bases on the map at once.
        yield _local_leg("demo03", now, heading, classification=_DEMO03_CLASSIFICATION)
        yield _net_leg("demo04", now, heading, classification=_DEMO04_CLASSIFICATION)

        if tick % 5 == 0:
            yield EventRecord(
                id=f"event:demo:{tick}",
                source=_SOURCE,
                observed_at=now,
                received_at=now,
                published_at=now,
                event_type="demo_tick",
                summary=f"demo tick {tick}",
            )
        yield SourceStatusRecord(
            id="source_status:demo",
            source=_SOURCE,
            observed_at=now,
            received_at=now,
            published_at=now,
            status="connected",
            records_received=tick,
        )


async def run_demo_publisher(bus: Bus, *, interval_s: float = 1.0) -> None:
    """Publish the demo stream onto the bus until cancelled."""
    async for record in demo_records(interval_s=interval_s):
        await bus.publish_record(record)


async def _main() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = Settings.from_env()
    log.info("demo publisher -> mqtt://%s:%s", settings.mqtt_host, settings.mqtt_port)
    async with connect(settings, identifier="aether-demo-publisher") as bus:
        await run_demo_publisher(bus)


if __name__ == "__main__":
    asyncio.run(_main())

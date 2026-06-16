"""No-hardware demo source: simulated mixed records published onto the bus.

Stands in for the M2/M3 feeds so the COP renders tracks, features, events, and
source status from simulated data (the M1 exit criterion) — now over real MQTT
topics. :func:`demo_records` is the pure generator (sink-agnostic, reused by
tests); :func:`run_demo_publisher` pumps it onto the bus. Nothing here transmits
or touches hardware.

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
    EventRecord,
    GeoFeatureRecord,
    Record,
    SourceStatusRecord,
    TrackRecord,
)

log = logging.getLogger(__name__)

#: Two notional aircraft orbiting points near a home station.
_ORBIT_CENTERS = [(-95.2, 40.7), (-94.8, 40.9)]

_SOURCE = "demo"


def _now() -> datetime:
    return datetime.now(UTC)


async def demo_records(*, interval_s: float = 1.0) -> AsyncIterator[Record]:
    """Yield an initial status + feature, then move tracks on each tick."""
    started = _now()
    yield SourceStatusRecord(
        id="source_status:demo",
        source=_SOURCE,
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

    tick = 0
    while True:
        await asyncio.sleep(interval_s)
        tick += 1
        now = _now()
        for n, (lon0, lat0) in enumerate(_ORBIT_CENTERS):
            heading = (tick * 10 + n * 180) % 360
            angle = math.radians(heading)
            lon = lon0 + 0.1 * math.cos(angle)
            lat = lat0 + 0.1 * math.sin(angle)
            local = n == 0
            yield TrackRecord(
                id=f"aircraft:demo{n}",
                source=_SOURCE,
                observed_at=now,
                received_at=now,
                published_at=now,
                track_type="aircraft",
                label=f"DEMO{n}",
                geometry=Point(coordinates=[lon, lat, 3000.0]),
                altitude_m=3000.0,
                speed_mps=120.0,
                heading_deg=float(heading),
                locally_received=local,
                provenance=[
                    Provenance(
                        source=_SOURCE,
                        observed_at=now,
                        received_at=now,
                        local_rf=local,
                    )
                ],
            )
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
            records_received=tick * len(_ORBIT_CENTERS),
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

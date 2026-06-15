"""In-process demo source: simulated mixed records to exercise the full path.

Not a real adapter — it stands in for the M2/M3 feeds so the COP renders tracks,
features, events, and source status from simulated data (the M1 exit criterion).
The MQTT-backed fake feeders arrive with the bus in M1.2c. It publishes straight
into the hub; nothing here transmits or touches hardware.
"""

import asyncio
import math
from datetime import UTC, datetime

from aether.backend.hub import Hub
from aether.schema.geometry import Point, Polygon
from aether.schema.provenance import Provenance
from aether.schema.records import (
    EventRecord,
    GeoFeatureRecord,
    SourceStatusRecord,
    TrackRecord,
)

# Two notional aircraft orbiting points near a home station.
_ORBIT_CENTERS = [(-95.2, 40.7), (-94.8, 40.9)]


def _now() -> datetime:
    return datetime.now(UTC)


async def run_demo_source(hub: Hub, *, interval_s: float = 1.0) -> None:
    """Publish an initial status + feature, then move tracks on each tick."""
    started = _now()
    hub.publish(
        SourceStatusRecord(
            id="source_status:demo",
            source="demo",
            observed_at=started,
            received_at=started,
            published_at=started,
            status="connected",
        )
    )
    hub.publish(
        GeoFeatureRecord(
            id="tfr:demo",
            source="demo",
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
            hub.publish(
                TrackRecord(
                    id=f"aircraft:demo{n}",
                    source="demo",
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
                            source="demo",
                            observed_at=now,
                            received_at=now,
                            local_rf=local,
                        )
                    ],
                )
            )
        if tick % 5 == 0:
            hub.publish(
                EventRecord(
                    id=f"event:demo:{tick}",
                    source="demo",
                    observed_at=now,
                    received_at=now,
                    published_at=now,
                    event_type="demo_tick",
                    summary=f"demo tick {tick}",
                )
            )
        hub.publish(
            SourceStatusRecord(
                id="source_status:demo",
                source="demo",
                observed_at=now,
                received_at=now,
                published_at=now,
                status="connected",
                records_received=tick * len(_ORBIT_CENTERS),
            )
        )

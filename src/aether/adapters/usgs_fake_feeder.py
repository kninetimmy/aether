"""No-hardware feeder: a fake USGS provider returning canned earthquake GeoJSON.

Stands in for the live USGS earthquake feed so the M5.1 path runs with no network
and no key (PRD §6 no-hardware gate, §34 "every source ships a fake/replay feeder").
It is the USGS sibling of the other fake feeders: real, production-wired code selected
by config (``AETHER_USGS_FEED_URL=fake``), never a live call.

The canned roster is placed *relative to the configured AOI center* (like the network
ADS-B fake feeder fills the queried region) so the demo renders quakes wherever the
operator points the station, and it exercises every branch of the adapter:

- a moderate quake AT the center and one a short hop away → both inside the AOI;
- a quake ~15° away → outside a 500 NM AOI, so the AOI filter drops it;
- a micro quake (M0.8) at the center → dropped when a ``min_magnitude`` is set;
- a quarry blast at the center → dropped by the earthquake-only type guard (it would
  be dishonest to plot it on the earthquake layer).

Frames are stamped at ``fetch`` time (``now_fn`` is injectable for deterministic
tests) so the demo's quakes read as fresh rather than perpetually stale.
"""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000.0)


class FakeUsgsProvider:
    """A controllable :class:`~aether.adapters.usgs.UsgsProvider` with canned quakes."""

    name = "fake"

    def __init__(
        self,
        *,
        center_lat: float = 0.0,
        center_lon: float = 0.0,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self._lat = center_lat
        self._lon = center_lon
        self._now = now_fn or (lambda: datetime.now(UTC))

    def _feature(
        self,
        eqid: str,
        *,
        dlat: float,
        dlon: float,
        mag: float,
        depth_km: float,
        ms: int,
        place: str,
        quake_type: str = "earthquake",
        status: str = "reviewed",
        tsunami: int = 0,
        alert: str | None = None,
        sig: int | None = None,
        felt: int | None = None,
    ) -> dict[str, Any]:
        return {
            "type": "Feature",
            "id": eqid,
            "properties": {
                "mag": mag,
                "magType": "mb",
                "place": place,
                "time": ms,
                "updated": ms,
                "status": status,
                "tsunami": tsunami,
                "sig": sig if sig is not None else int(mag * mag * 50),
                "felt": felt,
                "alert": alert,
                "url": f"https://earthquake.usgs.gov/earthquakes/eventpage/{eqid}",
                "title": f"M {mag:.1f} - {place}",
                "type": quake_type,
            },
            "geometry": {
                "type": "Point",
                # GeoJSON [lon, lat, depth_km].
                "coordinates": [self._lon + dlon, self._lat + dlat, depth_km],
            },
        }

    async def fetch(self) -> dict[str, Any]:
        ms = _ms(self._now())
        return {
            "type": "FeatureCollection",
            "metadata": {"title": "USGS fake feed", "status": 200},
            "features": [
                self._feature(
                    "ak_fake_001",
                    dlat=0.0,
                    dlon=0.0,
                    mag=4.6,
                    depth_km=12.0,
                    ms=ms,
                    place="near AOI center",
                    alert="green",
                    felt=8,
                ),
                self._feature(
                    "ak_fake_002",
                    dlat=0.3,
                    dlon=-0.4,
                    mag=3.2,
                    depth_km=7.5,
                    ms=ms,
                    place="short hop from center",
                    status="automatic",
                ),
                self._feature(
                    "ak_fake_003",
                    dlat=15.0,
                    dlon=15.0,
                    mag=5.8,
                    depth_km=33.0,
                    ms=ms,
                    place="far outside the AOI",
                    alert="yellow",
                ),
                self._feature(
                    "ak_fake_004",
                    dlat=0.0,
                    dlon=0.1,
                    mag=0.8,
                    depth_km=2.0,
                    ms=ms,
                    place="micro quake at center",
                ),
                self._feature(
                    "ak_fake_005",
                    dlat=0.0,
                    dlon=-0.1,
                    mag=2.1,
                    depth_km=0.0,
                    ms=ms,
                    place="quarry at center",
                    quake_type="quarry blast",
                ),
            ],
        }

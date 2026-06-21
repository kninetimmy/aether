"""No-hardware feeder: a fake FIRMS provider returning canned VIIRS Area-API CSV.

Stands in for the live NASA FIRMS feed so the M5.3 path runs with no network and no map
key (PRD §6 no-hardware gate, §34 "every source ships a fake/replay feeder"). It is the
FIRMS sibling of the other fake feeders: real, production-wired code selected by config
(``AETHER_FIRMS_API_BASE=fake`` or ``AETHER_FIRMS_MAP_KEY=fake``), never a live call.

The canned roster is placed *relative to the configured AOI center* (like the USGS fake
feeder) so the demo renders fires wherever the operator points the station, and it
exercises every branch of the adapter:

- a high-confidence detection AT the center and a nominal one a short hop away → both
  inside the AOI;
- a detection ~15° away → outside a 500 NM AOI, so the AOI disk filter drops it;
- a low-confidence detection at the center → dropped when ``min_confidence`` is raised.

The CSV mirrors the live VIIRS Area-API column set (``latitude,longitude,bright_ti4,...``)
with letter confidence codes; ``acq_date``/``acq_time`` are stamped at ``fetch`` time
(``now_fn`` is injectable for deterministic tests) so the demo's fires read as fresh.
"""

from collections.abc import Callable
from datetime import UTC, datetime

#: Live VIIRS Area-API CSV header (FIRMS-FR-004 columns).
_HEADER = (
    "latitude,longitude,bright_ti4,scan,track,acq_date,acq_time,"
    "satellite,instrument,confidence,version,bright_ti5,frp,daynight"
)


class FakeFirmsProvider:
    """A controllable :class:`~aether.adapters.firms.FirmsProvider` with canned detections."""

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

    def _row(
        self,
        *,
        dlat: float,
        dlon: float,
        bright_ti4: float,
        confidence: str,
        frp: float,
        acq_date: str,
        acq_time: str,
        daynight: str = "D",
    ) -> str:
        lat = self._lat + dlat
        lon = self._lon + dlon
        return (
            f"{lat:.5f},{lon:.5f},{bright_ti4:.1f},0.39,0.36,{acq_date},{acq_time},"
            f"N,VIIRS,{confidence},2.0NRT,{bright_ti4 - 70.0:.1f},{frp:.1f},{daynight}"
        )

    async def fetch(self) -> str:
        now = self._now()
        acq_date = now.strftime("%Y-%m-%d")
        acq_time = now.strftime("%H%M")
        rows = [
            _HEADER,
            # high-confidence fire at the AOI center
            self._row(
                dlat=0.0,
                dlon=0.0,
                bright_ti4=345.2,
                confidence="h",
                frp=45.0,
                acq_date=acq_date,
                acq_time=acq_time,
            ),
            # nominal-confidence fire a short hop from center
            self._row(
                dlat=0.3,
                dlon=-0.4,
                bright_ti4=320.7,
                confidence="n",
                frp=12.4,
                acq_date=acq_date,
                acq_time=acq_time,
                daynight="N",
            ),
            # high-confidence fire ~15° away — outside a 500 NM AOI
            self._row(
                dlat=15.0,
                dlon=15.0,
                bright_ti4=350.1,
                confidence="h",
                frp=88.0,
                acq_date=acq_date,
                acq_time=acq_time,
            ),
            # low-confidence detection at center — dropped when a confidence floor is set
            self._row(
                dlat=0.0,
                dlon=0.1,
                bright_ti4=300.3,
                confidence="l",
                frp=2.1,
                acq_date=acq_date,
                acq_time=acq_time,
            ),
        ]
        return "\n".join(rows) + "\n"

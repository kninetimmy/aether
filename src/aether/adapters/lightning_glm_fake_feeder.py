"""No-hardware feeder: a fake GLM provider returning canned lightning flashes.

Stands in for the live NOAA GOES GLM feed so the M5.6 path runs with no network and **no
``netCDF4`` dependency** (PRD §6 no-hardware gate, §34 "every source ships a fake/replay
feeder"). It is the GLM sibling of the other fake feeders: real, production-wired code
selected by config (``AETHER_GLM_SATELLITE=fake`` or ``AETHER_GLM_S3_BASE=fake``), never a
live call. Because the live parser lives only inside :class:`~aether.adapters.lightning_glm.
GlmS3Provider`, this feeder implements the same :class:`~aether.adapters.lightning_glm.
GlmProvider` contract by returning already-parsed :class:`~aether.adapters.lightning_glm.
GlmFile`/``GlmFlash`` objects directly.

Each :meth:`list_keys` call mints **one fresh key for the current 20 s window**, so every poll
sees a new file and emits a new batch (a live-feeling demo). The canned roster is placed
*relative to the configured AOI center* (like the FIRMS/USGS fake feeders) and exercises every
branch of the adapter:

- two good-quality flashes near the center → inside the AOI;
- one good-quality flash ~15° away → outside a 500 NM AOI, so the AOI disk filter drops it;
- one degraded-quality flash at the center → dropped when ``good_quality_only`` is set.
"""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from aether.adapters.lightning_glm import (
    PRODUCT,
    GlmFile,
    GlmFlash,
    start_time_from_key,
)

#: Synthetic satellite label so demo records are visibly not a real platform.
_FAKE_SAT = "GFAKE"


def _window_key(start: datetime) -> str:
    """Build a GLM-style key for the 20 s window containing ``start`` (UTC)."""
    doy = start.timetuple().tm_yday
    s = f"{start.year}{doy:03d}{start.hour:02d}{start.minute:02d}{start.second:02d}0"
    end = start + timedelta(seconds=20)
    edoy = end.timetuple().tm_yday
    e = f"{end.year}{edoy:03d}{end.hour:02d}{end.minute:02d}{end.second:02d}0"
    name = f"OR_{PRODUCT}_{_FAKE_SAT}_s{s}_e{e}_c{s}.nc"
    return f"{PRODUCT}/{start.year}/{doy:03d}/{start.hour:02d}/{name}"


class FakeGlmProvider:
    """A controllable :class:`~aether.adapters.lightning_glm.GlmProvider` with canned flashes."""

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

    @property
    def name(self) -> str:
        return f"glm-fake:{_FAKE_SAT}"

    def _current_window(self) -> datetime:
        """Floor ``now`` to the start of its 20 s GLM window."""
        now = self._now()
        return now.replace(second=(now.second // 20) * 20, microsecond=0)

    async def list_keys(self) -> list[str]:
        return [_window_key(self._current_window())]

    async def fetch(self, key: str) -> GlmFile:
        base = start_time_from_key(key) or self._current_window()
        flashes = [
            # good-quality flash AT the AOI center
            GlmFlash(
                flash_id=1,
                lat=self._lat,
                lon=self._lon,
                observed_at=base + timedelta(seconds=1.2),
                energy_j=4.5e-14,
                area_m2=180_000.0,
                quality_flag=0,
            ),
            # good-quality flash a short hop from center → still in a 500 NM AOI
            GlmFlash(
                flash_id=2,
                lat=self._lat + 0.4,
                lon=self._lon - 0.5,
                observed_at=base + timedelta(seconds=4.8),
                energy_j=1.2e-13,
                area_m2=320_000.0,
                quality_flag=0,
            ),
            # good-quality flash ~15° away → outside a 500 NM AOI (dropped by the disk filter)
            GlmFlash(
                flash_id=3,
                lat=self._lat + 15.0,
                lon=self._lon + 15.0,
                observed_at=base + timedelta(seconds=8.1),
                energy_j=9.0e-14,
                area_m2=500_000.0,
                quality_flag=0,
            ),
            # degraded-quality flash at center → dropped when good_quality_only is set
            GlmFlash(
                flash_id=4,
                lat=self._lat + 0.05,
                lon=self._lon + 0.05,
                observed_at=base + timedelta(seconds=12.5),
                energy_j=2.1e-14,
                area_m2=90_000.0,
                quality_flag=1,
            ),
        ]
        return GlmFile(key=key, satellite=_FAKE_SAT, time_coverage_start=base, flashes=flashes)

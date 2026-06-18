"""No-hardware feeder for the network ADS-B path: an in-process fake provider.

Stands in for a live Internet ADS-B feed so the M3.2 network adapter — and the
local↔network fusion path — runs with no SDR and no live API call (PRD §6
no-hardware gate, §34 "every source ships a fake/replay feeder"). It is the
network sibling of :mod:`aether.adapters.readsb_fake_feeder` and the in-process
demo publisher: real, production-wired code selected by config, never a live call.

:class:`FakeAircraftProvider` implements the ``AircraftProvider`` protocol. It is a
*stub*, not a simulator: it ignores the query region and returns the same small set
of airframes for any tile, anchored at *now* so they stay fresh for fusion (they
never expire mid-demo) and so the runner's cross-tile ``dedupe_observations`` is
exercised when an AOI tiles into several disks.

Two of the airframes deliberately reuse identities the local ADS-B sources emit,
so the M3 exit (local+network duplicates appear once) is demonstrable end to end:

- ``a1b2c3`` matches ``tests/fixtures/readsb/aircraft.json`` (the local-adapter
  integration fixture);
- ``a00000`` matches :mod:`aether.adapters.readsb_fake_feeder` (the local no-hw
  feeder), so a combined live demo shows one fused track;
- ``cafe01`` is network-only — it has no local counterpart, so it stays a plain
  network track and shows the non-fused case alongside.

Run the combined no-hardware fusion demo (broker first), local + network fakes::

    python -m aether.adapters.readsb_fake_feeder /tmp/aircraft.json &
    AETHER_DEMO_SOURCE=0 \
        AETHER_LOCAL_ADSB=1 AETHER_LOCAL_ADSB_SOURCE=/tmp/aircraft.json \
        AETHER_NETWORK_ADSB=1 AETHER_NETWORK_ADSB_PROVIDER=fake \
        uvicorn aether.backend.main:app --app-dir src

This generates data in-process only; it never transmits or touches a radio.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from aether.adapters.adsb_provider import ADSBFI_MAX_RADIUS_NM, AircraftObservation
from aether.adapters.aoi import GeoRegion
from aether.schema.geometry import Point


@dataclass(frozen=True)
class _FakeAircraft:
    """A canned airframe; ``observed_at`` is stamped per fetch so it stays fresh."""

    icao_hex: str
    lon: float
    lat: float
    altitude_m: float
    speed_mps: float
    heading_deg: float
    label: str


#: The fixed roster (see module docstring for why these specific ICAOs).
_ROSTER: tuple[_FakeAircraft, ...] = (
    _FakeAircraft("a1b2c3", -95.2, 40.71, 10668.0, 231.0, 270.0, "UAL123"),
    _FakeAircraft("a00000", -95.2, 40.7, 3048.0, 154.0, 10.0, "FAKE0"),
    _FakeAircraft("cafe01", -94.5, 41.0, 9000.0, 200.0, 90.0, "NETONLY"),
)


class FakeAircraftProvider:
    """In-process fake :class:`AircraftProvider` for the no-hardware path (PRD §34).

    Returns the fixed :data:`_ROSTER` for *any* region, with every airframe's
    ``observed_at``/``received_at`` stamped at call time so fusion treats them as
    currently heard. ``now_fn`` is injectable so a test can pin the clock; it
    defaults to wall-clock UTC.
    """

    name = "fake"
    #: Mirror adsb.fi's cap so a >250 NM AOI tiles (and the runner dedupes) the
    #: same way it would against the real provider.
    max_radius_nm = ADSBFI_MAX_RADIUS_NM

    def __init__(self, *, now_fn: Callable[[], datetime] | None = None) -> None:
        self._now: Callable[[], datetime] = now_fn or (lambda: datetime.now(UTC))

    async def fetch_region(self, region: GeoRegion) -> list[AircraftObservation]:
        now = self._now()
        return [
            AircraftObservation(
                icao_hex=ac.icao_hex,
                observed_at=now,
                received_at=now,
                label=ac.label,
                geometry=Point(coordinates=[ac.lon, ac.lat, ac.altitude_m]),
                altitude_m=ac.altitude_m,
                speed_mps=ac.speed_mps,
                heading_deg=ac.heading_deg,
                attributes={"squawk": "1200", "fake_feeder": True},
            )
            for ac in _ROSTER
        ]

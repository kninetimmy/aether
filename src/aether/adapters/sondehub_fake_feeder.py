"""No-hardware feeder: a fake SondeHub provider returning canned telemetry.

Stands in for the live SondeHub feed so the M5.2 path runs with no network and no
key (PRD §6 no-hardware gate, §34 "every source ships a fake/replay feeder"). It is
the SondeHub sibling of the other fake feeders: real, production-wired code selected
by config (``AETHER_SONDEHUB_API_BASE=fake``), never a live call.

The canned roster is placed *relative to the configured AOI center* (like the USGS
fake feeder fills the queried region) so the demo renders sondes wherever the
operator points the station, and it exercises every branch of the adapter:

- an **ascending** RS41 at the center (``vel_v`` +5) → inside the AOI, ``ascending``;
- a **descending** M10 a short hop away (``vel_v`` -8) → inside the AOI, ``descending``;
- a sonde ~15° away → outside a 500 NM AOI, so the AOI filter drops it;
- a frame with no position → dropped by the normalizer (counted as rejected).

The map mirrors SondeHub's ``{serial: {datetime: frame}}`` shape. Frames are stamped
at ``fetch`` time (``now_fn`` is injectable for deterministic tests) so the demo's
sondes read as fresh — and so a fixed ``now_fn`` gives a stable frame across polls,
exercising the serial+frame dedupe.
"""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any


class FakeSondeHubProvider:
    """A controllable :class:`~aether.adapters.sondehub.SondeHubProvider` with canned sondes."""

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

    def _frame(
        self,
        serial: str,
        *,
        dlat: float,
        dlon: float,
        alt: float,
        vel_v: float,
        iso: str,
        sonde_type: str = "RS41",
        subtype: str = "RS41-SG",
        manufacturer: str = "Vaisala",
        frame: int = 1234,
        vel_h: float = 12.5,
        heading: float = 270.0,
        with_position: bool = True,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "software_name": "aether-fake",
            "software_version": "1.0",
            "uploader_callsign": "FAKE-IGATE",
            "time_received": iso,
            "datetime": iso,
            "manufacturer": manufacturer,
            "type": sonde_type,
            "subtype": subtype,
            "serial": serial,
            "frame": frame,
            "alt": alt,
            "vel_h": vel_h,
            "vel_v": vel_v,
            "heading": heading,
            "temp": -42.5,
            "humidity": 18.0,
            "pressure": 56.0,
            "sats": 9,
            "batt": 2.9,
            "frequency": 404.0,
        }
        if with_position:
            body["lat"] = self._lat + dlat
            body["lon"] = self._lon + dlon
        return body

    async def fetch_telemetry(self) -> dict[str, Any]:
        iso = self._now().strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        return {
            "RS41_FAKE_001": {
                iso: self._frame(
                    "RS41_FAKE_001", dlat=0.0, dlon=0.0, alt=18250.0, vel_v=5.2, iso=iso
                )
            },
            "M10_FAKE_002": {
                iso: self._frame(
                    "M10_FAKE_002",
                    dlat=0.3,
                    dlon=-0.4,
                    alt=9100.0,
                    vel_v=-8.4,
                    iso=iso,
                    sonde_type="M10",
                    subtype="M10",
                    manufacturer="Meteomodem",
                    frame=5678,
                )
            },
            "RS41_FAKE_003": {
                iso: self._frame(
                    "RS41_FAKE_003", dlat=15.0, dlon=15.0, alt=24000.0, vel_v=3.1, iso=iso
                )
            },
            "DFM_FAKE_004": {
                iso: self._frame(
                    "DFM_FAKE_004",
                    dlat=0.0,
                    dlon=0.1,
                    alt=0.0,
                    vel_v=0.0,
                    iso=iso,
                    sonde_type="DFM",
                    subtype="DFM09",
                    manufacturer="Graw",
                    with_position=False,  # no fix → dropped by the normalizer
                )
            },
        }

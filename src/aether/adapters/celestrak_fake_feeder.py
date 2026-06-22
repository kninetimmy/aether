"""No-hardware feeder: a fake CelesTrak provider returning canned OMM JSON.

Stands in for the live CelesTrak GP feed so the M6.5 path runs with no network (PRD §6
no-hardware gate, §34 "every source ships a fake/replay feeder"). It is the CelesTrak
sibling of the other fake feeders: real, production-wired code selected by config
(``AETHER_CELESTRAK_BASE_URL=fake``), never a live call. It still drives the **real**
SGP4 propagate path — :func:`~aether.adapters.celestrak.build_satrecs` builds genuine
``Satrec`` objects from this OMM, and :func:`~aether.adapters.celestrak.element_to_record`
propagates them — so the full chain (adapter → bus → state → ws → UI) exercises the actual
math, only the network is faked. (This means it *does* need the ``[orbital]`` ``sgp4`` extra,
exactly like the live path; the capability gate is tested separately by stubbing the import.)

The roster exercises every branch of the adapter, relative to the configured observer:

- **ISS (NORAD 25544)** — a real LEO element set (a fresh, epoch-stamped clone), so the demo
  carries a recognizable object; whether it is above the horizon depends on wall-clock, which
  is honest.
- **AETHER-GEO-OVERHEAD** — a synthetic geostationary object whose sub-satellite point is
  *solved* to sit at the observer's longitude at fetch time, so it is reliably high above the
  horizon (~75°+) regardless of when the demo runs — the always-visible object the elevation
  filter keeps.
- **AETHER-GEO-FAR** — a synthetic geostationary object ~150° of longitude away, so it is
  below the local horizon and the ``min_elevation_deg`` filter drops it.

A fresh epoch is stamped at every :meth:`fetch_group` call (``now_fn`` injectable for
deterministic tests) so the demo's elements always read as current.
"""

import math
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from aether.orbital.transforms import ecef_to_geodetic, teme_to_ecef

#: Geostationary mean motion (revolutions/sidereal-day). Near-circular, near-equatorial so
#: the sub-satellite point is effectively a fixed longitude that the solve below targets.
_GEO_MEAN_MOTION = "1.00273790"


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")


def _omm(
    *,
    name: str,
    object_id: str,
    norad_id: int,
    epoch: datetime,
    mean_motion: str,
    eccentricity: str,
    inclination: str,
    raan: str,
    arg_pe: str,
    mean_anomaly: str,
    bstar: str = "0.0",
) -> dict[str, Any]:
    """One OMM object dict with the full CelesTrak ``FORMAT=json`` key set."""
    return {
        "OBJECT_NAME": name,
        "OBJECT_ID": object_id,
        "EPOCH": _iso(epoch),
        "MEAN_MOTION": mean_motion,
        "ECCENTRICITY": eccentricity,
        "INCLINATION": inclination,
        "RA_OF_ASC_NODE": raan,
        "ARG_OF_PERICENTER": arg_pe,
        "MEAN_ANOMALY": mean_anomaly,
        "EPHEMERIS_TYPE": "0",
        "CLASSIFICATION_TYPE": "U",
        "NORAD_CAT_ID": norad_id,  # JSON number, like the live feed
        "ELEMENT_SET_NO": "999",
        "REV_AT_EPOCH": "100",
        "BSTAR": bstar,
        "MEAN_MOTION_DOT": "0.0",
        "MEAN_MOTION_DDOT": "0.0",
    }


def _geo_subpoint_lon(raan_deg: float, epoch: datetime) -> float:
    """Sub-satellite longitude of a near-equatorial GEO with the given RAAN, at ``epoch``.

    Uses the same real propagate path the adapter uses (build a Satrec, propagate, TEME→ECEF→
    geodetic), so the solve below is consistent with what the adapter will emit.
    """
    from sgp4 import omm
    from sgp4.api import Satrec, jday

    fields = _omm(
        name="solve",
        object_id="0000-000A",
        norad_id=90000,
        epoch=epoch,
        mean_motion=_GEO_MEAN_MOTION,
        eccentricity="0.0001",
        inclination="0.05",
        raan=f"{raan_deg % 360.0}",
        arg_pe="0.0",
        mean_anomaly="0.0",
    )
    sat = Satrec()
    omm.initialize(sat, fields)
    jd, fr = jday(
        epoch.year,
        epoch.month,
        epoch.day,
        epoch.hour,
        epoch.minute,
        epoch.second + epoch.microsecond * 1e-6,
    )
    _e, r, _v = sat.sgp4(jd, fr)
    ecef = teme_to_ecef((r[0], r[1], r[2]), epoch)
    _lat, lon, _alt = ecef_to_geodetic((ecef[0] * 1000.0, ecef[1] * 1000.0, ecef[2] * 1000.0))
    return lon


def _solve_geo_raan(target_lon: float, epoch: datetime) -> float:
    """Find the RAAN that places a GEO sub-satellite point at ``target_lon`` (deg) at ``epoch``.

    A simple fixed-point iteration on the longitude residual; the GEO subpoint moves ~1:1 with
    RAAN, so this converges in a handful of steps. Deterministic for a fixed ``epoch``.
    """
    raan = 0.0
    for _ in range(40):
        lon = _geo_subpoint_lon(raan, epoch)
        residual = (target_lon - lon + 180.0) % 360.0 - 180.0
        if abs(residual) < 0.02:
            break
        raan = (raan + residual) % 360.0
    return raan % 360.0


class FakeCelestrakProvider:
    """A controllable :class:`~aether.adapters.celestrak.CelestrakProvider` with canned OMM.

    The roster is solved relative to the configured observer so the demo always has at least
    one reliably above-horizon object (the overhead GEO) and one reliably below it (the far
    GEO), regardless of wall-clock.
    """

    name = "celestrak-fake"

    def __init__(
        self,
        *,
        observer_lat: float = 0.0,
        observer_lon: float = 0.0,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self._lat = observer_lat
        self._lon = observer_lon
        self._now = now_fn or (lambda: datetime.now(UTC))

    async def fetch_group(self, group: str) -> list[dict[str, Any]]:
        epoch = self._now()
        overhead_raan = _solve_geo_raan(self._lon, epoch)
        far_raan = _solve_geo_raan((self._lon + 150.0 + 180.0) % 360.0 - 180.0, epoch)
        return [
            # Real ISS element set (epoch refreshed to now so it reads as current). A genuine
            # LEO orbit, propagated by the real SGP4 path; above/below horizon is wall-clock.
            _omm(
                name="ISS (ZARYA)",
                object_id="1998-067A",
                norad_id=25544,
                epoch=epoch,
                mean_motion="15.50103472",
                eccentricity="0.0007123",
                inclination="51.6412",
                raan="247.4627",
                arg_pe="130.5360",
                mean_anomaly="325.0288",
                bstar="0.00016717",
            ),
            # Synthetic GEO solved to sit over the observer's longitude → reliably high above
            # the horizon (the always-visible demo object the elevation filter keeps).
            _omm(
                name="AETHER-GEO-OVERHEAD",
                object_id="2026-900A",
                norad_id=99001,
                epoch=epoch,
                mean_motion=_GEO_MEAN_MOTION,
                eccentricity="0.0001",
                inclination="0.05",
                raan=f"{overhead_raan}",
                arg_pe="0.0",
                mean_anomaly="0.0",
            ),
            # Synthetic GEO ~150 deg of longitude away → below the local horizon, dropped by
            # the min-elevation filter (exercises ORBIT-FR-007).
            _omm(
                name="AETHER-GEO-FAR",
                object_id="2026-901A",
                norad_id=99002,
                epoch=epoch,
                mean_motion=_GEO_MEAN_MOTION,
                eccentricity="0.0001",
                inclination="0.05",
                raan=f"{far_raan}",
                arg_pe="0.0",
                mean_anomaly="0.0",
            ),
        ]


def observer_overhead_elevation(observer_lat: float) -> float:
    """Rough elevation (deg) of a GEO directly over the observer's meridian and the equator.

    A convenience for tests/docs: with the GEO subpoint at the observer longitude, the only
    geometry left is the observer's latitude offset from the equator. Pure trig on a spherical
    Earth — illustrative, not the adapter's authoritative ellipsoidal computation.
    """
    geo_r = 42164.0  # km, geostationary radius
    earth_r = 6378.0  # km
    beta = math.radians(abs(observer_lat))
    # Elevation of a GEO from a sub-meridian observer (standard look-angle simplification).
    return math.degrees(math.atan2(math.cos(beta) - earth_r / geo_r, math.sin(beta)))

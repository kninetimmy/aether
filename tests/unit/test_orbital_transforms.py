"""Unit tests for the orbital coordinate transforms (M6.5, PRD §18.12).

The highest-stakes correctness in the slice: az/el/range and the sub-satellite point come
straight from these. Anchored to PUBLISHED reference values from Vallado, *Fundamentals of
Astrodynamics and Applications* (4th ed.):

- GMST82 vs Example 3-5 (1992-08-20 12:14 UT1 → 152.578787810 deg).
- TEME→ECEF vs Example 3-15 (2004-04-06 07:51:28.386 UTC) — to ~km, since we deliberately
  omit polar motion / nutation (sub-km, far under SGP4's own ~km error).
- geodetic_to_ecef / ecef_to_geodetic round-trip to sub-millimetre.
- look_angles geometry sanity (overhead, horizon, below-horizon) and the azimuth quadrant.
"""

import math
from datetime import UTC, datetime

import pytest

from aether.orbital.transforms import (
    WGS84_A_M,
    ecef_to_geodetic,
    geodetic_to_ecef,
    gmst82,
    julian_date,
    look_angles,
    teme_to_ecef,
)

# --- GMST82 vs a published reference -------------------------------------------


def test_gmst82_matches_vallado_example_3_5() -> None:
    # Vallado Example 3-5: 1992-08-20 12:14:00 UT1 → GMST = 152.578787810 deg.
    dt = datetime(1992, 8, 20, 12, 14, 0, tzinfo=UTC)
    assert math.degrees(gmst82(dt)) == pytest.approx(152.578787810, abs=1e-6)


def test_gmst82_is_wrapped_to_two_pi() -> None:
    g = gmst82(datetime(2026, 6, 21, 18, 30, 0, tzinfo=UTC))
    assert 0.0 <= g < 2.0 * math.pi


def test_julian_date_j2000_epoch() -> None:
    # J2000.0 = 2000-01-01 12:00:00 TT ≈ UTC here → JD 2451545.0.
    assert julian_date(datetime(2000, 1, 1, 12, 0, 0, tzinfo=UTC)) == pytest.approx(2451545.0)


# --- TEME→ECEF vs a published reference (to ~km) -------------------------------


def test_teme_to_ecef_matches_vallado_example_3_15_to_km() -> None:
    # Vallado Example 3-15: 2004-04-06 07:51:28.386009 UTC.
    # TEME r = [5094.18016210, 6127.64465950, 6380.34453270] km.
    # Full ITRF reduction → [-1033.47938300, 7901.29527540, 6380.35659580] km. We omit polar
    # motion + nutation, so we match to ~1 km (well under the SGP4/TLE ~km error, PRD §18.12).
    dt = datetime(2004, 4, 6, 7, 51, 28, 386009, tzinfo=UTC)
    r_teme = (5094.18016210, 6127.64465950, 6380.34453270)
    x, y, z = teme_to_ecef(r_teme, dt)
    assert x == pytest.approx(-1033.479383, abs=1.0)
    assert y == pytest.approx(7901.295275, abs=1.0)
    assert z == pytest.approx(6380.356596, abs=1.0)  # Z is unchanged by the sidereal rotation


def test_teme_to_ecef_preserves_z_and_magnitude() -> None:
    dt = datetime(2026, 6, 21, 0, 0, 0, tzinfo=UTC)
    r = (4000.0, -5000.0, 3000.0)
    x, y, z = teme_to_ecef(r, dt)
    assert z == pytest.approx(3000.0)  # rotation about Z leaves Z fixed
    assert math.sqrt(x * x + y * y + z * z) == pytest.approx(math.sqrt(sum(c * c for c in r)))


# --- Observer geodetic <-> ECEF round trip -------------------------------------


@pytest.mark.parametrize(
    "lat,lon,alt",
    [(0.0, 0.0, 0.0), (40.0, -75.0, 100.0), (-33.9, 151.2, 58.0), (89.5, 10.0, 0.0)],
)
def test_geodetic_ecef_round_trip(lat: float, lon: float, alt: float) -> None:
    back_lat, back_lon, back_alt = ecef_to_geodetic(geodetic_to_ecef(lat, lon, alt))
    assert back_lat == pytest.approx(lat, abs=1e-7)
    assert back_lon == pytest.approx(lon, abs=1e-7)
    assert back_alt == pytest.approx(alt, abs=1e-3)


def test_geodetic_equator_prime_meridian_is_equatorial_radius() -> None:
    x, y, z = geodetic_to_ecef(0.0, 0.0, 0.0)
    assert x == pytest.approx(WGS84_A_M)
    assert y == pytest.approx(0.0, abs=1e-6)
    assert z == pytest.approx(0.0, abs=1e-6)


# --- Look angles: geometry sanity + azimuth quadrant ---------------------------


def test_look_angles_overhead_is_near_90_elevation() -> None:
    # An ECEF point straight up from (0,0): along +X, well above the surface.
    sat_km = (WGS84_A_M / 1000.0 + 400.0, 0.0, 0.0)
    az, el, rng = look_angles(sat_km, 0.0, 0.0, 0.0)
    assert el == pytest.approx(90.0, abs=1e-6)
    assert rng == pytest.approx(400_000.0, abs=1.0)  # 400 km straight up


def test_look_angles_below_horizon_is_negative_elevation() -> None:
    # A point on the opposite side of the Earth is below the local horizon.
    sat_km = (-(WGS84_A_M / 1000.0 + 400.0), 0.0, 0.0)
    _az, el, _rng = look_angles(sat_km, 0.0, 0.0, 0.0)
    assert el < 0.0


def test_look_angles_azimuth_quadrant_north_and_east() -> None:
    # Observer at the equator/prime meridian. A satellite displaced toward +Z (north pole
    # direction) should read an azimuth near North (0/360); displaced toward +Y near East (90).
    surface = WGS84_A_M / 1000.0
    north_sat = (surface + 800.0, 0.0, 600.0)  # tilted toward +Z → northward
    az_n, el_n, _ = look_angles(north_sat, 0.0, 0.0, 0.0)
    assert el_n > 0.0
    assert min(az_n, 360.0 - az_n) < 1.0  # within 1 deg of due North

    east_sat = (surface + 800.0, 600.0, 0.0)  # tilted toward +Y → eastward
    az_e, _el_e, _ = look_angles(east_sat, 0.0, 0.0, 0.0)
    assert az_e == pytest.approx(90.0, abs=1.0)


def test_ecef_to_geodetic_subpoint_altitude_for_leo() -> None:
    # A point 700 km above (0,0) reads back as that subpoint and altitude.
    r_m = (WGS84_A_M + 700_000.0, 0.0, 0.0)
    lat, lon, alt = ecef_to_geodetic(r_m)
    assert lat == pytest.approx(0.0, abs=1e-6)
    assert lon == pytest.approx(0.0, abs=1e-6)
    assert alt == pytest.approx(700_000.0, abs=1.0)

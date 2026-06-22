"""TEME/ECEF/observer coordinate transforms for orbital tracking (M6.5, §18.12).

Pure Python (only :mod:`math`); deterministic and unit-testable against published
reference vectors. The chain that turns an SGP4 state vector into the COP geometry:

1. :func:`gmst82` — Greenwich Mean Sidereal Time from a UTC instant (the 1982 / IAU-82
   GMST polynomial, Vallado *Fundamentals of Astrodynamics and Applications*, Eq. 3-47).
2. :func:`teme_to_ecef` — rotate a TEME position about Z by GMST into Earth-fixed ECEF.
   Polar motion and nutation are neglected: at the ~1 km accuracy of a TLE/SGP4 they are
   far below the noise floor for a hobby COP, and the target is sub-degree az/el.
3. :func:`geodetic_to_ecef` — observer ``(lat, lon, alt_m)`` → ECEF on the WGS-84 oblate
   ellipsoid.
4. :func:`look_angles` — ``ECEF(sat) - ECEF(observer)`` → topocentric SEZ → azimuth /
   elevation / slant range.
5. :func:`ecef_to_geodetic` — sub-satellite point ``(lat, lon, alt_m)`` (Bowring's
   closed-form iteration) for the map ``Point`` and ``altitude_m``.

All angles are degrees at the API boundary; distances are metres; ECEF/TEME positions
are kilometres (the unit SGP4 returns), converted to metres inside :func:`look_angles`.
"""

import math
from datetime import UTC, datetime

#: WGS-84 ellipsoid (PRD §18.12 "WGS-84 oblate ellipsoid").
WGS84_A_M = 6_378_137.0  # semi-major axis (equatorial radius), metres
WGS84_F = 1.0 / 298.257223563  # flattening
WGS84_B_M = WGS84_A_M * (1.0 - WGS84_F)  # semi-minor axis, metres
WGS84_E2 = WGS84_F * (2.0 - WGS84_F)  # first eccentricity squared
#: Second eccentricity squared, used by Bowring's geodetic-latitude iteration.
_EP2 = (WGS84_A_M**2 - WGS84_B_M**2) / WGS84_B_M**2

#: Two-pi, the radian wrap for GMST.
_TWO_PI = 2.0 * math.pi

Vec3 = tuple[float, float, float]


def julian_date(dt: datetime) -> float:
    """UTC :class:`datetime` → Julian Date (days). Naive datetimes are read as UTC.

    Standard civil-calendar → JD conversion (Vallado Eq. 3-42); fractional day carries
    the time of day to sub-second precision (microseconds included).
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    dt = dt.astimezone(UTC)
    year, month = dt.year, dt.month
    if month <= 2:
        year -= 1
        month += 12
    a = year // 100
    b = 2 - a + a // 4
    day_frac = (
        dt.day + (dt.hour + (dt.minute + (dt.second + dt.microsecond * 1e-6) / 60.0) / 60.0) / 24.0
    )
    return (
        math.floor(365.25 * (year + 4716))
        + math.floor(30.6001 * (month + 1))
        + day_frac
        + b
        - 1524.5
    )


def gmst82(dt: datetime) -> float:
    """Greenwich Mean Sidereal Time (radians, ``[0, 2pi)``) for a UTC instant.

    The IAU-1982 GMST polynomial in seconds of the day (Vallado Eq. 3-47): a cubic in
    the Julian centuries ``T`` since J2000 plus the Earth-rotation term. We approximate
    UT1 by UTC — the |UT1-UTC| < 0.9 s difference moves az/el by far less than the
    sub-degree target — and never need a leap-second table for a hobby COP.
    """
    jd = julian_date(dt)
    t = (jd - 2451545.0) / 36525.0  # Julian centuries of UT1 since J2000.0
    # GMST in seconds (Vallado Eq. 3-47, the "1982" form).
    gmst_sec = (
        67310.54841
        + (876600.0 * 3600.0 + 8640184.812866) * t
        + 0.093104 * t * t
        - 6.2e-6 * t * t * t
    )
    gmst_rad = math.radians(gmst_sec / 240.0)  # 240 s of time = 1 degree (86400 s / 360)
    return gmst_rad % _TWO_PI


def teme_to_ecef(r_teme_km: Vec3, dt: datetime) -> Vec3:
    """Rotate a TEME position (km) into Earth-fixed ECEF (km) about Z by GMST.

    The dominant TEME→ECEF transform is the sidereal Z-rotation; polar motion and the
    equation-of-equinoxes nutation correction are neglected (sub-km, far under SGP4's own
    ~km error — PRD §18.12). ECEF then shares the WGS-84 frame the observer is expressed
    in, so a simple vector difference gives the line of sight.
    """
    theta = gmst82(dt)
    cos_t, sin_t = math.cos(theta), math.sin(theta)
    x, y, z = r_teme_km
    # R3(theta): rotate the inertial vector into the rotating Earth-fixed frame.
    return (cos_t * x + sin_t * y, -sin_t * x + cos_t * y, z)


def geodetic_to_ecef(lat_deg: float, lon_deg: float, alt_m: float) -> Vec3:
    """Observer geodetic ``(lat, lon, alt_m)`` → ECEF metres on the WGS-84 ellipsoid.

    Standard closed form (Vallado §3.2): ``N`` is the prime-vertical radius of curvature
    at the geodetic latitude; altitude is height above the ellipsoid in metres.
    """
    lat = math.radians(lat_deg)
    lon = math.radians(lon_deg)
    sin_lat, cos_lat = math.sin(lat), math.cos(lat)
    n = WGS84_A_M / math.sqrt(1.0 - WGS84_E2 * sin_lat * sin_lat)
    x = (n + alt_m) * cos_lat * math.cos(lon)
    y = (n + alt_m) * cos_lat * math.sin(lon)
    z = (n * (1.0 - WGS84_E2) + alt_m) * sin_lat
    return (x, y, z)


def ecef_to_geodetic(r_ecef_m: Vec3) -> tuple[float, float, float]:
    """ECEF metres → geodetic ``(lat_deg, lon_deg, alt_m)`` (Bowring's closed form).

    Bowring's 1976 single-pass approximation, accurate to well under a metre for any
    near-Earth-to-geostationary altitude — ample for a sub-satellite map point. Longitude
    is the exact ``atan2(y, x)``; the pole is handled by falling back to a polar latitude.
    """
    x, y, z = r_ecef_m
    lon = math.atan2(y, x)
    p = math.hypot(x, y)
    if p < 1e-9:  # on the spin axis — a pole; latitude is +/-90, longitude undefined (0)
        lat = math.copysign(math.pi / 2.0, z)
        alt = abs(z) - WGS84_B_M
        return (math.degrees(lat), 0.0, alt)
    # Bowring's auxiliary (parametric) latitude theta, then the geodetic latitude.
    theta = math.atan2(z * WGS84_A_M, p * WGS84_B_M)
    sin_t, cos_t = math.sin(theta), math.cos(theta)
    lat = math.atan2(
        z + _EP2 * WGS84_B_M * sin_t**3,
        p - WGS84_E2 * WGS84_A_M * cos_t**3,
    )
    sin_lat = math.sin(lat)
    n = WGS84_A_M / math.sqrt(1.0 - WGS84_E2 * sin_lat * sin_lat)
    alt = p / math.cos(lat) - n
    return (math.degrees(lat), math.degrees(lon), alt)


def look_angles(
    sat_ecef_km: Vec3, observer_lat_deg: float, observer_lon_deg: float, observer_alt_m: float
) -> tuple[float, float, float]:
    """Topocentric look angles from observer to satellite: ``(az_deg, el_deg, range_m)``.

    Builds the line-of-sight vector in ECEF (metres), rotates it into the observer's
    topocentric **SEZ** frame (South, East, Zenith), and reads:

    - **azimuth** from the S/E components, measured clockwise from **North** in ``[0, 360)``;
    - **elevation** from the zenith component (negative ⇒ below the horizon);
    - **slant range** as the line-of-sight magnitude.

    SEZ unit axes follow Vallado §4.4: ``s_hat`` points South, ``e_hat`` East,
    ``z_hat`` toward the local zenith.
    """
    obs = geodetic_to_ecef(observer_lat_deg, observer_lon_deg, observer_alt_m)
    sx, sy, sz = (c * 1000.0 for c in sat_ecef_km)  # km -> m
    rx, ry, rz = sx - obs[0], sy - obs[1], sz - obs[2]  # line-of-sight, ECEF metres

    lat = math.radians(observer_lat_deg)
    lon = math.radians(observer_lon_deg)
    sin_lat, cos_lat = math.sin(lat), math.cos(lat)
    sin_lon, cos_lon = math.sin(lon), math.cos(lon)

    # ECEF -> SEZ (South, East, Zenith) rotation (Vallado Eq. 4-15).
    south = sin_lat * cos_lon * rx + sin_lat * sin_lon * ry - cos_lat * rz
    east = -sin_lon * rx + cos_lon * ry
    zenith = cos_lat * cos_lon * rx + cos_lat * sin_lon * ry + sin_lat * rz

    rng = math.sqrt(rx * rx + ry * ry + rz * rz)
    elevation = math.degrees(math.asin(max(-1.0, min(1.0, zenith / rng)))) if rng > 0 else 0.0
    # Azimuth clockwise from North: north = -south, so atan2(east, north).
    azimuth = math.degrees(math.atan2(east, -south)) % 360.0
    return (azimuth, elevation, rng)

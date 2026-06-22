"""SGP4 propagation of an OMM element set to observer geometry (M6.5, §18.12).

Thin, fail-visibly wrapper over the optional ``sgp4`` library (the ``[orbital]`` extra,
PRD §11.14). It is the orbital sibling of the GLM NetCDF parser: ``sgp4`` is imported
**lazily** inside :func:`build_satrec`, so a missing dependency surfaces as a single
``offline`` status in :func:`aether.adapters.celestrak.run_celestrak` rather than a crash
at import (the capability-gating stance, PRD §2/§37).

The flow per object:

- :func:`build_satrec` — OMM JSON dict → ``sgp4.api.Satrec`` via ``sgp4.omm.initialize``
  (OMM support is flagged *experimental* upstream, so required fields are validated and any
  initialize failure raises :class:`OmmInitError` — we never propagate a half-built orbit).
- :func:`propagate` — propagate to a UTC instant, returning a :class:`SatState` (TEME km +
  the derived az/el/range and sub-satellite point), or ``None`` on any SGP4 error / NaN
  (the object is *skipped*, never plotted at a bad position — fail-visibly).

The scalar ``Satrec.sgp4`` path needs no numpy, so this module imports none.
"""

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from aether.orbital.transforms import ecef_to_geodetic, look_angles, teme_to_ecef

#: Minimal OMM keys ``sgp4.omm.initialize`` needs to build a Satrec. CelesTrak's
#: ``FORMAT=json`` always includes these; a payload missing any of them is rejected as
#: malformed rather than silently producing a degenerate orbit (PRD §37 fail-visibly).
REQUIRED_OMM_KEYS = (
    "EPOCH",
    "MEAN_MOTION",
    "ECCENTRICITY",
    "INCLINATION",
    "RA_OF_ASC_NODE",
    "ARG_OF_PERICENTER",
    "MEAN_ANOMALY",
    "NORAD_CAT_ID",
    "BSTAR",
    "MEAN_MOTION_DOT",
)


class Sgp4Unavailable(RuntimeError):
    """Raised when ``sgp4`` (the optional ``[orbital]`` dep) is not installed."""


class OmmInitError(ValueError):
    """Raised when an OMM record cannot be turned into a valid ``Satrec``."""


@dataclass(frozen=True)
class SatState:
    """A propagated satellite state: TEME position + observer-relative geometry."""

    teme_km: tuple[float, float, float]
    sub_lat_deg: float
    sub_lon_deg: float
    altitude_m: float
    azimuth_deg: float
    elevation_deg: float
    slant_range_m: float


def build_satrec(fields: dict[str, Any]) -> Any:
    """Build an ``sgp4`` ``Satrec`` from an OMM JSON dict (lazy import of ``sgp4``).

    ``sgp4.omm.initialize`` coerces string values, so CelesTrak's JSON (numbers + strings)
    is fed verbatim. Raises :class:`Sgp4Unavailable` if the optional dep is absent (surfaced
    as one ``offline`` status), or :class:`OmmInitError` if a required field is missing or
    ``initialize`` fails — either way the caller skips the object, never plots a bad orbit.
    """
    try:
        from sgp4 import omm
        from sgp4.api import Satrec
    except ImportError as exc:  # surfaced by run_celestrak as an offline status
        raise Sgp4Unavailable(str(exc)) from exc

    missing = [k for k in REQUIRED_OMM_KEYS if not str(fields.get(k, "")).strip()]
    if missing:
        raise OmmInitError(f"OMM record missing required field(s): {', '.join(missing)}")

    sat = Satrec()
    try:
        omm.initialize(sat, fields)
    except Exception as exc:  # experimental OMM path — never trust a partial init
        raise OmmInitError(f"sgp4.omm.initialize failed: {exc}") from exc
    return sat


def propagate(
    sat: Any,
    when: datetime,
    *,
    observer_lat_deg: float,
    observer_lon_deg: float,
    observer_alt_m: float,
) -> SatState | None:
    """Propagate ``sat`` to ``when`` and derive observer geometry, or ``None`` to skip.

    Returns ``None`` (object skipped, fail-visibly) when SGP4 flags a propagation error
    (``e != 0`` — see ``sgp4.api.SGP4_ERRORS``, e.g. code 6 = decayed) or any position
    component is non-finite (NaN/inf). On success the TEME km vector is rotated to ECEF for
    the sub-satellite point and the observer look angles.
    """
    from sgp4.api import jday

    jd, fr = jday(
        when.year,
        when.month,
        when.day,
        when.hour,
        when.minute,
        when.second + when.microsecond * 1e-6,
    )
    err, r, _v = sat.sgp4(jd, fr)
    if err != 0:
        return None  # propagation error (decayed/underground/etc.) — skip, never plot
    if not all(math.isfinite(c) for c in r):
        return None  # a NaN position is "no position" — skip

    teme_km = (float(r[0]), float(r[1]), float(r[2]))
    ecef_km = teme_to_ecef(teme_km, when)
    ecef_m = (ecef_km[0] * 1000.0, ecef_km[1] * 1000.0, ecef_km[2] * 1000.0)
    sub_lat, sub_lon, alt_m = ecef_to_geodetic(ecef_m)
    az, el, rng = look_angles(ecef_km, observer_lat_deg, observer_lon_deg, observer_alt_m)
    return SatState(
        teme_km=teme_km,
        sub_lat_deg=sub_lat,
        sub_lon_deg=sub_lon,
        altitude_m=alt_m,
        azimuth_deg=az,
        elevation_deg=el,
        slant_range_m=rng,
    )


def sgp4_error_text(code: int) -> str:
    """Human text for an SGP4 error code from ``sgp4.api.SGP4_ERRORS`` (never hard-coded)."""
    from sgp4.api import SGP4_ERRORS

    return str(SGP4_ERRORS.get(code, f"SGP4 error {code}"))

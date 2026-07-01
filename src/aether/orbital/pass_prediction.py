"""Satellite pass prediction: rise / culmination / set (M6.8, PRD §12/§32 #18-#19).

:func:`aether.orbital.sgp4_propagate.propagate` only ever resolves an orbital object's
geometry at a single instant. This module answers the question that the CelesTrak adapter's
tick-by-tick propagation cannot: *when does the next (or in-progress) pass rise above the
elevation floor, when does it culminate, and when does it set?* — the prediction the M6
exit criterion ("watched satellite passes are predicted and alerted", PRD §32) requires, and
the input the new ``culmination_reached`` alert operator (#18) and the satellite-pass-end
template (#19) are built on.

Pure, deterministic, side-effect-free: only :func:`~aether.orbital.sgp4_propagate.propagate`
is called (no I/O, no wall-clock reads, no randomness), so the same ``(satrec, start,
window_s, min_elevation_deg, coarse_step_s)`` always yields the same :class:`PassPrediction`
— required because the CelesTrak adapter caches this result per watchlisted NORAD id.

Algorithm (PRD §37 honest-degradation stance throughout — never crash, never invent a
position):

1. **Coarse scan** — sample elevation every ``coarse_step_s`` (default 30 s) across
   ``[start, start + window_s]`` (default 24 h, comfortably longer than any LEO/MEO orbital
   period). 30 s is short enough to guarantee at least one above-floor sample even for a
   grazing pass that only dwells above the floor for a minute or two, without the cost of a
   finer step.
2. **Peak = MAX-FIRST** — walk the samples chronologically and take the *first* local
   maximum (an elevation slope sign change, ``+`` then ``-``) whose elevation exceeds
   ``min_elevation_deg``: that is the next (or already in-progress) pass. Bisecting outward
   from this peak for the rise/set floor crossings (rather than bracketing both crossings up
   front) is what makes this robust to short/grazing passes.
3. **Refine the peak** — ternary search on the elevation *value* (tolerance ~0.01°), not on
   time: elevation is nearly flat near culmination, so time-of-max converges slowly while the
   max value converges fast. Culmination time is accepted to within roughly +/-5 s.
4. **Refine the rise/set crossings** — bisect each floor crossing to ~1 s precision (~6
   iterations from the ~30 s coarse bracket).
5. **Degenerate fallback** — if the object is already above the floor at ``start`` and no
   interior maximum is found ahead of it (an in-progress pass whose true peak was before
   ``start``, or an object that never drops below the floor within the window at all, e.g.
   geostationary), ``start`` is reported as ``culmination_at`` with the elevation at
   ``start`` as ``max_elevation_deg`` — a documented stand-in for "already at its (unresolved)
   peak", never a fabricated instant. ``rise_at`` is ``None`` (the rise was before the
   window); ``set_at`` is still searched for forward across the whole window.
6. If the object never exceeds the floor anywhere in the window (and the degenerate fallback
   does not apply), there is no pass to report: ``None``.
7. If :func:`~aether.orbital.sgp4_propagate.propagate` ever fails (decayed object / SGP4
   error / non-finite position) at *any* sampled instant, the whole prediction is **aborted**
   — ``None`` is returned rather than a partial/fake result.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from aether.orbital.sgp4_propagate import propagate

#: Ternary-search iteration cap for the culmination refine (fixed, deterministic — no
#: time-based loop). The value tolerance below ends the search early in practice.
_MAX_PEAK_ITERS = 60
#: Stop refining the peak once the best elevation found changes by less than this between
#: iterations (degrees) — elevation is the thing that matters, not the time-of-max.
_PEAK_TOL_DEG = 0.01
#: Bisection iterations for a floor crossing: from a ~30 s coarse bracket, 6 halvings land
#: under ~1 s (30 / 2**6 ~= 0.47 s).
_CROSSING_BISECT_ITERS = 6


class _Aborted(Exception):
    """Internal sentinel: a sampled instant failed to propagate (decayed object) — the
    whole prediction is abandoned, never a partial result (PRD §37)."""


@dataclass(frozen=True)
class PassPrediction:
    """One observer pass of an orbital object (rise -> culmination -> set).

    All datetimes are timezone-aware UTC. ``culmination_at`` and ``max_elevation_deg`` are
    ALWAYS set when a ``PassPrediction`` is returned. ``rise_at`` / ``set_at`` are ``None``
    when the corresponding floor crossing does not occur within the search window:

    - an **in-progress pass** already above the floor at the search start (``rise_at`` is
      ``None`` — its rise happened before the window began), and/or
    - an object that **never sets** within the window (``set_at`` is ``None``) — most
      notably a geostationary object, which can stay above the floor for the entire window.

    In the degenerate case where no interior elevation maximum is found ahead of the search
    start (see module docstring, step 5), the search start itself is reported as
    ``culmination_at`` with the elevation at that instant as ``max_elevation_deg`` — a
    deliberate, documented stand-in for "already at its (unresolved) peak", not a claim that
    the satellite's true geometric maximum occurs at exactly that instant.
    """

    rise_at: datetime | None
    culmination_at: datetime
    set_at: datetime | None
    max_elevation_deg: float


def _elevation_at(
    satrec: Any, when: datetime, *, lat: float, lon: float, alt: float
) -> float | None:
    """Propagate to one instant and return elevation (deg), or ``None`` to abort the scan."""
    state = propagate(satrec, when, observer_lat_deg=lat, observer_lon_deg=lon, observer_alt_m=alt)
    return None if state is None else state.elevation_deg


def _elev(satrec: Any, when: datetime, *, lat: float, lon: float, alt: float) -> float:
    """Like :func:`_elevation_at` but raises :class:`_Aborted` instead of returning ``None``."""
    elevation = _elevation_at(satrec, when, lat=lat, lon=lon, alt=alt)
    if elevation is None:
        raise _Aborted
    return elevation


def _scan(
    satrec: Any,
    start: datetime,
    end: datetime,
    step_s: float,
    *,
    lat: float,
    lon: float,
    alt: float,
) -> list[tuple[datetime, float]]:
    """Fixed-step elevation samples over ``[start, end]`` (inclusive of both ends)."""
    samples: list[tuple[datetime, float]] = []
    t = start
    while True:
        samples.append((t, _elev(satrec, t, lat=lat, lon=lon, alt=alt)))
        if t >= end:
            break
        t = min(t + timedelta(seconds=step_s), end)
    return samples


def _bisect_crossing(
    satrec: Any,
    t_lo: datetime,
    t_hi: datetime,
    floor: float,
    *,
    lat: float,
    lon: float,
    alt: float,
    rising: bool,
    iters: int = _CROSSING_BISECT_ITERS,
) -> datetime:
    """Bisect the single floor crossing inside ``[t_lo, t_hi]`` (``t_lo`` on the ``not
    rising`` side of the floor, ``t_hi`` on the ``rising`` side, or vice versa for a falling
    crossing) to ~1 s precision."""
    for _ in range(iters):
        t_mid = t_lo + (t_hi - t_lo) / 2
        above = _elev(satrec, t_mid, lat=lat, lon=lon, alt=alt) > floor
        if above == rising:
            t_hi = t_mid
        else:
            t_lo = t_mid
    return t_lo + (t_hi - t_lo) / 2


def _find_rising_crossing(
    satrec: Any,
    samples: list[tuple[datetime, float]],
    lo_idx: int,
    hi_idx: int,
    floor: float,
    *,
    lat: float,
    lon: float,
    alt: float,
) -> datetime | None:
    """The below->above floor crossing in ``samples[lo_idx..hi_idx]`` closest to ``hi_idx``
    (the peak) — i.e. the rise leading into *this* pass, not an earlier unrelated blip."""
    for j in range(hi_idx - 1, lo_idx - 1, -1):
        if samples[j][1] <= floor < samples[j + 1][1]:
            return _bisect_crossing(
                satrec,
                samples[j][0],
                samples[j + 1][0],
                floor,
                lat=lat,
                lon=lon,
                alt=alt,
                rising=True,
            )
    return None


def _find_falling_crossing(
    satrec: Any,
    samples: list[tuple[datetime, float]],
    lo_idx: int,
    hi_idx: int,
    floor: float,
    *,
    lat: float,
    lon: float,
    alt: float,
) -> datetime | None:
    """The above->below floor crossing in ``samples[lo_idx..hi_idx]`` closest to ``lo_idx``
    (the peak) — i.e. the set ending *this* pass, not a later unrelated one."""
    for j in range(lo_idx, hi_idx):
        if samples[j][1] > floor >= samples[j + 1][1]:
            return _bisect_crossing(
                satrec,
                samples[j][0],
                samples[j + 1][0],
                floor,
                lat=lat,
                lon=lon,
                alt=alt,
                rising=False,
            )
    return None


def _refine_peak(
    satrec: Any,
    t_lo: datetime,
    t_hi: datetime,
    *,
    lat: float,
    lon: float,
    alt: float,
) -> tuple[datetime, float]:
    """Ternary-search the elevation maximum within ``[t_lo, t_hi]`` (a ~60 s bracket).

    Tolerance is on the ELEVATION VALUE, not time (see module docstring step 3). Fixed
    iteration cap with an early value-tolerance exit — deterministic either way.
    """
    prev_best: float | None = None
    for _ in range(_MAX_PEAK_ITERS):
        third = (t_hi - t_lo) / 3
        if third.total_seconds() < 0.05:
            break
        m1 = t_lo + third
        m2 = t_hi - third
        e1 = _elev(satrec, m1, lat=lat, lon=lon, alt=alt)
        e2 = _elev(satrec, m2, lat=lat, lon=lon, alt=alt)
        if e1 < e2:
            t_lo = m1
        else:
            t_hi = m2
        best = max(e1, e2)
        if prev_best is not None and abs(best - prev_best) < _PEAK_TOL_DEG:
            break
        prev_best = best
    t_mid = t_lo + (t_hi - t_lo) / 2
    return t_mid, _elev(satrec, t_mid, lat=lat, lon=lon, alt=alt)


def predict_next_pass(
    satrec: Any,
    start: datetime,
    *,
    observer_lat_deg: float,
    observer_lon_deg: float,
    observer_alt_m: float,
    window_s: float = 86400.0,
    min_elevation_deg: float = 10.0,
    coarse_step_s: float = 30.0,
) -> PassPrediction | None:
    """Find the next (or in-progress) pass of ``satrec`` over the observer, or ``None``.

    ``satrec`` is an ``sgp4.api.Satrec`` (typed ``Any``, mirroring
    :func:`aether.orbital.sgp4_propagate.propagate`). Returns ``None`` only when no
    elevation maximum above ``min_elevation_deg`` exists anywhere in ``[start, start +
    window_s]`` (e.g. an object that never clears the floor), or when ``propagate()`` fails
    at any sampled instant (a decayed object — aborted, no partial result). See the module
    docstring for the full algorithm. Deterministic: identical inputs always yield an
    identical (or equally-``None``) result, since this feeds the CelesTrak adapter's
    per-NORAD-id cache.
    """
    lat, lon, alt = observer_lat_deg, observer_lon_deg, observer_alt_m
    try:
        end = start + timedelta(seconds=window_s)
        samples = _scan(satrec, start, end, coarse_step_s, lat=lat, lon=lon, alt=alt)
        n = len(samples)

        peak_idx: int | None = None
        for i in range(1, n - 1):
            e_prev = samples[i - 1][1]
            e_i = samples[i][1]
            e_next = samples[i + 1][1]
            if e_i > e_prev and e_i > e_next and e_i > min_elevation_deg:
                peak_idx = i
                break

        if peak_idx is None:
            start_elev = samples[0][1]
            if start_elev <= min_elevation_deg:
                return None  # never above the floor anywhere in the window
            # Degenerate fallback (module docstring step 5): already above the floor at
            # `start` with no interior maximum found ahead of it.
            fallback_set_at = _find_falling_crossing(
                satrec, samples, 0, n - 1, min_elevation_deg, lat=lat, lon=lon, alt=alt
            )
            return PassPrediction(
                rise_at=None,
                culmination_at=start,
                set_at=fallback_set_at,
                max_elevation_deg=start_elev,
            )

        culmination_at, max_elevation_deg = _refine_peak(
            satrec, samples[peak_idx - 1][0], samples[peak_idx + 1][0], lat=lat, lon=lon, alt=alt
        )

        # Always search for the SELECTED pass's own rise/set crossings and trust the finders'
        # own `None` (no crossing found) — gating this search on the elevation at the window's
        # ENDPOINT is wrong whenever a *different, unrelated* pass occupies that endpoint (e.g.
        # a later pass still above the floor at `start + window_s`, or an earlier/different
        # pass above the floor at `start` while THIS pass's own rise is genuinely inside the
        # window). The finders already search only within `[0, peak_idx]` / `[peak_idx, n-1]`
        # and correctly return `None` when the crossing truly isn't in that sub-range.
        rise_at = _find_rising_crossing(
            satrec, samples, 0, peak_idx, min_elevation_deg, lat=lat, lon=lon, alt=alt
        )
        set_at = _find_falling_crossing(
            satrec, samples, peak_idx, n - 1, min_elevation_deg, lat=lat, lon=lon, alt=alt
        )

        return PassPrediction(
            rise_at=rise_at,
            culmination_at=culmination_at,
            set_at=set_at,
            max_elevation_deg=max_elevation_deg,
        )
    except _Aborted:
        return None

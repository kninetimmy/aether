"""Unit tests for satellite pass prediction (M6.8, PRD §12/§32 #18-#19).

Validates the ROOT-FINDER (coarse-scan + bisection/ternary refinement) in
:mod:`aether.orbital.pass_prediction` against a brute-force 1 s-step reference scan of the
same :func:`~aether.orbital.sgp4_propagate.propagate` the production path uses — the
propagator itself is already pinned to published Vallado reference values in
test_orbital_transforms.py, so this module does not re-pin it.

Fixtures reuse the canned roster from :mod:`aether.adapters.celestrak_fake_feeder` (the same
fake-provider OMM the CelesTrak adapter tests use) rather than inventing new orbital elements:
a real ISS (LEO, NORAD 25544), a synthetic always-above-the-floor GEO (NORAD 99001, solved to
sit over the observer), and a synthetic always-below-the-floor GEO (NORAD 99002, ~150 deg of
longitude away).
"""

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from aether.adapters.celestrak import build_satrecs
from aether.adapters.celestrak_fake_feeder import FakeCelestrakProvider
from aether.orbital import pass_prediction as pp
from aether.orbital.pass_prediction import PassPrediction, predict_next_pass
from aether.orbital.sgp4_propagate import propagate

# The whole module drives the REAL SGP4 propagate path (mirrors test_celestrak_adapter.py's
# stance) — skip cleanly when the optional `[orbital]` extra is absent rather than failing.
pytest.importorskip("sgp4")

OBS_LAT, OBS_LON = 30.0, -97.0
NOW = datetime(2026, 6, 21, 18, 0, 0, tzinfo=UTC)

# A start time + short window isolating a single SHORT, GRAZING ISS pass (peak ~10.13 deg,
# found empirically against this exact roster/observer/epoch): the prior pass over this
# observer sets at 05:21:45Z and the next rises at 06:55:05Z, so a 06:00:00Z start with a
# 90-minute window captures this grazing pass alone as the chronologically FIRST (and only)
# maximum above the floor — exactly what MAX-FIRST peak selection must resolve correctly.
GRAZING_START = datetime(2026, 6, 25, 6, 0, 0, tzinfo=UTC)
GRAZING_WINDOW_S = 5400.0


def _elements() -> list[Any]:
    async def _rows() -> list[dict[str, Any]]:
        feeder = FakeCelestrakProvider(
            observer_lat=OBS_LAT, observer_lon=OBS_LON, now_fn=lambda: NOW
        )
        return await feeder.fetch_group("stations")

    rows = asyncio.run(_rows())
    elements, _skipped = build_satrecs(rows, group="stations")
    return elements


_ELEMENTS = _elements()
_ISS = next(e for e in _ELEMENTS if e.norad_id == 25544)
_GEO_OVER = next(e for e in _ELEMENTS if e.norad_id == 99001)  # AETHER-GEO-OVERHEAD
_GEO_FAR = next(e for e in _ELEMENTS if e.norad_id == 99002)  # AETHER-GEO-FAR


def _brute_force_pass(
    satrec: Any,
    start: datetime,
    window_s: float,
    *,
    floor: float = 10.0,
    step_s: float = 1.0,
) -> tuple[datetime | None, tuple[datetime, float] | None, datetime | None]:
    """Brute-force 1 s-step reference: ``(rise, (culmination_at, max_elev), set)`` for the
    FIRST above-floor run in the window (mirrors MAX-FIRST semantics). ``rise``/``set`` are
    ``None`` when the run starts already above the floor / never drops back below it."""
    n = int(window_s // step_s) + 1
    rise: datetime | None = None
    set_at: datetime | None = None
    best: tuple[datetime, float] | None = None
    in_pass = False
    for i in range(n):
        t = start + timedelta(seconds=i * step_s)
        state = propagate(
            satrec, t, observer_lat_deg=OBS_LAT, observer_lon_deg=OBS_LON, observer_alt_m=0.0
        )
        elev = state.elevation_deg if state is not None else None
        if elev is not None and elev > floor:
            if not in_pass:
                in_pass = True
                if i > 0:
                    rise = t
            if best is None or elev > best[1]:
                best = (t, elev)
        elif in_pass:
            set_at = t
            break
    return rise, best, set_at


# --- Normal high ISS-class pass -------------------------------------------------


def test_normal_pass_matches_brute_force_reference() -> None:
    ref_rise, ref_best, ref_set = _brute_force_pass(_ISS.satrec, NOW, 86400.0)
    assert ref_rise is not None and ref_best is not None and ref_set is not None

    pred = predict_next_pass(
        _ISS.satrec, NOW, observer_lat_deg=OBS_LAT, observer_lon_deg=OBS_LON, observer_alt_m=0.0
    )

    assert pred is not None
    assert pred.rise_at is not None and pred.set_at is not None
    assert pred.rise_at.tzinfo is not None and pred.culmination_at.tzinfo is not None
    assert pred.set_at.tzinfo is not None
    assert pred.rise_at < pred.culmination_at < pred.set_at

    assert abs((pred.rise_at - ref_rise).total_seconds()) <= 30.0
    assert abs((pred.set_at - ref_set).total_seconds()) <= 30.0
    assert abs((pred.culmination_at - ref_best[0]).total_seconds()) <= 30.0
    assert pred.max_elevation_deg == pytest.approx(ref_best[1], abs=0.5)


# --- Grazing pass (peak just above the floor) -----------------------------------


def test_grazing_pass_is_found_just_above_the_floor() -> None:
    ref_rise, ref_best, ref_set = _brute_force_pass(_ISS.satrec, GRAZING_START, GRAZING_WINDOW_S)
    assert ref_rise is not None and ref_best is not None and ref_set is not None
    assert ref_best[1] < 11.0  # confirm the fixture is in fact a grazing (low-margin) pass

    pred = predict_next_pass(
        _ISS.satrec,
        GRAZING_START,
        observer_lat_deg=OBS_LAT,
        observer_lon_deg=OBS_LON,
        observer_alt_m=0.0,
        window_s=GRAZING_WINDOW_S,
    )

    assert pred is not None
    assert pred.rise_at is not None and pred.set_at is not None
    assert pred.rise_at < pred.culmination_at < pred.set_at
    assert pred.max_elevation_deg > 10.0  # a pass IS found above the floor
    assert pred.max_elevation_deg == pytest.approx(ref_best[1], abs=0.5)
    assert abs((pred.rise_at - ref_rise).total_seconds()) <= 30.0
    assert abs((pred.set_at - ref_set).total_seconds()) <= 30.0


# --- Rise/set crossings must belong to the SELECTED pass, not an unrelated one at a window
# --- endpoint (regression: the endpoint-gated `rise_at`/`set_at` checks removed in M6.8's fix
# --- pass produced a confidently-wrong `None` whenever a DIFFERENT pass occupied the window's
# --- start or end instant) -------------------------------------------------------------------


def test_set_at_not_suppressed_by_unrelated_later_pass_at_window_end() -> None:
    start = datetime(2026, 6, 21, 6, 5, 0, tzinfo=UTC)
    end_state = propagate(
        _ISS.satrec,
        start + timedelta(seconds=86400.0),
        observer_lat_deg=OBS_LAT,
        observer_lon_deg=OBS_LON,
        observer_alt_m=0.0,
    )
    # Precondition: the window's END instant sits inside a LATER, unrelated pass still above
    # the floor — exactly the condition that made the old endpoint gate misfire.
    assert end_state is not None and end_state.elevation_deg > 10.0

    ref_rise, ref_best, ref_set = _brute_force_pass(_ISS.satrec, start, 86400.0)
    assert ref_rise is not None and ref_best is not None and ref_set is not None

    pred = predict_next_pass(
        _ISS.satrec, start, observer_lat_deg=OBS_LAT, observer_lon_deg=OBS_LON, observer_alt_m=0.0
    )
    assert pred is not None
    assert pred.set_at is not None  # must NOT be None just because the window end is above floor
    assert abs((pred.set_at - ref_set).total_seconds()) <= 30.0


def test_rise_at_not_suppressed_when_window_start_is_a_different_descending_pass() -> None:
    start = datetime(2026, 6, 21, 6, 56, 0, tzinfo=UTC)
    start_state = propagate(
        _ISS.satrec, start, observer_lat_deg=OBS_LAT, observer_lon_deg=OBS_LON, observer_alt_m=0.0
    )
    # Precondition: `start` itself sits inside a DIFFERENT, already-descending pass — exactly
    # the condition that made the old endpoint gate misfire (it forced rise_at=None for the
    # NEXT pass, which has a perfectly genuine rise).
    assert start_state is not None and start_state.elevation_deg > 10.0

    # Reference: skip past the in-progress pass's own tail (its floor-crossing set) — that
    # descending tail has no interior local max in-window, so predict_next_pass's MAX-FIRST
    # peak search skips it and reports the NEXT full pass, which is what we validate against.
    _tail_rise, _tail_best, tail_set = _brute_force_pass(_ISS.satrec, start, 86400.0)
    assert tail_set is not None
    ref_rise, ref_best, ref_set = _brute_force_pass(
        _ISS.satrec, tail_set, 86400.0 - (tail_set - start).total_seconds()
    )
    assert ref_rise is not None and ref_best is not None and ref_set is not None

    pred = predict_next_pass(
        _ISS.satrec, start, observer_lat_deg=OBS_LAT, observer_lon_deg=OBS_LON, observer_alt_m=0.0
    )
    assert pred is not None
    assert pred.rise_at is not None  # must NOT be None just because `start` was above the floor
    assert abs((pred.rise_at - ref_rise).total_seconds()) <= 30.0
    assert abs((pred.culmination_at - ref_best[0]).total_seconds()) <= 30.0


# --- GEO edge cases --------------------------------------------------------------


def test_geo_always_above_returns_in_progress_pass() -> None:
    pred = predict_next_pass(
        _GEO_OVER.satrec,
        NOW,
        observer_lat_deg=OBS_LAT,
        observer_lon_deg=OBS_LON,
        observer_alt_m=0.0,
    )
    assert pred is not None
    assert pred.rise_at is None
    assert pred.set_at is None
    assert pred.culmination_at.tzinfo is not None
    assert pred.max_elevation_deg > 10.0


def test_geo_always_below_returns_none() -> None:
    pred = predict_next_pass(
        _GEO_FAR.satrec,
        NOW,
        observer_lat_deg=OBS_LAT,
        observer_lon_deg=OBS_LON,
        observer_alt_m=0.0,
    )
    assert pred is None


# --- Decayed / propagate()-returns-None mid-scan ----------------------------------


def test_decayed_object_mid_scan_aborts_to_none(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    def _flaky_propagate(*args: Any, **kwargs: Any) -> Any:
        calls["n"] += 1
        if calls["n"] > 5:  # fail partway through the coarse scan — no partial result
            return None
        return propagate(*args, **kwargs)

    monkeypatch.setattr(pp, "propagate", _flaky_propagate)

    pred = predict_next_pass(
        _ISS.satrec, NOW, observer_lat_deg=OBS_LAT, observer_lon_deg=OBS_LON, observer_alt_m=0.0
    )
    assert pred is None
    assert calls["n"] > 5  # confirms the scan actually ran into the injected failure


# --- Determinism -------------------------------------------------------------------


def test_determinism_same_inputs_same_result() -> None:
    kwargs = dict(
        observer_lat_deg=OBS_LAT, observer_lon_deg=OBS_LON, observer_alt_m=0.0, window_s=86400.0
    )
    first = predict_next_pass(_ISS.satrec, NOW, **kwargs)
    second = predict_next_pass(_ISS.satrec, NOW, **kwargs)
    assert first is not None and second is not None
    assert first == second
    assert isinstance(first, PassPrediction)

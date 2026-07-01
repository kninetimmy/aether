#!/usr/bin/env python3
"""CelesTrak SGP4 propagation benchmark (PRD §11.14 ORBIT-FR-011, §18.12, M6.6b Part B).

The two-tier propagator (ORBIT-FR-011) splits the synced catalog into a small **fast** tier
(the operator's watchlisted objects, propagated every ~2 s) and the broad **slow** tier (the
whole ``active`` group, propagated every 15 s). The fast tier is a handful of objects and is
negligible; the dominant Pi-5 cost is one **slow-tier tick** — propagating the entire ``active``
group with SGP4 and filtering to those above the horizon. This script measures exactly that one
slow tick, repeatedly, the way :func:`aether.adapters.celestrak._propagate_set` runs it:

    fetch the active GP group once  →  build a Satrec per object (build_satrecs)
                                    →  propagate ALL to now + elevation-filter (one slow tick)

It measures the one-time build cost, the per-tick propagate wall time (mean over ``--iters``),
the above-horizon count, and process peak RSS, then renders a verdict comparing the mean tick
time against the 15 s slow-tier budget with margin — the GLM benchmark-gate pattern.

Read-only against CelesTrak's public GP service (PRD §38; anonymous HTTPS, ``FORMAT=json``
explicit, a SINGLE fetch — never loop fetches; ~50 errors in 2 h firewalls the IP). Requires
the optional ``sgp4`` parser (``pip install "aether[orbital]"``). Run from the repo root:

    python scripts/bench_celestrak.py                       # active group, 10 ticks
    python scripts/bench_celestrak.py --group active --iters 10 --lat 39 --lon -98
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from typing import Any

GP_URL = "https://celestrak.org/NORAD/elements/gp.php?GROUP={group}&FORMAT=json"
SLOW_BUDGET_S = 15.0  # mirrors aether.config.DEFAULT_CELESTRAK_PROPAGATE_S (the slow tier)
FAST_BUDGET_S = 2.0  # mirrors aether.config.DEFAULT_CELESTRAK_PROPAGATE_FAST_S (M6.8)


def _peak_rss_mb() -> float | None:
    """Process peak RSS in MB, or ``None`` where the POSIX-only ``resource`` module isn't
    available (e.g. Windows dev machines). RSS is a secondary diagnostic here — SGP4 memory
    use is already known to be far below the 1024 MB gate — so its absence degrades the
    printed report, never the benchmark itself (honest degradation, PRD §37)."""
    try:
        import resource
    except ImportError:
        return None
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024  # KB→MB on Linux


def _get(url: str, timeout_s: float = 30.0) -> bytes:
    # https-only — never downgrade the public CelesTrak service (mirrors the adapter guard).
    assert url.startswith("https://"), f"refusing non-https URL: {url!r}"
    req = urllib.request.Request(url, headers={"User-Agent": "aether-celestrak-bench/1.0"})
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:  # noqa: S310 (https public)
        return bytes(resp.read())


def _bench_pass_predict(elements: list[Any], args: argparse.Namespace) -> int:
    """Time one M6.8 pass-prediction recompute for an ISS-class object (PRD §32 #18/#19).

    Picks NORAD 25544 (ISS) out of the already-built element set (falling back to the first
    element if 25544 is absent — e.g. a group that does not include ISS), then times
    ``predict_next_pass`` over ``--iters`` calls and reports the mean wall time and margin
    against the **2 s fast-tier** propagate budget (the budget this recompute must fit inside
    if it stays inline on the event loop — see ``docs/orbital-celestrak.md``).
    """
    from aether.orbital.pass_prediction import predict_next_pass

    target = next((e for e in elements if e.norad_id == 25544), elements[0])
    print(f"# pass-prediction bench: NORAD {target.norad_id} ({target.object_name})")
    print(f"# iters={args.iters}  min_elevation={args.min_elevation}\n")

    widths = (("iter", 4), ("predict_s", 11), ("result", 18))
    hdr = "  ".join(f"{name:>{w}}" for name, w in widths)
    print(hdr)
    print("-" * len(hdr))

    times: list[float] = []
    for i in range(args.iters):
        start = datetime.now(UTC)
        t0 = time.monotonic()
        pred = predict_next_pass(
            target.satrec,
            start,
            observer_lat_deg=args.lat,
            observer_lon_deg=args.lon,
            observer_alt_m=args.alt_m,
            min_elevation_deg=args.min_elevation,
        )
        dt = time.monotonic() - t0
        times.append(dt)
        result = "pass found" if pred is not None else "no pass in window"
        print(f"{i:>4}  {dt:>11.3f}  {result:>18}")

    mean_t = sum(times) / len(times) if times else 0.0
    margin = FAST_BUDGET_S / mean_t if mean_t else float("inf")

    print("\n# ---- summary (mean) ----")
    print(f"recompute/call   : {mean_t:8.3f} s   (budget {FAST_BUDGET_S:.0f}s/fast tick)")
    print(
        f"\n# real-time margin: {margin:.1f}x  ({FAST_BUDGET_S:.0f}s cadence / {mean_t:.3f}s work)"
    )
    print(
        "# NOTE: this script cannot detect Pi-5 hardware — label this result by the machine it "
        "actually ran on (e.g. '(dev machine, not Pi-5)') in docs/orbital-celestrak.md; never "
        "report a non-Pi-5 run as a Pi-5 number."
    )
    if margin >= 4.0:
        print("# VERDICT: ACCEPTABLE — comfortable margin to stay inline on the event loop.")
        return 0
    if margin >= 1.5:
        print("# VERDICT: MARGINAL — keeps up but with limited headroom; consider to_thread.")
        return 0
    print("# VERDICT: NOT VIABLE inline — wrap predict_next_pass in asyncio.to_thread.")
    return 1


def main() -> int:
    ap = argparse.ArgumentParser(description="CelesTrak SGP4 propagation Pi benchmark")
    ap.add_argument("--group", default="active", help="GP group slug (active is the heavy one)")
    ap.add_argument("--iters", type=int, default=10, help="slow-tier ticks to time")
    ap.add_argument("--lat", type=float, default=39.0, help="observer latitude")
    ap.add_argument("--lon", type=float, default=-98.0, help="observer longitude")
    ap.add_argument("--alt-m", type=float, default=0.0, help="observer altitude (m, WGS-84)")
    ap.add_argument("--min-elevation", type=float, default=10.0, help="horizon floor (deg)")
    ap.add_argument("--timeout", type=float, default=30.0, help="HTTP timeout (s)")
    ap.add_argument(
        "--pass-predict",
        action="store_true",
        help="time one pass-prediction recompute for an ISS-class object (M6.8) instead of "
        "the slow-tier catalog-propagate benchmark",
    )
    args = ap.parse_args()

    try:
        import sgp4  # noqa: F401

        from aether.adapters.celestrak import build_satrecs, element_to_record
    except ImportError:
        print('sgp4 not installed — pip install "aether[orbital]"')
        return 2

    group_q = urllib.parse.quote(args.group, safe="")
    url = GP_URL.format(group=group_q)
    print(f"# CelesTrak benchmark  group={args.group}  observer=({args.lat},{args.lon})")
    print(f"# url={url}  iters={args.iters}  min_elevation={args.min_elevation}")

    # --- Single fetch (§38-safe: never loop fetches; one bench fetch is fine) ---
    t0 = time.monotonic()
    raw = _get(url, timeout_s=args.timeout)
    fetch_s = time.monotonic() - t0
    rows: Any = json.loads(raw)
    if not isinstance(rows, list) or not rows:
        print("No GP rows returned — the group may be empty or the service delayed.")
        return 1
    print(f"# fetched {len(rows)} OMM rows in {fetch_s:.2f}s\n")

    # --- Build phase: one Satrec per object (the one-time sync cost) ---
    b0 = time.monotonic()
    elements, skipped = build_satrecs(rows, group=args.group)
    build_s = time.monotonic() - b0
    build_rss_mb = _peak_rss_mb()
    rss_note = f"RSS {build_rss_mb:.0f} MB" if build_rss_mb is not None else "RSS n/a (non-POSIX)"
    print(f"# built {len(elements)} satrecs ({skipped} skipped) in {build_s:.2f}s, {rss_note}\n")
    if not elements:
        print("No usable elements built — cannot benchmark propagation.")
        return 1

    if args.pass_predict:
        return _bench_pass_predict(elements, args)

    widths = (("iter", 4), ("propagate_s", 11), ("above", 6))
    hdr = "  ".join(f"{name:>{w}}" for name, w in widths)
    print(hdr)
    print("-" * len(hdr))

    # --- Propagate phase: each iteration is exactly one slow-tier tick over ALL elements ---
    prop_t: list[float] = []
    above_all: list[int] = []
    for i in range(args.iters):
        at = datetime.now(UTC)
        a = time.monotonic()
        above = 0
        for element in elements:
            record = element_to_record(
                element,
                observer_lat=args.lat,
                observer_lon=args.lon,
                observer_alt_m=args.alt_m,
                at=at,
                valid_s=30.0,
            )
            if record is None:
                continue  # NaN/decayed — skipped, never plotted (fail-visibly, §37)
            elevation = record.attributes["elevation_deg"]
            if isinstance(elevation, (int, float)) and elevation >= args.min_elevation:
                above += 1
        dt = time.monotonic() - a
        prop_t.append(dt)
        above_all.append(above)
        print(f"{i:>4}  {dt:>11.3f}  {above:>6}")

    def mean(xs: list[float]) -> float:
        return sum(xs) / len(xs) if xs else 0.0

    peak_rss_mb = _peak_rss_mb()
    mean_prop = mean(prop_t)

    print("\n# ---- summary (means) ----")
    print(f"catalog size     : {len(elements):8d} objects")
    print(f"build (satrecs)  : {build_s:8.3f} s   (one-time per sync, every 6 h)")
    print(f"propagate/tick   : {mean_prop:8.3f} s   (budget {SLOW_BUDGET_S:.0f}s/slow tick)")
    print(f"above/tick       : {mean([float(x) for x in above_all]):8.0f}")
    if peak_rss_mb is not None:
        print(f"peak RSS         : {peak_rss_mb:8.0f} MB  (process incl. sgp4)")
    else:
        print("peak RSS         :      n/a  (resource module unavailable — non-POSIX)")

    margin = SLOW_BUDGET_S / mean_prop if mean_prop else float("inf")
    print(
        f"\n# real-time margin: {margin:.1f}x  "
        f"({SLOW_BUDGET_S:.0f}s cadence / {mean_prop:.2f}s work)   "
        "# SGP4 RSS is far below 1024 MB, so the margin is the real gate."
    )
    if margin >= 4.0 and (peak_rss_mb is None or peak_rss_mb < 1024):
        print("# VERDICT: ACCEPTABLE — comfortable real-time margin and bounded memory.")
        return 0
    if margin >= 1.5:
        print("# VERDICT: MARGINAL — keeps up but with limited headroom; see notes.")
        return 0
    print("# VERDICT: NOT VIABLE — cannot sustain the 15s slow-tier cadence on this hardware.")
    return 1


if __name__ == "__main__":
    sys.exit(main())

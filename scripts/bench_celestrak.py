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
import resource
import sys
import time
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from typing import Any

GP_URL = "https://celestrak.org/NORAD/elements/gp.php?GROUP={group}&FORMAT=json"
SLOW_BUDGET_S = 15.0  # mirrors aether.config.DEFAULT_CELESTRAK_PROPAGATE_S (the slow tier)


def _get(url: str, timeout_s: float = 30.0) -> bytes:
    # https-only — never downgrade the public CelesTrak service (mirrors the adapter guard).
    assert url.startswith("https://"), f"refusing non-https URL: {url!r}"
    req = urllib.request.Request(url, headers={"User-Agent": "aether-celestrak-bench/1.0"})
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:  # noqa: S310 (https public)
        return bytes(resp.read())


def main() -> int:
    ap = argparse.ArgumentParser(description="CelesTrak SGP4 propagation Pi benchmark")
    ap.add_argument("--group", default="active", help="GP group slug (active is the heavy one)")
    ap.add_argument("--iters", type=int, default=10, help="slow-tier ticks to time")
    ap.add_argument("--lat", type=float, default=39.0, help="observer latitude")
    ap.add_argument("--lon", type=float, default=-98.0, help="observer longitude")
    ap.add_argument("--alt-m", type=float, default=0.0, help="observer altitude (m, WGS-84)")
    ap.add_argument("--min-elevation", type=float, default=10.0, help="horizon floor (deg)")
    ap.add_argument("--timeout", type=float, default=30.0, help="HTTP timeout (s)")
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
    build_rss_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024  # KB→MB on Linux
    print(
        f"# built {len(elements)} satrecs ({skipped} skipped) in {build_s:.2f}s, "
        f"RSS {build_rss_mb:.0f} MB\n"
    )
    if not elements:
        print("No usable elements built — cannot benchmark propagation.")
        return 1

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

    peak_rss_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024  # KB→MB on Linux
    mean_prop = mean(prop_t)

    print("\n# ---- summary (means) ----")
    print(f"catalog size     : {len(elements):8d} objects")
    print(f"build (satrecs)  : {build_s:8.3f} s   (one-time per sync, every 6 h)")
    print(f"propagate/tick   : {mean_prop:8.3f} s   (budget {SLOW_BUDGET_S:.0f}s/slow tick)")
    print(f"above/tick       : {mean([float(x) for x in above_all]):8.0f}")
    print(f"peak RSS         : {peak_rss_mb:8.0f} MB  (process incl. sgp4)")

    margin = SLOW_BUDGET_S / mean_prop if mean_prop else float("inf")
    print(
        f"\n# real-time margin: {margin:.1f}x  "
        f"({SLOW_BUDGET_S:.0f}s cadence / {mean_prop:.2f}s work)   "
        "# SGP4 RSS is far below 1024 MB, so the margin is the real gate."
    )
    if margin >= 4.0 and peak_rss_mb < 1024:
        print("# VERDICT: ACCEPTABLE — comfortable real-time margin and bounded memory.")
        return 0
    if margin >= 1.5:
        print("# VERDICT: MARGINAL — keeps up but with limited headroom; see notes.")
        return 0
    print("# VERDICT: NOT VIABLE — cannot sustain the 15s slow-tier cadence on this hardware.")
    return 1


if __name__ == "__main__":
    sys.exit(main())

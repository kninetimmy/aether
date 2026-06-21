#!/usr/bin/env python3
"""NOAA GLM Level-2 ingestion benchmark (PRD §11.10 LIGHTNING-FR-002, §18.7, M5.6).

GLM is **benchmark-gated**: the open lightning baseline uses NOAA GOES GLM L2 *only when
the Pi resource benchmark is acceptable*. There is no server-side AOI filter — each 20 s
GLM L2 file is a whole-disk product that must be downloaded and parsed in full before the
out-of-AOI flashes are discarded (LIGHTNING-FR-005). That download→parse→filter→discard
loop, run every 20 s forever, is the cost this script measures on the actual hardware:

    newest GLM L2 files  →  download once  →  parse flashes (in-memory netCDF4)
                         →  AOI filter     →  discard

It measures, per file and in aggregate: download time, parse time, AOI-filter time, flashes
per file, in-AOI flashes, and process peak RSS (parser memory). The verdict compares the
mean per-file wall time against the 20 s file cadence (the real-time budget) with margin.

Read-only against NOAA's public GOES Open Data on AWS (PRD §38; anonymous HTTPS, no creds).
Requires ``netCDF4`` (optional dep): ``pip install netCDF4``. Run from the repo root:

    python scripts/bench_glm.py                 # GOES-19 (East), 12 newest files
    python scripts/bench_glm.py --files 20 --sat G19 --lat 39 --lon -98 --radius-nm 500
"""

from __future__ import annotations

import argparse
import math
import resource
import sys
import time
import urllib.request
from datetime import UTC, datetime, timedelta

S3_HOST = "https://noaa-goes{sat_num}.s3.amazonaws.com"
PRODUCT = "GLM-L2-LCFA"
FILE_CADENCE_S = 20.0  # GLM L2 files are published every 20 s per satellite
_M_PER_NM = 1852.0


def _sat_num(sat: str) -> str:
    return sat.upper().removeprefix("G").removeprefix("OES-").removeprefix("OES")


def _get(url: str, timeout_s: float = 30.0) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "aether-glm-bench/1.0"})
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:  # noqa: S310 (https public S3)
        return bytes(resp.read())


def _list_keys(host: str, prefix: str) -> list[tuple[str, int]]:
    """Return (key, size) for a GLM hour prefix via the S3 list-objects-v2 XML API."""
    url = f"{host}/?list-type=2&prefix={prefix}&max-keys=400"
    body = _get(url).decode("utf-8", "replace")
    out: list[tuple[str, int]] = []
    # Minimal XML scrape (avoids an xml dep): pull <Key>/<Size> pairs in document order.
    for chunk in body.split("<Contents>")[1:]:
        key = chunk.split("<Key>")[1].split("</Key>")[0]
        size = int(chunk.split("<Size>")[1].split("</Size>")[0])
        out.append((key, size))
    return out


def newest_keys(host: str, n: int) -> list[tuple[str, int]]:
    """Newest ``n`` GLM L2 keys, walking back hour-by-hour from now (UTC)."""
    now = datetime.now(UTC)
    collected: list[tuple[str, int]] = []
    for back in range(6):  # look back up to 6 hours to gather enough files
        t = now - timedelta(hours=back)
        prefix = f"{PRODUCT}/{t.year}/{t.timetuple().tm_yday:03d}/{t.hour:02d}/"
        keys = _list_keys(host, prefix)
        collected = keys + collected  # older hours prepend
        if len([k for k in collected if True]) >= n and back >= 0 and keys:
            # keep walking only if the current (newest) hour was thin
            if len(keys) >= n or back > 0:
                break
    return collected[-n:]


def haversine_nm(lat1: float, lon1: float, lats, lons):  # type: ignore[no-untyped-def]
    """Vectorized great-circle distance (nautical miles) from a point to numpy arrays."""
    import numpy as np

    r_nm = 6371000.0 / _M_PER_NM
    p1 = math.radians(lat1)
    p2 = np.radians(lats)
    dphi = np.radians(lats - lat1)
    dlmb = np.radians(lons - lon1)
    a = np.sin(dphi / 2) ** 2 + math.cos(p1) * np.cos(p2) * np.sin(dlmb / 2) ** 2
    return 2 * r_nm * np.arcsin(np.sqrt(a))


def main() -> int:
    ap = argparse.ArgumentParser(description="NOAA GLM L2 Pi ingestion benchmark")
    ap.add_argument("--sat", default="G19", help="GOES satellite (G19=East default, G18=West)")
    ap.add_argument("--files", type=int, default=12, help="number of newest files to fetch")
    ap.add_argument("--lat", type=float, default=39.0, help="AOI center latitude")
    ap.add_argument("--lon", type=float, default=-98.0, help="AOI center longitude")
    ap.add_argument("--radius-nm", type=float, default=500.0, help="AOI radius (NM)")
    args = ap.parse_args()

    try:
        import netCDF4  # noqa: F401
        import numpy as np
    except ImportError:
        print("netCDF4/numpy not installed — `pip install netCDF4` (the optional dep).")
        return 2

    host = S3_HOST.format(sat_num=_sat_num(args.sat))
    print(f"# GLM benchmark  sat={args.sat}  host={host}")
    print(f"# AOI center=({args.lat},{args.lon})  radius={args.radius_nm} NM  files={args.files}")

    t0 = time.monotonic()
    keys = newest_keys(host, args.files)
    list_dt = time.monotonic() - t0
    if not keys:
        print("No GLM keys found — feed may be delayed.")
        return 1
    print(f"# listed {len(keys)} keys in {list_dt:.2f}s\n")

    widths = (
        ("file", 3),
        ("KB", 6),
        ("dl_s", 6),
        ("parse_s", 7),
        ("filt_s", 6),
        ("flashes", 7),
        ("in_aoi", 6),
    )
    hdr = "  ".join(f"{name:>{w}}" for name, w in widths)
    print(hdr)
    print("-" * len(hdr))

    dl_t, parse_t, filt_t, sizes, flashes_all, in_aoi_all = [], [], [], [], [], []
    for i, (key, size) in enumerate(keys):
        url = f"{host}/{key}"
        a = time.monotonic()
        raw = _get(url)
        b = time.monotonic()
        ds = netCDF4.Dataset("inmem", memory=raw)  # parse from RAM — no disk (LIGHTNING-FR-005)
        lat = np.asarray(ds.variables["flash_lat"][:], dtype="float64")
        lon = np.asarray(ds.variables["flash_lon"][:], dtype="float64")
        _energy = np.asarray(ds.variables["flash_energy"][:], dtype="float64")  # scaled by netCDF4
        n_flashes = int(lat.shape[0])
        ds.close()
        c = time.monotonic()
        if n_flashes:
            d_nm = haversine_nm(args.lat, args.lon, lat, lon)
            in_aoi = int(np.count_nonzero(d_nm <= args.radius_nm))
        else:
            in_aoi = 0
        d = time.monotonic()

        dl_t.append(b - a)
        parse_t.append(c - b)
        filt_t.append(d - c)
        sizes.append(size / 1024)
        flashes_all.append(n_flashes)
        in_aoi_all.append(in_aoi)
        print(
            f"{i:>3}  {size / 1024:>6.0f}  {b - a:>6.3f}  {c - b:>7.3f}  "
            f"{d - c:>6.3f}  {n_flashes:>7}  {in_aoi:>6}"
        )

    def mean(xs: list[float]) -> float:
        return sum(xs) / len(xs) if xs else 0.0

    peak_rss_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024  # KB→MB on Linux
    per_file_wall = mean(dl_t) + mean(parse_t) + mean(filt_t)
    daily_dl_gb = mean(sizes) * (86400 / FILE_CADENCE_S) / (1024 * 1024)

    print("\n# ---- summary (means) ----")
    print(f"file size        : {mean(sizes):8.0f} KB")
    print(f"download         : {mean(dl_t):8.3f} s")
    print(f"parse (netCDF4)  : {mean(parse_t):8.3f} s")
    print(f"AOI filter       : {mean(filt_t):8.3f} s")
    print(f"per-file wall    : {per_file_wall:8.3f} s   (budget {FILE_CADENCE_S:.0f}s/file)")
    print(f"flashes/file     : {mean([float(x) for x in flashes_all]):8.0f}")
    print(f"in-AOI/file      : {mean([float(x) for x in in_aoi_all]):8.0f}")
    print(f"peak RSS         : {peak_rss_mb:8.0f} MB  (process incl. numpy/netCDF4)")
    print(f"projected DL/day : {daily_dl_gb:8.2f} GB  (one satellite, continuous)")

    margin = FILE_CADENCE_S / per_file_wall if per_file_wall else float("inf")
    print(
        f"\n# real-time margin: {margin:.1f}x  "
        f"({FILE_CADENCE_S:.0f}s cadence / {per_file_wall:.2f}s work)"
    )
    if margin >= 4.0 and peak_rss_mb < 1024:
        print("# VERDICT: ACCEPTABLE — comfortable real-time margin and bounded memory.")
        return 0
    if margin >= 1.5:
        print("# VERDICT: MARGINAL — keeps up but with limited headroom; see notes.")
        return 0
    print("# VERDICT: NOT VIABLE — cannot sustain the 20s cadence on this hardware.")
    return 1


if __name__ == "__main__":
    sys.exit(main())

# NOAA GLM lightning — Pi resource benchmark (M5.6 gate)

> PRD §11.10 **LIGHTNING-FR-002**: the open lightning baseline uses NOAA GOES GLM Level-2
> data *only when the Pi resource benchmark is acceptable*. This is that benchmark. It must
> pass before the GLM adapter is enabled for continuous operation (PRD §18.7, §32 M5).

## Why a gate exists

GLM Level-2 (LCFA) is a **whole-disk** product published every **20 seconds** per satellite.
There is no server-side area filter — each file must be downloaded and parsed in full before
out-of-AOI flashes can be discarded (LIGHTNING-FR-005). Run forever, that download → parse →
AOI-filter → discard loop is a standing CPU / memory / network cost on a Raspberry Pi 5, and
GLM was always the milestone's "benchmark if viable, sequence last" source (the NetCDF parser
in particular was the open risk — PRD §2974).

## Method

`scripts/bench_glm.py` measures the real path against live NOAA GOES Open Data on AWS
(`noaa-goes19.s3.amazonaws.com`, anonymous HTTPS, PRD §38). For each of the *N* newest GLM L2
files it times the download, the in-memory NetCDF parse (`netCDF4`, no disk — the adapter's
intended path), and a vectorized AOI haversine filter, and it samples process peak RSS. The
verdict compares mean per-file wall time against the 20 s file cadence (the real-time budget).

```bash
pip install netCDF4                  # optional dep; the gate measures its cost too
python scripts/bench_glm.py --files 24
```

## Results — Raspberry Pi 5 (4 cores, 8 GB), 2026-06-21

GOES-19 (GOES-East), AOI center 39 N / 98 W, 500 NM radius, 24 newest files:

| Metric | Value |
| --- | --- |
| File size (mean) | ~385 KB |
| Download (mean) | 0.13 s |
| Parse — netCDF4, in-memory (mean) | **0.010 s** |
| AOI filter (mean) | <0.001 s |
| Per-file wall (mean) | **0.14 s** |
| Flashes per file (mean) | ~308 (whole disk) |
| In-AOI flashes per file (500 NM) | ~35 |
| Peak RSS (process incl. numpy/netCDF4) | **59 MB** |
| Projected download, continuous | **~1.6 GB/day** (one satellite) |
| Real-time margin | **~146×** (20 s cadence ÷ 0.14 s work) |

Dependency install (`netCDF4` 1.7.4 + numpy + cftime) used a prebuilt `aarch64` wheel from
piwheels — ~28 MB, no compilation, no system libraries.

## Verdict: **ACCEPTABLE** — GLM L2 is viable on the Pi 5

CPU and parser memory are negligible (146× real-time headroom, 59 MB RSS). The NetCDF parse —
the original risk — is ~10 ms per file. The only material cost is **network bandwidth: ~1.6 GB/day**
per satellite for continuous, complete coverage (there is no way to reduce it server-side short
of sampling files and accepting gaps). Because the adapter is **opt-in / off by default** (like
FIRMS), an operator who enables it accepts that cost knowingly.

### Consequences for the adapter

- **`netCDF4` is an optional dependency** (`pip install "aether[lightning]"`). Missing → the
  adapter reports one `offline` source status and exits cleanly; the app never crashes
  (capability-gated, the FIRMS-map-key stance).
- Default poll cadence 60 s, fetching every new file since the last poll (complete coverage),
  with a per-poll file cap so a reconnect after an outage catches up to *live* rather than
  replaying hours of backlog (LIGHTNING-FR-005 "only the newest required").
- Flashes are transient and high-volume, so each carries a short `valid_until` TTL and ages
  off the map via the live-state expiry sweep (bounded memory during an active storm).
- Honest labeling (LIGHTNING-FR-003/004): GLM reports **total-lightning flashes**, never
  "confirmed cloud-to-ground strikes".

Re-run the benchmark before enabling GLM on materially different hardware.

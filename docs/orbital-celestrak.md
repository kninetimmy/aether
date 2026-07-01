# CelesTrak orbital tracking (M6.5)

> PRD §11.14 / §18.12: propagate published orbital element sets to plot overhead objects on
> the COP. This is **tracking** — propagating CelesTrak's public elements to a position — not
> satellite *reception* (PRD §5 settled decision; aether never receives a downlink). Positions
> are **predicted** and labelled as such; the product is **not for navigation or operational
> use**.

## What it does

The adapter (`src/aether/adapters/celestrak.py`) runs three cadences over one provider — one
sync and **two** propagate tiers (ORBIT-FR-011), plus a cheap watchlist re-read:

1. **Sync** the General Perturbations (GP) element sets for the configured groups, then
2. **Propagate** the synced set with **SGP4** on **two tiers** over a disjoint partition: the
   operator's **watchlisted** objects ride a **fast** cadence (default 2 s) for smooth tracks,
   while the broad catalog rides the existing **slow** cadence (default 15 s). Each tick emits
   one `orbital_object` `TrackRecord` per object currently above the observer's horizon.

### Two-tier propagation (ORBIT-FR-011)

The `active` group is large and Pi-heavy to propagate, so propagating *everything* on a fast
cadence is forbidden. Instead the synced catalog is split by NORAD id into two **disjoint**
lists each tick:

- **Fast tier** — the objects whose `orbital:celestrak:<norad>` key is on the operator's
  persisted watchlist. Propagated every `celestrak_propagate_fast_s` (default 2 s) so the few
  objects the operator cares about move smoothly.
- **Slow tier** — the rest of the catalog. Propagated every `celestrak_propagate_s` (default
  15 s, now the **slow** tier) and the tier that emits the single `connected` source status.

Because the split is a strict set difference, a watchlisted object is propagated and emitted by
the fast tier **only** — never double-emitted by the slow tier (the no-double-emit guarantee is
structural, independent of timing). The watchlist is re-read every
`celestrak_watchlist_refresh_s` (default 30 s), so toggling a satellite in the UI moves it
between tiers **with no adapter restart**. The fast tier changes only the *propagate* cadence
(local CPU); it never touches the 6 h fetch/sync cadence (§38 rate limit).

The two-tier path is gated on `persist=True` **and** a non-empty orbital watchlist: with
persistence off (or an empty watchlist) the fast tier collapses and the behaviour is identical
to the single-cadence path — zero watchlist I/O, byte-identical output. The `connected` status
surfaces `watchlisted` (orbital keys read from the DB), `fast_tracked` (watchlist ∩ synced
catalog), `slow_tracked`, and `fast_above_horizon`. A watchlisted satellite must be in a synced
group to be propagated, so `watchlisted > fast_tracked` honestly surfaces the gap when an
operator watchlists an object whose group is not synced.

The sub-satellite point becomes the map `Point` and `altitude_m`; the observer-relative
azimuth / elevation / slant range and the element-set epoch + age ride in `attributes`
(the schema is `extra="forbid"`, so there are **no new top-level fields** and **no
`schema_version` bump** — the maintainer-approved approach). The coordinate transforms
(`src/aether/orbital/transforms.py`) are pure-Python and unit-tested against published Vallado
reference values (GMST82 Example 3-5; TEME→ECEF Example 3-15) to sub-degree az/el.

### Pass prediction — rise / culmination / set (M6.8, PRD §32 #18/#19)

The tick-by-tick propagate above only ever resolves a position at a single instant; it cannot
say *when* a pass begins, peaks, or ends. `src/aether/orbital/pass_prediction.py`
(`predict_next_pass`) answers that separately: given a `Satrec` + observer + a start time, it
scans forward (default a 24 h window) for the next (or already in-progress) pass above the
elevation floor and returns the rise / culmination / set instants and the peak elevation. This
is the M6 exit criterion ("watched satellite passes are predicted and alerted", PRD §32) and the
input to the `culmination_reached` alert operator (#18) and the satellite-pass-end template
(#19).

A prediction is attached to a `TrackRecord`'s `attributes` **only on the fast (watchlisted)
tier** — never the broad catalog, which would be far too costly to predict-ahead for every
object every tick:

| Attribute | Type | Present when |
| --- | --- | --- |
| `pass_culmination_at` | ISO-8601 UTC `str` | a prediction exists (always paired with `pass_max_elevation_deg`) |
| `pass_max_elevation_deg` | `float` (deg) | a prediction exists |
| `pass_rise_at` | ISO-8601 UTC `str` | the rise crossing falls inside the search window (omitted for an already in-progress pass) |
| `pass_set_at` | ISO-8601 UTC `str` | the set crossing falls inside the search window (omitted for an object that never drops back below the floor, e.g. geostationary) |

When `predict_next_pass` finds no pass at all (object never clears the floor, or has decayed —
`propagate()` fails mid-scan), **all four keys are omitted** — never null/placeholder values
(honest-unevaluable, §37). `element_to_record`'s new `prediction` parameter defaults to `None`,
so the slow tier and every pre-M6.8 caller/test/bench emit byte-identical records.

**Cache + recompute strategy.** `celestrak_records` keeps a NORAD-id-keyed
`pass_cache`/`pass_retry_at` pair in the generator's loop scope (same lifetime as the watchlist
partition — naturally rebuilt on reconnect). Each fast tick, `_update_pass_cache` recomputes **at
most one** watchlisted object's prediction (never the whole watchlist in the same tick, even
right after a resync invalidates every entry — the watchlist's predictions ramp up over a few
ticks instead) when:

- the object has never been predicted yet, or
- the cached prediction is `None` (decayed object, or genuinely no pass in the window) and at
  least `PASS_PREDICT_RETRY_S` (60 s) has elapsed since the last attempt — re-running a ~24 h
  scan on every 2 s tick for a hopeless object would be wasteful, or
- the cached pass has a `set_at` **and** wall-clock has passed it **and** the object's *current*
  elevation (one extra cheap `propagate()` call) has actually dropped below the floor. Gating on
  the real floor crossing rather than purely on the predicted `set_at` closes a race: if the
  prediction's `set_at` is a few seconds optimistic, a `now >= set_at`-only gate would silently
  swap in next-pass data while the satellite is still above the floor mid-pass.

The first two conditions above ("never predicted" and "cached pass has actually set") are
**urgent** and win the single per-tick slot immediately, in list order; the `None`-retry
condition is a lower-priority fallback, only taken when nothing urgent needs the slot this tick
(M6.8 fix pass). Without this split, a watchlist heavy in objects that genuinely never pass
(cached `None`, retried forever on `PASS_PREDICT_RETRY_S`) could starve a *different*, real
object's actual-set recompute out of the slot for its whole below-floor inter-pass gap — by the
time that object rose again for its next pass, the actual-floor-crossing gate could no longer
fire (the object is back above the floor), leaving the cache silently serving the *previous*
pass's rise/culmination/set for the entire new pass.

`pass_cache`/`pass_retry_at` are also **pruned** to just the current fast-tier NORAD ids every
time the fast/slow partition is recomputed (after a sync or a watchlist refresh) — a
de-watchlisted object's entry does not linger for the rest of the connection's lifetime (bounded
maps, §37); a later re-watchlisted object gets a fresh cold-cache recompute rather than
resurrecting a stale one.

`predict_next_pass` runs **inline** on the event loop (not `asyncio.to_thread`): a coarse scan is
on the order of a few thousand `propagate()` calls, and the existing slow-tier benchmark (below)
shows propagating the *entire* multi-thousand-object `active` catalog once takes ~0.5 s on a
Pi 5 — a single-object 24 h scan is a small fraction of that, comfortably inside the 2 s fast-tier
budget. If a real Pi-5 `--pass-predict` measurement (see below) ever shows a single recompute
approaching that budget, wrap the `predict_next_pass` call in `await asyncio.to_thread(...)`
(the same pattern already used for the watchlist read in `run_celestrak`).

#### Pass-prediction bench mode

`scripts/bench_celestrak.py --pass-predict` times one full `predict_next_pass` recompute for an
ISS-class object (NORAD 25544, falling back to the first element of the fetched group) over
`--iters` calls and reports the mean wall time and margin against the 2 s fast-tier budget:

```bash
python scripts/bench_celestrak.py --group stations --pass-predict --iters 10
```

> **Bench-mode status:** **not yet measured on a Pi 5.** The script's pre-existing RSS-reporting
> path used a module-level `import resource` (Linux/POSIX-only), which broke the whole script
> — including this brand-new `--pass-predict` mode, which never touches `resource` — on
> Windows; that import is now lazy and RSS reporting degrades to `n/a` when unavailable rather
> than crashing the script (M6.8 fix pass). With that fixed, one real (non-Pi-5) measurement was
> taken on the Windows development machine this slice was authored on:
>
> ```
> $ python scripts/bench_celestrak.py --group stations --pass-predict --iters 5 --lat 30 --lon -97
> # pass-prediction bench: NORAD 25544 (ISS (ZARYA))
> recompute/call   :    0.025 s   (budget 2s/fast tick)
> real-time margin: 80.2x
> # VERDICT: ACCEPTABLE — comfortable margin to stay inline on the event loop.
> ```
>
> **(dev machine, not Pi-5)** — labelled per the honest-labeling stance (§37); a Pi 5 is
> expected to be slower than this development machine, so this number is directionally
> reassuring (comfortably inside the 2 s fast-tier budget with an 80x margin) but is **not** a
> substitute for a real Pi-5 run. Re-run the command above on the target Pi 5 and replace this
> entry before treating the inline (non-`to_thread`) call above as hardware-validated.

## Endpoint, format, and rate limits (PRD §38)

```
GET https://celestrak.org/NORAD/elements/gp.php?GROUP=<slug>&FORMAT=json
```

- **`FORMAT=json` is sent explicitly.** The service default changed to CSV on 2026-05-09; the
  adapter never relies on the default.
- The JSON body is an array of OMM objects (`OBJECT_NAME`, `OBJECT_ID`, `EPOCH`, the six
  Keplerian/derived elements, `NORAD_CAT_ID`, `BSTAR`, …) which `sgp4.omm.initialize` turns
  into a `Satrec`.
- **Rate limits are hard.** CelesTrak refreshes elements only ~every 2 h, so the default
  **sync cadence is 6 h** — well within the limit. On HTTP **301 / 403 / 404** the request is
  **abandoned** (the response will not change; ~50 such errors in 2 h firewalls the source IP)
  and the **last-good cache** is served — the adapter never tight-loops. The canonical `https`
  host is used and redirects are followed in-client.

Default groups: `stations`, `active`, `amateur` (operator-configurable via
`AETHER_CELESTRAK_GROUPS`). The `active` group is large; propagating it on a fast cadence is
Pi-heavy, which is exactly why ORBIT-FR-011 keeps it on the **slow** tier while only the
watchlisted handful rides the fast tier (see "Two-tier propagation" above). If `active` is too
heavy on your Pi, trim the groups or raise `AETHER_CELESTRAK_PROPAGATE_S`.

### Pi-5 SGP4 benchmark

`scripts/bench_celestrak.py` fetches the live `active` group once and times one slow-tier tick
(propagate the whole group + elevation-filter) repeatedly, reporting build cost, mean tick wall
time, above-horizon count, peak RSS, and a verdict vs the 15 s slow-tier budget — the GLM
benchmark-gate pattern. SGP4's scalar path needs no numpy and its RSS is far below the 1024 MB
bound, so the real gate is the real-time margin. Run it on the target Pi 5 and record the
verdict here:

```bash
python scripts/bench_celestrak.py --group active --iters 10
```

> **Benchmark verdict (Pi 5, sgp4 2.25, 2026-06-28): `ACCEPTABLE`.** Fetched 15 894 OMM rows
> from the live `active` group; built 15 894 Satrecs in 0.40 s (one-time per 6 h sync); a full
> slow-tier tick (propagate the whole catalog + elevation-filter) averaged **0.52 s** over 10
> iters with ~524 objects above a 10° horizon, peak RSS **84 MB**. That is a **28.8x** real-time
> margin against the 15 s slow-tier budget (`>= 4x` ⇒ ACCEPTABLE) and far below the 1024 MB
> bound — the heavy catalog tick is comfortably within the Pi 5's budget, and the fast tier (a
> handful of watchlisted objects) is negligible on top.

## Capability gate — the `[orbital]` extra

SGP4 is the optional `sgp4` dependency, imported **lazily** inside the live propagator. With
it absent the adapter publishes exactly one `offline` source status (`Sgp4Unavailable`,
`pip install "aether[orbital]"`) and exits cleanly — the app and every other adapter keep
running (the same stance as the GLM `netCDF4` parser, PRD §2/§37). Install it with:

```bash
pip install -e ".[orbital]"      # or: pip install "sgp4>=2.25"
```

The scalar `Satrec.sgp4` path needs **no numpy**.

## Honest labeling (PRD §37)

- Every record is `predicted=True` and `locally_received=False` (network-derived elements,
  propagated locally; provenance is `derived=True`, `confidence="medium"`).
- Each carries `attribution = "Orbital data: CelesTrak (celestrak.org)"` and
  `caveat = "Predicted SGP4 position; not for navigation or operational use."`.
- The element-set **epoch** and its **age in seconds** are surfaced in `attributes` so a stale
  element set is visible rather than silently trusted.
- An object whose SGP4 propagation errors (`e != 0`, e.g. decayed) or returns a NaN position is
  **skipped**, never plotted at a bad position.
- A pass prediction (M6.8) is itself SGP4-predicted, not observed, and degrades accuracy with
  element-set age exactly like the instantaneous position does; when no pass can be predicted
  (decayed object, or genuinely never above the floor in the search window) the `pass_*`
  attributes are omitted entirely — never a fabricated rise/culmination/set time.

## Run it with no hardware and no network (PRD §6 / §34)

The fake feeder (`src/aether/adapters/celestrak_fake_feeder.py`) ships canned OMM — a real ISS
(NORAD 25544) element set plus two synthetic geostationary objects, one solved to sit above the
configured observer and one ~150° away (below the horizon) — and drives the **real** SGP4
propagate path, so the full chain (adapter → bus → state → ws → UI) runs offline.

```bash
docker compose up -d                                  # local MQTT broker
AETHER_DEMO_SOURCE=0 \
AETHER_CELESTRAK=1 AETHER_CELESTRAK_BASE_URL=fake \
AETHER_STATION_LAT=30 AETHER_STATION_LON=-97 \
uvicorn aether.backend.main:app --host 127.0.0.1 --port 8000
curl -s localhost:8000/api/state | python -m json.tool   # an orbital_object track, predicted
```

## Configuration (`AETHER_CELESTRAK*`)

| Env var | Default | Meaning |
| --- | --- | --- |
| `AETHER_CELESTRAK` | `0` | Enable the adapter. |
| `AETHER_CELESTRAK_BASE_URL` | `https://celestrak.org` | GP host; `fake` selects the feeder. |
| `AETHER_CELESTRAK_GROUPS` | `stations,active,amateur` | CSV of GP group slugs. |
| `AETHER_CELESTRAK_LAT` / `_LON` | station location | Observer for look angles. |
| `AETHER_CELESTRAK_ALT_M` | `0` | Observer altitude above the WGS-84 ellipsoid (m). |
| `AETHER_CELESTRAK_MIN_ELEVATION_DEG` | `10` | Emit only objects above this elevation. |
| `AETHER_CELESTRAK_SYNC_S` | `21600` (6 h) | GP sync cadence (≥ 2 h per §38). |
| `AETHER_CELESTRAK_PROPAGATE_S` | `15` | **Slow** tier: broad-catalog propagation cadence. |
| `AETHER_CELESTRAK_PROPAGATE_FAST_S` | `2` | **Fast** tier: watchlisted-object propagation cadence (ORBIT-FR-011; local CPU, never the fetch cadence). |
| `AETHER_CELESTRAK_WATCHLIST_REFRESH_S` | `30` | How often the persisted watchlist is re-read so a toggle moves an object between tiers without a restart. |
| `AETHER_CELESTRAK_VALID_S` | `30` | On-map freshness of a propagated position. |
| `AETHER_CELESTRAK_TIMEOUT_S` | `15` | Per-request HTTP timeout. |

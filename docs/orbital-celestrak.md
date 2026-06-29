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

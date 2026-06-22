# CelesTrak orbital tracking (M6.5)

> PRD §11.14 / §18.12: propagate published orbital element sets to plot overhead objects on
> the COP. This is **tracking** — propagating CelesTrak's public elements to a position — not
> satellite *reception* (PRD §5 settled decision; aether never receives a downlink). Positions
> are **predicted** and labelled as such; the product is **not for navigation or operational
> use**.

## What it does

The adapter (`src/aether/adapters/celestrak.py`) runs two cadences over one provider:

1. **Sync** the General Perturbations (GP) element sets for the configured groups, then
2. **Propagate** the whole synced set with **SGP4** every few seconds and emit one
   `orbital_object` `TrackRecord` per object currently above the observer's horizon.

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
Pi-heavy. M6.5 propagates the synced set on one conservative cadence and filters by elevation;
the full multi-tier propagation cadence (ORBIT-FR-011) is **deferred to M6.6**. If `active` is
too heavy on your Pi, trim the groups or raise `AETHER_CELESTRAK_PROPAGATE_S`.

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
| `AETHER_CELESTRAK_PROPAGATE_S` | `15` | Propagation cadence. |
| `AETHER_CELESTRAK_VALID_S` | `30` | On-map freshness of a propagated position. |
| `AETHER_CELESTRAK_TIMEOUT_S` | `15` | Per-request HTTP timeout. |

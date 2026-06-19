# aether

A private, local-first, browser-based **common operating picture (COP)** for a Raspberry Pi 5 home
station. Local SDR receivers are the trusted first-party sources; on top of them, aether fuses open
Internet feeds into one time-aware map where every record carries **provenance** — so the operator always
knows what their own radios received versus what only an Internet feed reported.

aether is a hobbyist situational-awareness tool. It is **not** an authoritative aviation, maritime,
emergency-management, orbital, weather, or navigation product, and it makes no operational decisions.

> **Authority:** [`PRD.md`](PRD.md) is the product + architecture specification. [`CLAUDE.md`](CLAUDE.md) /
> [`AGENTS.md`](AGENTS.md) is the lean operating guide for AI coding agents. When anything conflicts, the
> PRD wins; code is ground truth for current state.

---

## What it does

aether runs continuously on a Pi 5 with two dedicated SDR receivers and fuses local radio observations with
open Internet data into one coherent, dark, high-density map. Sources, in roadmap order:

- **Local SDR (receive-only RF):** 1090 MHz ADS-B/Mode-S via `readsb`; 144.39 MHz APRS via Dire Wolf.
- **Network feeds:** wider-area ADS-B from an open provider (adsb.fi), APRS-IS, AIS vessels (AISStream), and
  — planned for later milestones — SondeHub radiosondes, lightning, NASA FIRMS active-fire detections, USGS
  earthquakes, FAA TFR/NOTAM, and CelesTrak orbital data with locally propagated overhead-object positions
  and pass predictions.
- **Operator layers:** filters, tracks of interest (TOI) watchlist, and — planned — geofences, alerts,
  history, and replay.

The default area of interest is a **500-nautical-mile radius** around the configured home station,
operator-adjustable. Local and Internet observations of the same identity **fuse into one** record via
strict identity keys (ICAO hex for aircraft, callsign/object for APRS, MMSI for vessels), local RF
privileged. The UI can always collapse to local-only.

### Receive-only, private by default

The APRS station operates as a **receive-only RF iGate**: valid RF packets may be gated *to* APRS-IS, but
the system never transmits, beacons, digipeats, acks over RF, or creates an Internet-to-RF path. Every
Internet feed is a read-only subscription (the APRS-IS login uses passcode `-1`, which cannot inject
packets). The app and broker bind loopback; remote access is via **Tailscale Serve only, never Funnel**.
The repo carries no secrets, callsign, or station coordinates — each operator supplies their own config.

---

## Status

Milestones **M1 (COP core) → M3 (network fusion)** are complete, and **M4 (alerts & history)** is in
progress: SQLite persistence with track history and a retention manager have landed; the alert engine and
replay are next. Live state is served from memory — persistence is a sibling consumer that never gates it.
Working today:

**Core**
- **Schema v2** — a Pydantic v2 discriminated record union (track / geo-feature / event / alert /
  source-status) with provenance, correlation keys, and observed/received/published timestamps.
- **MQTT bus** — records flow over `aether/v2/...` topics (Mosquitto).
- **FastAPI backend** — in-memory live state at `/api/state`, runtime config at `/api/config`, current track
  detail at `/api/v2/tracks/{id}`, persisted track history at `/api/v2/tracks/{id}/history`, and a
  sequence-numbered websocket `/ws/v2` (snapshot then deltas, with gap detection and resync). Clients can
  `subscribe` to narrow the stream to a viewport bbox, source set, and track types (server-side filtering).
- **Fusion engine** — local and Internet observations of one identity collapse into a single track via
  strict identity keys, with source precedence, per-contributor freshness, and stale-track expiry.
- **MapLibre frontend** — React + Vite + TypeScript COP shell rendering the full record union live through
  a centralized presentation registry: dark map, layer control, display filters, source-health panel, track
  list, event/alert feed, and a TOI watchlist with a details panel that honestly distinguishes "live
  locally" from "last heard locally."

**Sources** (each ships a fake feeder, so the full path runs with no radios or live APIs)
- **Local ADS-B** — `readsb` `aircraft.json` snapshot adapter, with emergency-squawk (7500/7600/7700) events.
- **Local APRS** — Dire Wolf KISS adapter, receive-only iGate config.
- **Network ADS-B** — adsb.fi provider with 500 NM AOI tiling, fused with local ADS-B by ICAO hex.
- **APRS-IS** — Internet APRS display feed, fused with local APRS by callsign/object identity.
- **AIS vessels** — AISStream.io secure-WebSocket feed, merged per MMSI.
- **Military Mode-S classification** — an honest, two-basis (provider DB flag + operator-supplied ICAO
  address blocks) classifier shared by both ADS-B adapters; never stated as certain.

**Persistence** (M4, opt-in via `AETHER_PERSIST=1`; never gates serving live state)
- **Track history** — fused track observations written to SQLite (WAL, versioned migrations) by an
  independent bus consumer with a bounded write queue that drops rather than back-pressures the bus.
- **Retention manager** — enforces the 30-day window and the `AETHER_DB_MAX_GB` / `AETHER_MIN_FREE_DISK_GB`
  limits via a storage-pressure ladder (downsample → delete oldest → shorten retention → VACUUM), emitting a
  health warning + system event under pressure. Disk limits override time retention.
- **History read API** — `GET /api/v2/tracks/{id}/history` returns a track's persisted observations
  (oldest-first, optional `start`/`end` window, `limit`-capped with a `truncated` flag), served on a fresh
  read-only connection so the read path never gates serving live state.

See the milestone roadmap (M0–M7) in [`CLAUDE.md`](CLAUDE.md) §4 and the exit criteria in `PRD.md` §32–33.

---

## Quick start (no hardware required)

**Backend** — needs Python 3.11+ and Docker (for the Mosquitto broker):

```bash
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\Activate.ps1
pip install -e ".[dev]"
docker compose up -d                                          # local MQTT broker
uvicorn aether.backend.main:app --host 127.0.0.1 --port 8000  # backend + in-process demo source
```

Then check the state and stream:

```bash
curl -s localhost:8000/api/state | python -m json.tool       # mixed records from the demo source
```

The in-process **demo source** publishes a simulated mix of every record type so the COP renders with no
radios. A real deployment sets `AETHER_DEMO_SOURCE=0` and runs source adapters instead (below).

**Frontend** — needs Node:

```bash
cd frontend
npm install
npm run dev                                                   # Vite dev server, connects to /ws/v2
```

---

## Run a source (still no hardware needed)

Every source ships a **fake feeder** so its full path (adapter → bus → state → websocket → UI) runs with no
SDR and no live API. Turn off the demo source (`AETHER_DEMO_SOURCE=0`), start a feeder, and enable the
adapter. Run two or more together and aether **fuses** duplicate observations into one track with the
correct provenance.

### Local ADS-B (readsb)

```bash
python -m aether.adapters.readsb_fake_feeder /tmp/aircraft.json &              # writes a data file only
AETHER_DEMO_SOURCE=0 AETHER_LOCAL_ADSB=1 \
    AETHER_LOCAL_ADSB_SOURCE=/tmp/aircraft.json \
    uvicorn aether.backend.main:app --app-dir src
```

A real deployment points `AETHER_LOCAL_ADSB_SOURCE` at your readsb/tar1090 feed (default
`http://127.0.0.1:8080/data/aircraft.json`).

### Local APRS (Dire Wolf)

```bash
python -m aether.adapters.aprs_fake_feeder 127.0.0.1 8001 &                    # fake KISS frames only; never transmits
AETHER_DEMO_SOURCE=0 AETHER_LOCAL_APRS=1 \
    AETHER_LOCAL_APRS_HOST=127.0.0.1 AETHER_LOCAL_APRS_PORT=8001 \
    uvicorn aether.backend.main:app --app-dir src
```

The APRS station is **receive-only**: see [`docs/local-aprs-igate.md`](docs/local-aprs-igate.md) and the
sample [`config/direwolf.conf.example`](config/direwolf.conf.example) for the real-radio setup and the
no-transmit guardrail.

### Network ADS-B + fusion

The network adapter sweeps the AOI from an open provider (default `adsb.fi`); the `fake` provider stands in
for the no-hardware path. Run it **alongside** local ADS-B and the same airframe seen by both fuses into one
track — local RF privileged, the Internet feed filling the gaps:

```bash
python -m aether.adapters.readsb_fake_feeder /tmp/aircraft.json &              # the local leg
AETHER_DEMO_SOURCE=0 \
    AETHER_LOCAL_ADSB=1 AETHER_LOCAL_ADSB_SOURCE=/tmp/aircraft.json \
    AETHER_NETWORK_ADSB=1 AETHER_NETWORK_ADSB_PROVIDER=fake \
    uvicorn aether.backend.main:app --app-dir src
```

A real deployment sets `AETHER_NETWORK_ADSB_PROVIDER=adsb.fi` and supplies the AOI center via the canonical
`AETHER_STATION_LAT` / `AETHER_STATION_LON` (see [Station location & AOI](#station-location--aoi)). A radius
above the provider's 250 NM per-query cap is tiled into compliant requests automatically, at a polite
~1 req/s. No API key is required for adsb.fi.

### APRS-IS (Internet APRS display)

The APRS-IS adapter reads network APRS traffic for the AOI and fuses it with local APRS by callsign/object
identity — so a station you hear on RF and via the Internet appears once. A fake APRS-IS server stands in
for the no-hardware path:

```bash
python -m aether.adapters.aprs_is_fake_feeder 127.0.0.1 14580 &               # canned TNC2 lines; receive-only
AETHER_DEMO_SOURCE=0 AETHER_APRS_IS=1 \
    AETHER_APRS_IS_HOST=127.0.0.1 AETHER_APRS_IS_PORT=14580 \
    AETHER_APRS_IS_CALLSIGN=N0CALL \
    AETHER_APRS_IS_LAT=38.5 AETHER_APRS_IS_LON=-74.5 \
    uvicorn aether.backend.main:app --app-dir src
```

A real deployment uses the defaults `AETHER_APRS_IS_HOST=rotate.aprs2.net` / `AETHER_APRS_IS_PORT=14580`,
sets `AETHER_APRS_IS_CALLSIGN` to your own callsign, leaves `AETHER_APRS_IS_PASSCODE=-1` (receive-only — the
login is the only thing aether ever sends, and `-1` cannot inject packets), and sets the AOI via
`AETHER_STATION_LAT` / `AETHER_STATION_LON`. An enabled-but-callsign-less adapter reports `offline`, never
connecting as the maintainer's identity. **Limitations:** APRS-IS positions are *reported broadcasts*, and
the same packet is relayed by multiple igates (the adapter de-dups within the classic ~30 s window).

### AIS vessels (AISStream)

The AIS adapter streams vessel tracks from [AISStream.io](https://aisstream.io) over a secure WebSocket,
subscribed to a bounding box around the AOI, merging each vessel's separate position and static/voyage
messages into one track by MMSI. A fake WebSocket feeder stands in for the no-hardware path (plain `ws`, no
key):

```bash
python -m aether.adapters.ais_fake_feeder 127.0.0.1 8765 &                     # canned vessel frames only
AETHER_DEMO_SOURCE=0 AETHER_AIS=1 AETHER_AIS_TLS=0 \
    AETHER_AIS_HOST=127.0.0.1 AETHER_AIS_PORT=8765 AETHER_AIS_API_KEY=demo \
    AETHER_AIS_LAT=38.5 AETHER_AIS_LON=-74.5 \
    uvicorn aether.backend.main:app --app-dir src
```

A real deployment leaves `AETHER_AIS_TLS=1` (the `wss://stream.aisstream.io` endpoint), supplies a free
[AISStream API key](#api-keys--credentials) via `AETHER_AIS_API_KEY`, and sets the AOI via
`AETHER_STATION_LAT` / `AETHER_STATION_LON`. An enabled-but-unconfigured adapter reports `offline`, never
connects anonymously, and never logs the key. **Limitations:** AIS positions are *reported broadcasts*, not
verified navigation truth; AISStream enforces per-key rate limits, so the adapter holds a single
subscription; this is a network-only feed, so every vessel is network provenance.

### Military Mode-S classification

This is not a runnable source — it's an honest classifier applied to **both** ADS-B adapters. Two bases (and
only two) may flag a track military, and neither is treated as certain:

- **provider** — the feed's own `dbFlags` military bit (adsb.fi / ADS-B-Exchange / readsb). Always on.
- **address_block** — the ICAO 24-bit address falls in an **operator-supplied** range. The repo ships the
  mechanism, not a baked-in table (a stale table could mislabel civil airframes). Supply verified ranges:

```bash
AETHER_MIL_ICAO_BLOCKS="adf7c8-afffff, 43c000-43cfff"   # comma-separated start-end hex pairs
```

Left empty, only the provider bit classifies. The UI never states military classification as certain.

---

## Station location & AOI

aether centers its area of interest on one home position, supplied by the operator — **the repo carries no
coordinates**. Set it once and every consumer uses it: the websocket default viewport, the frontend
range-from-station filter (served via `/api/config`), and the per-adapter AOI centers for network ADS-B,
APRS-IS, and AIS.

```bash
AETHER_STATION_LAT=38.5 AETHER_STATION_LON=-74.5 AETHER_STATION_RADIUS_NM=500
```

Left at the `0,0` default the station is treated as **unconfigured** and every consumer degrades *visibly*:
the websocket default viewport becomes unbounded (never a degenerate null-island box), the range filter
disables, and the AOI sweeps cover open ocean and find nothing — failing loudly rather than leaking a
location. A per-adapter override (e.g. `AETHER_AIS_LAT`/`_LON`) still wins for the rare multi-AOI setup.

---

## API keys & credentials

aether keeps **no** credentials in the repo. Each operator supplies their own; an enabled-but-unconfigured
source reports `offline` rather than failing silently or connecting anonymously.

| Source | Credential | Where to get it | Notes |
| --- | --- | --- | --- |
| Network ADS-B (adsb.fi) | none | — | Free open feed; aether tiles and rate-limits requests politely. `adsb.lol` / OpenSky are documented fallbacks. |
| APRS-IS | your callsign + passcode `-1` | use your own amateur-radio callsign | Passcode stays `-1` (receive-only; aether never transmits). The local RF iGate path requires a licensed callsign. |
| AIS (AISStream) | free API key | register at [aisstream.io](https://aisstream.io) and create a key | Free tier is rate-limited → single subscription. Key travels only in the subscription body and is never logged. |
| Military classification | operator-supplied ICAO ranges | your own verified allocation list | No account; set `AETHER_MIL_ICAO_BLOCKS`. Empty is fine (provider bit still classifies). |
| Local readsb / Dire Wolf | none | your own SDR + decoder software | Local-only; no Internet credential. |

Supply credentials via the environment (or a process manager / `.env` you do **not** commit). Provider
interfaces change — re-verify the references in `PRD.md` §38 at build time.

---

## Configuration reference

All settings are environment variables with safe loopback defaults; the no-hardware demo needs none.
Defaults shown in parentheses. Each source also accepts `*_POLL_S` / `*_THROTTLE_S` / `*_TIMEOUT_S` tuning
(see [`src/aether/config.py`](src/aether/config.py)).

| Variable | Default | Purpose |
| --- | --- | --- |
| `AETHER_MQTT_HOST` / `AETHER_MQTT_PORT` | `127.0.0.1` / `1883` | Mosquitto broker (loopback). |
| `AETHER_DEMO_SOURCE` | `1` | Run the in-process demo publisher. Set `0` for real adapters. |
| `AETHER_STATION_LAT` / `_LON` / `_RADIUS_NM` | `0.0` / `0.0` / `500` | Canonical home position + AOI radius (NM). `0,0` ⇒ unconfigured (degrades visibly). |
| `AETHER_LOCAL_ADSB` | `0` | Enable the local readsb adapter. |
| `AETHER_LOCAL_ADSB_SOURCE` | `http://127.0.0.1:8080/data/aircraft.json` | `aircraft.json` file path or URL. |
| `AETHER_LOCAL_APRS` | `0` | Enable the local Dire Wolf KISS adapter. |
| `AETHER_LOCAL_APRS_HOST` / `_PORT` | `127.0.0.1` / `8001` | Dire Wolf KISS endpoint (read-only). |
| `AETHER_NETWORK_ADSB` | `0` | Enable the network ADS-B adapter. |
| `AETHER_NETWORK_ADSB_PROVIDER` | `adsb.fi` | Provider, or `fake` for the no-hardware feeder. |
| `AETHER_NETWORK_ADSB_LAT` / `_LON` / `_RADIUS_NM` | station / station / `500` | AOI center + radius (default to the station). |
| `AETHER_MIL_ICAO_BLOCKS` | _(empty)_ | Comma-separated `start-end` military ICAO hex ranges. |
| `AETHER_APRS_IS` | `0` | Enable the APRS-IS display adapter. |
| `AETHER_APRS_IS_HOST` / `_PORT` | `rotate.aprs2.net` / `14580` | APRS-IS server (public infrastructure). |
| `AETHER_APRS_IS_CALLSIGN` | _(empty)_ | **Required** — your callsign. Empty ⇒ `offline`. |
| `AETHER_APRS_IS_PASSCODE` | `-1` | Receive-only login. aether never transmits regardless. |
| `AETHER_APRS_IS_LAT` / `_LON` / `_RADIUS_NM` | station / station / `500` | AOI server-side range filter. |
| `AETHER_AIS` | `0` | Enable the AISStream adapter. |
| `AETHER_AIS_API_KEY` | _(empty)_ | **Required** — your AISStream key. Empty ⇒ `offline`. |
| `AETHER_AIS_TLS` | `1` | `wss` (real). Set `0` only for the plain-`ws` fake feeder. |
| `AETHER_AIS_HOST` / `_PORT` / `_PATH` | `stream.aisstream.io` / `443` / `/v0/stream` | AISStream endpoint. |
| `AETHER_AIS_LAT` / `_LON` / `_RADIUS_NM` | station / station / `500` | AOI bounding box. |
| `AETHER_PERSIST` | `0` | Persist tracks to SQLite. A sibling consumer — never gates serving live state. |
| `AETHER_DB_PATH` | `aether.db` | SQLite store path (`-wal`/`-shm` sidecars sit alongside). |
| `AETHER_PERSIST_QUEUE_MAX` | `10000` | Bounded write queue; a full queue drops records, never back-pressures the bus. |
| `AETHER_RETENTION_DAYS` | `30` | Retention window. Observations older than this are deleted each sweep. |
| `AETHER_DB_MAX_GB` | `0` (off) | Size budget (DB + WAL). Over it, the storage-pressure ladder reclaims space. |
| `AETHER_MIN_FREE_DISK_GB` | `0` (off) | Free-disk floor. Crossing it is critical pressure; aether sheds its own oldest data. |
| `AETHER_RETENTION_INTERVAL_S` | `3600` | How often the retention sweep runs. |
| `AETHER_DB_HIGH_WATER` / `_CRITICAL_WATER` | `0.85` / `0.95` | Fractions of `DB_MAX_GB` where the ladder engages / escalates. |
| `AETHER_HISTORY_MAX_POINTS` | `10000` | Cap on points returned by one `/api/v2/tracks/{id}/history` request (response flags `truncated`). |

---

## Verify

The same gate CI runs, locally:

```bash
scripts/check.sh        # ruff lint + format check, mypy (strict), pytest
cd frontend && npm test # vitest
```

Every change is done only when its tests pass **and** the no-hardware path still renders.

---

## Stack

Python 3.11+ async · Pydantic v2 · Mosquitto (local MQTT) · FastAPI (REST + WebSocket) · `websockets` ·
React + Vite + TypeScript · MapLibre GL JS · Tailscale Serve for private access.

## Layout

```
src/aether/
  schema/      schema v2 record union, geometry, provenance, validation
  bus/         MQTT client, topics, no-hardware demo publisher
  state/       in-memory live state + sequence numbering
  fusion/      identity fusion engine, source precedence, freshness/expiry
  adapters/    local ADS-B (readsb) + APRS (Dire Wolf KISS); network ADS-B (adsb.fi)
               with AOI tiling; APRS-IS; AIS (AISStream); military classification;
               and a fake feeder for every source
  backend/     FastAPI app, /api/config, websocket hub + per-connection subscribe filtering
frontend/      React + Vite + TS COP shell (MapLibre): map, layer control, display filters,
               source health, track list, event/alert feed, TOI watchlist + details
config/        Dire Wolf receive-only iGate example
deploy/        Mosquitto broker config
docs/          local APRS iGate setup
scripts/       local check parity + git hooks
tests/         schema / parser / fusion / path tests
```

## License

[GPL-3.0](LICENSE). aether is a private, single-operator project; the repo is public but ships no operator
identity or credentials.
</content>
</invoke>

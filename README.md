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
- **Network feeds:** wider-area ADS-B from an open provider (adsb.fi), APRS-IS, and AIS vessels (AISStream).
- **Environmental layers:** USGS earthquakes, SondeHub radiosondes, NASA FIRMS active-fire detections, and
  NOAA GOES GLM lightning — plus earthquake and fire-detection alerts driven by the same alert engine.
- **Operator layers:** display filters, a tracks-of-interest (TOI) watchlist, geofences, an alert-rule
  engine with multi-channel notifications, persisted track history, and a replay timeline.
- **Planned (later milestones):** FAA TFR/NOTAM airspace, CelesTrak orbital data with locally propagated
  overhead-object positions and pass predictions, and map clustering for dense point layers.

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

Milestones **M1 (COP core) → M4 (alerts & history)** are complete, and **M5 (environmental layers)** is
nearly done — earthquakes, radiosondes, active-fire, and GLM lightning have all landed, along with
earthquake and fire-detection alerts; the last remaining M5 item is map clustering for dense point layers.
Live state is always served from memory — persistence is a sibling consumer that never gates it. Working
today:

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
  a centralized presentation registry: dark map (hosted CARTO dark basemap by default, with an offline
  fallback), layer control, display filters, source-health panel, track list, event/alert feed, and a TOI
  watchlist with a details panel that honestly distinguishes "live locally" from "last heard locally."

**Sources** (each ships a fake feeder, so the full path runs with no radios or live APIs)
- **Local ADS-B** — `readsb` `aircraft.json` snapshot adapter, with emergency-squawk (7500/7600/7700) events.
- **Local APRS** — Dire Wolf KISS adapter, receive-only iGate config.
- **Network ADS-B** — adsb.fi provider with 500 NM AOI tiling, fused with local ADS-B by ICAO hex.
- **APRS-IS** — Internet APRS display feed, fused with local APRS by callsign/object identity.
- **AIS vessels** — AISStream.io secure-WebSocket feed, merged per MMSI.
- **USGS earthquakes** — public-domain GeoJSON → earthquake geo-features (no key).
- **SondeHub radiosondes** — public REST telemetry → radiosonde tracks (no key).
- **NASA FIRMS active-fire** — Area-API CSV → fire-detection geo-features (capability-gated on a free map key).
- **NOAA GOES GLM lightning** — GOES Open-Data L2/LCFA NetCDF → lightning-flash geo-features (benchmark-gated;
  the `netCDF4` parser is an optional dependency).
- **Military Mode-S classification** — an honest, two-basis (provider DB flag + operator-supplied ICAO
  address blocks) classifier shared by both ADS-B adapters; never stated as certain.

**Persistence, alerts & history** (M4, opt-in via `AETHER_PERSIST=1`; never gates serving live state)
- **Track history** — fused observations written to SQLite (WAL, versioned migrations) by an independent
  bus consumer with a bounded write queue that drops rather than back-pressures the bus; read back at
  `GET /api/v2/tracks/{id}/history`.
- **Retention manager** — enforces the 30-day window and the `AETHER_DB_MAX_GB` / `AETHER_MIN_FREE_DISK_GB`
  limits via a storage-pressure ladder (downsample → delete oldest → shorten retention → VACUUM). Disk
  limits override time retention.
- **Alert-rule engine** — operator rules (CRUD at `/api/v2/alert-rules`, dry-run preview at `…/{id}/test`)
  evaluate fused-state transitions, events, and source-status changes, with contextual operators
  (geofence enter/exit/contains, distance, elevation, count, changed); matches open an alert with a full
  lifecycle (open → acknowledged/resolved/suppressed) plus cooldown, dedup, and quiet-hours. Environmental
  feeds drive it too: earthquake magnitude/proximity alerts and a FIRMS fire-detection alert template.
- **Geofences** — operator circles/polygons (CRUD at `/api/v2/geofences`) persisted and projected onto the
  map; the authoritative shape feeds the alert engine's containment math.
- **Notifications** — each fired alert settles a per-channel `delivery_status` off the hot path: the
  dashboard alert-centre, browser Notifications, **SMTP email**, and **Discord webhook**. Drivers are
  independent, retry transient failures, never log the SMTP password or webhook token, and an unconfigured
  channel degrades visibly to `unconfigured`. `POST /api/v2/notifications/test` fires a synthetic alert
  through the selected channels so an operator can confirm config.
- **Replay** — `POST /api/v2/replay/sessions` reconstructs live state over a past `[start, end)` window from
  the persisted store on a fresh read-only connection; replay can never fire live alerts and never gates
  serving live state.

See the milestone roadmap (M0–M7) in [`CLAUDE.md`](CLAUDE.md) §4 and the exit criteria in `PRD.md` §32–33.

---

## Quick start (no hardware required)

You can run the entire COP — backend, websocket, map UI, and a simulated mix of every record type — with no
radios and no API keys. This is the verification path; start here.

**1. Backend** — needs Python 3.11+ and Docker (for the Mosquitto broker):

```bash
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\Activate.ps1
pip install -e ".[dev]"
docker compose up -d                                          # local MQTT broker on 127.0.0.1:1883
uvicorn aether.backend.main:app --host 127.0.0.1 --port 8000  # backend + in-process demo source
```

**2. Check the state and stream:**

```bash
curl -s localhost:8000/api/state | python -m json.tool       # mixed records from the demo source
```

The in-process **demo source** publishes a simulated mix of every record type so the COP renders with no
radios. A real deployment sets `AETHER_DEMO_SOURCE=0` and runs source adapters instead (below).

**3. Frontend** — needs Node 18+:

```bash
cd frontend
npm install
npm run dev                                                   # Vite dev server, connects to /ws/v2
```

Open the URL Vite prints (default `http://localhost:5173`). The map defaults to a hosted CARTO dark
basemap; with no Internet (or `VITE_AETHER_BASEMAP=offline`) it falls back to a self-contained dark canvas.

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
`http://127.0.0.1:8080/data/aircraft.json`) — see [Tie in your SDRs](#tie-in-your-sdrs).

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
~1 req/s. **No API key is required for adsb.fi.**

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
[AISStream API key](#aisstream-ais-vessels) via `AETHER_AIS_API_KEY`, and sets the AOI via
`AETHER_STATION_LAT` / `AETHER_STATION_LON`. An enabled-but-unconfigured adapter reports `offline`, never
connects anonymously, and never logs the key. **Limitations:** AIS positions are *reported broadcasts*, not
verified navigation truth; this is a network-only feed, so every vessel is network provenance.

### USGS earthquakes

Public-domain GeoJSON, **no key**. The adapter polls a USGS summary feed, keeps quakes inside the AOI (and
above an optional magnitude floor), and renders each as an earthquake geo-feature; magnitude/proximity can
drive the alert engine (M5.4). `fake` selects the no-hardware feeder:

```bash
AETHER_DEMO_SOURCE=0 AETHER_USGS=1 AETHER_USGS_FEED_URL=fake \
    AETHER_STATION_LAT=38.5 AETHER_STATION_LON=-74.5 \
    uvicorn aether.backend.main:app --app-dir src
```

A real deployment leaves `AETHER_USGS_FEED_URL` at its default (`all_hour` summary) or points it at another
USGS summary feed (`all_day`, `2.5_day`, `significant_month`, …), optionally setting `AETHER_USGS_MIN_MAGNITUDE`.

### SondeHub radiosondes

Public REST telemetry, **no key**. The adapter polls the SondeHub v2 API for sondes heard near the AOI within
a recency window and renders each as a radiosonde track. `fake` selects the no-hardware feeder:

```bash
AETHER_DEMO_SOURCE=0 AETHER_SONDEHUB=1 AETHER_SONDEHUB_API_BASE=fake \
    AETHER_STATION_LAT=38.5 AETHER_STATION_LON=-74.5 \
    uvicorn aether.backend.main:app --app-dir src
```

A real deployment leaves `AETHER_SONDEHUB_API_BASE` at its default. **Limitation:** SondeHub is crowd-sourced;
coverage depends on nearby community receivers. (Predicted-landing + descending-balloon alerts are deferred
pending verification of the live `/predictions` payload.)

### NASA FIRMS active-fire

Capability-gated on a **free map key** (see [Getting API keys](#nasa-firms-map-key)). The adapter queries
the FIRMS Area API for the AOI bounding box and renders each thermal detection as a `fire_detection`
geo-feature; a FIRMS fire-detection alert template (M5.5) can drive the alert engine. `fake` (as base **or**
key) selects the no-hardware feeder:

```bash
AETHER_DEMO_SOURCE=0 AETHER_FIRMS=1 AETHER_FIRMS_MAP_KEY=fake \
    AETHER_STATION_LAT=38.5 AETHER_STATION_LON=-74.5 \
    uvicorn aether.backend.main:app --app-dir src
```

A real deployment supplies a real `AETHER_FIRMS_MAP_KEY`; an enabled-but-keyless adapter reports `offline`
(never crashes, never bakes in a key). **Honest labeling:** a FIRMS record is a satellite *thermal-anomaly /
active-fire detection*, **not** a confirmed wildfire — the confidence class is a detection-quality label, not
a hazard severity.

### NOAA GOES GLM lightning

Benchmark-gated (verdict *acceptable* on the Pi 5 — see [`docs/glm-benchmark.md`](docs/glm-benchmark.md)) and
**off by default**. The live provider reads GLM L2 (LCFA) NetCDF from NOAA's public GOES Open Data on AWS (no
key); the only extra requirement is the optional `netCDF4` parser (`pip install "aether[lightning]"`). A
missing parser degrades to one `offline` status, never a crash. `fake` (as satellite **or** S3 base) selects
the no-hardware feeder, which needs no parser:

```bash
AETHER_DEMO_SOURCE=0 AETHER_GLM=1 AETHER_GLM_SATELLITE=fake \
    AETHER_STATION_LAT=38.5 AETHER_STATION_LON=-74.5 \
    uvicorn aether.backend.main:app --app-dir src
```

A real deployment sets `AETHER_GLM_SATELLITE=G19` (GOES-East) or `G18` (GOES-West) and installs the
`[lightning]` extra. Each flash ages off the map after `AETHER_GLM_FLASH_TTL_S` (10 min by default). Live GLM
is ~1.6 GB/day of download per satellite — see the benchmark note before enabling it on a metered link.

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

## Tie in your SDRs

aether treats your own radios as first-party, privileged sources. The decoders (`readsb`, Dire Wolf) are
external programs that talk to the dongles; aether only **reads** their output. This keeps aether strictly
receive-only and lets each decoder be tuned and updated independently.

### Hardware

- **Raspberry Pi 5** (the target host; any Linux box works for testing).
- **Two RTL-SDR dongles** — one continuous RF service per dongle, addressed by serial; **no antenna switch**
  (settled decision). One dongle for 1090 MHz ADS-B, one for 144.39 MHz APRS.
- **Antennas** — a 1090 MHz ADS-B antenna (a dedicated ADS-B whip/collinear, ideally mast-mounted) and a
  2 m / 144 MHz antenna for APRS. Keep feedlines short or use a low-noise preamp/filter for 1090.
- *(Optional)* a third dongle for 978 MHz UAT, or a separate view-only waterfall dongle — not required.

> **One dongle, one service.** Label each dongle's EEPROM serial once so the decoders bind deterministically:
> ```bash
> rtl_eeprom -d 0 -s 1090     # the dongle you'll use for ADS-B
> rtl_eeprom -d 1 -s 1440     # the dongle you'll use for APRS  (re-plug after writing)
> ```
> Then address `readsb` and Dire Wolf by serial, never by index, so a reboot or re-plug can't swap them.

### 1090 MHz ADS-B via readsb

aether's local ADS-B adapter needs a reachable `aircraft.json` (a file path **or** an HTTP URL). The standard
way to produce one is [`readsb`](https://github.com/wiedehopf/readsb) (with `tar1090` for the web view); the
widely-used automated installer from `wiedehopf/adsb-scripts` sets up both and serves the JSON over HTTP.

1. **Install a decoder.** Install `readsb` (+ optional `tar1090`) and point it at your 1090 dongle by serial.
   readsb decodes 1090 MHz Mode-S/ADS-B and continuously writes `aircraft.json`.
2. **Find the JSON.** Common locations are `http://127.0.0.1:8080/data/aircraft.json` (aether's default),
   `http://<pi-ip>/tar1090/data/aircraft.json`, or an on-disk path like `/run/readsb/aircraft.json`. Confirm
   it with `curl`:
   ```bash
   curl -s http://127.0.0.1:8080/data/aircraft.json | python -m json.tool | head
   ```
3. **Point aether at it** and enable the adapter:
   ```bash
   AETHER_DEMO_SOURCE=0 AETHER_LOCAL_ADSB=1 \
       AETHER_LOCAL_ADSB_SOURCE=http://127.0.0.1:8080/data/aircraft.json \
       AETHER_STATION_LAT=38.5 AETHER_STATION_LON=-74.5 \
       uvicorn aether.backend.main:app --app-dir src
   ```

Local aircraft now render with `local_rf` provenance and feed emergency-squawk (7500/7600/7700) events. Run
the [network ADS-B](#network-ads-b--fusion) adapter alongside it and the same airframe seen by both fuses
into one track. The JSON-format reference is in `PRD.md` §38.

### 144.39 MHz APRS via Dire Wolf (receive-only iGate)

Dire Wolf demodulates and decodes APRS off the 2 m dongle and exposes decoded frames on a TCP KISS socket;
aether reads that socket and **never** writes to it (writing a KISS frame would ask Dire Wolf to transmit).

1. **Copy the sample config** and supply your own callsign + APRS-IS passcode (the repo ships neither):
   ```bash
   cp config/direwolf.conf.example direwolf.conf
   # edit direwolf.conf: set MYCALL and IGLOGIN <callsign> <passcode>
   ```
   The sample is a strict receive-only iGate — it deliberately contains **no** `IGTXVIA`, `PBEACON`,
   `TBEACON`, `DIGIPEAT`, or `PTT` directive, so there is no transmit, beacon, digipeat, or Internet-to-RF
   path. (Generating a real APRS-IS passcode requires a licensed callsign; aether itself never transmits
   regardless — see [APRS-IS / Dire Wolf passcode](#aprs-is--dire-wolf-callsign).)
2. **Run Dire Wolf** against the 2 m dongle, piping `rtl_fm` audio in:
   ```bash
   rtl_fm -f 144.39M -o 4 -s 24000 -g 49 -p <ppm> - | \
       direwolf -c direwolf.conf -r 24000 -D 1 -t 0 -
   ```
   Dire Wolf now decodes 144.39 MHz APRS, serves decoded frames on TCP **KISS port 8001**, and relays
   eligible RF-heard packets to APRS-IS.
3. **Point aether at the KISS port** and enable the adapter:
   ```bash
   AETHER_DEMO_SOURCE=0 AETHER_LOCAL_APRS=1 \
       AETHER_LOCAL_APRS_HOST=127.0.0.1 AETHER_LOCAL_APRS_PORT=8001 \
       uvicorn aether.backend.main:app --app-dir src
   ```

Local APRS stations now render with `local_rf` provenance. Full setup, the responsibility split, and the
no-transmit guardrail are in [`docs/local-aprs-igate.md`](docs/local-aprs-igate.md).

---

## Station location & AOI

aether centers its area of interest on one home position, supplied by the operator — **the repo carries no
coordinates**. Set it once and every consumer uses it: the websocket default viewport, the frontend
range-from-station filter (served via `/api/config`), and the per-adapter AOI centers for every network/
environmental source.

```bash
AETHER_STATION_LAT=38.5 AETHER_STATION_LON=-74.5 AETHER_STATION_RADIUS_NM=500
```

Left at the `0,0` default the station is treated as **unconfigured** and every consumer degrades *visibly*:
the websocket default viewport becomes unbounded (never a degenerate null-island box), the range filter
disables, and the AOI sweeps cover open ocean and find nothing — failing loudly rather than leaking a
location. A per-adapter override (e.g. `AETHER_AIS_LAT`/`_LON`) still wins for the rare multi-AOI setup.

---

## Getting API keys & credentials — step by step

aether keeps **no** credentials in the repo. Each operator supplies their own via the environment; an
enabled-but-unconfigured source reports `offline` rather than failing silently or connecting anonymously.
Only a few sources need anything at all — here is exactly how to get each one. Provider interfaces change;
re-verify against the references in `PRD.md` §38 at setup time.

### What needs a credential (and what doesn't)

| Source | Credential | Cost | Required? |
| --- | --- | --- | --- |
| Network ADS-B (adsb.fi) | none | free | no |
| USGS earthquakes | none | free | no |
| SondeHub radiosondes | none | free | no |
| NOAA GLM lightning | none (optional `netCDF4` parser) | free | no |
| Local readsb / Dire Wolf | none (your own SDR + decoder) | free | no |
| **AIS (AISStream)** | free API key | free | to enable AIS |
| **NASA FIRMS** | free map key | free | to enable FIRMS |
| **APRS-IS / Dire Wolf** | amateur-radio callsign (+ passcode) | free (licensing applies) | to enable APRS-IS / RF iGate |
| **Email notifications** | SMTP login (e.g. Gmail app password) | free | optional channel |
| **Discord notifications** | webhook URL | free | optional channel |

### AISStream (AIS vessels)

Free, but the service is in beta — there is no SLA, and throttling applies per key/user (at most one
subscription update per second).

1. Go to **<https://aisstream.io>** and click **Sign in** (the link goes to `aisstream.io/authenticate`).
   Authenticate with the supported provider (e.g. GitHub).
2. Once signed in, open the **API Keys** page at **<https://aisstream.io/apikeys>**.
3. **Create a new API key** and copy it.
4. Provide it to aether and enable the adapter:
   ```bash
   AETHER_DEMO_SOURCE=0 AETHER_AIS=1 \
       AETHER_AIS_API_KEY=<your-aisstream-key> \
       AETHER_STATION_LAT=38.5 AETHER_STATION_LON=-74.5 \
       uvicorn aether.backend.main:app --app-dir src
   ```
   The key travels only in the subscription body and is never logged. Keep it out of the repo — pass it via
   the environment or a `.env` you do **not** commit.

### NASA FIRMS map key

Free "MAP_KEY," emailed to you. The limit is **5000 transactions / 10-minute interval** (a multi-day request
counts as several transactions); aether's default 15-minute poll of a one-day window stays far under it.

1. Go to **<https://firms.modaps.eosdis.nasa.gov/api/map_key/>**.
2. Enter your **email address** and submit. (If you've registered before, it will say the email is already
   registered and re-send / show your key.)
3. **Check your email** — NASA sends the MAP_KEY to that address. Copy it.
4. Provide it to aether and enable the adapter:
   ```bash
   AETHER_DEMO_SOURCE=0 AETHER_FIRMS=1 \
       AETHER_FIRMS_MAP_KEY=<your-firms-map-key> \
       AETHER_STATION_LAT=38.5 AETHER_STATION_LON=-74.5 \
       uvicorn aether.backend.main:app --app-dir src
   ```
   Without a key the adapter degrades to an `offline` status rather than crashing. The reference tutorial is
   <https://firms.modaps.eosdis.nasa.gov/content/academy/data_api/firms_api_use.html>.

### APRS-IS / Dire Wolf (callsign)

APRS-IS and the local RF iGate are keyed on your **amateur-radio callsign**, not an account you sign up for.

- **For aether's APRS-IS *display* adapter** you need only your callsign; the passcode stays `-1`
  (receive-only — `-1` cannot inject packets, and aether never transmits):
  ```bash
  AETHER_DEMO_SOURCE=0 AETHER_APRS_IS=1 \
      AETHER_APRS_IS_CALLSIGN=<your-callsign> \
      AETHER_STATION_LAT=38.5 AETHER_STATION_LON=-74.5 \
      uvicorn aether.backend.main:app --app-dir src
  ```
- **For the local RF iGate** (Dire Wolf gating RF-heard packets *up* to APRS-IS) Dire Wolf needs a real
  APRS-IS passcode in `IGLOGIN`. The passcode is a deterministic function of your callsign, generated by the
  standard `callpass`/`aprspass` tools shipped with APRS software (Dire Wolf includes one). You must hold a
  valid amateur-radio license to use a real callsign and passcode. aether still never transmits over RF.

### Email notifications (SMTP — e.g. a Gmail app password)

Optional. The email channel is wired only when `AETHER_SMTP_HOST` **and** `AETHER_EMAIL_FROM` **and**
`AETHER_EMAIL_TO` are set; otherwise it resolves to `unconfigured`. For Gmail (other providers are similar):

1. Enable **2-Step Verification** on the Google account (required before app passwords exist):
   **<https://myaccount.google.com/security>**.
2. Create an **App password**: **<https://myaccount.google.com/apppasswords>** → name it "aether" → Google
   shows a 16-character password. Copy it (you won't see it again).
3. Configure aether (587 + STARTTLS is the default; Gmail also supports 465 with `AETHER_SMTP_TLS=ssl`):
   ```bash
   AETHER_SMTP_HOST=smtp.gmail.com AETHER_SMTP_PORT=587 AETHER_SMTP_TLS=starttls \
       AETHER_SMTP_USERNAME=you@gmail.com AETHER_SMTP_PASSWORD=<16-char-app-password> \
       AETHER_EMAIL_FROM=you@gmail.com AETHER_EMAIL_TO=alerts@example.com \
       ...other env... uvicorn aether.backend.main:app --app-dir src
   ```
4. Confirm it end-to-end: `POST /api/v2/notifications/test` fires a synthetic alert through the configured
   channels. The SMTP password is a secret — never logged, never echoed by an API; keep it out of the repo.

### Discord notifications (webhook URL)

Optional. The Discord channel is wired only when `AETHER_DISCORD_WEBHOOK_URL` is set; otherwise it resolves to
`unconfigured`. No app or bot token is needed — just a channel webhook:

1. In Discord, open the target **server** → **Server Settings** → **Integrations** → **Webhooks**.
2. Click **New Webhook**, pick the channel it should post to, optionally rename it, then **Copy Webhook URL**.
3. Configure aether:
   ```bash
   AETHER_DISCORD_WEBHOOK_URL=<your-webhook-url> \
       ...other env... uvicorn aether.backend.main:app --app-dir src
   ```
4. Confirm it with `POST /api/v2/notifications/test`. The webhook URL is a secret — redacted to scheme+host
   in logs/API; keep it out of the repo.

---

## Configuration reference

All settings are environment variables with safe loopback defaults; the no-hardware demo needs none.
Defaults shown in parentheses. Each source also accepts `*_POLL_S` / `*_THROTTLE_S` / `*_TIMEOUT_S` tuning
(see [`src/aether/config.py`](src/aether/config.py)).

### Core & station

| Variable | Default | Purpose |
| --- | --- | --- |
| `AETHER_MQTT_HOST` / `AETHER_MQTT_PORT` | `127.0.0.1` / `1883` | Mosquitto broker (loopback). |
| `AETHER_DEMO_SOURCE` | `1` | Run the in-process demo publisher. Set `0` for real adapters. |
| `AETHER_STATION_LAT` / `_LON` / `_RADIUS_NM` | `0.0` / `0.0` / `500` | Canonical home position + AOI radius (NM). `0,0` ⇒ unconfigured (degrades visibly). |
| `VITE_AETHER_BASEMAP` | _(hosted)_ | Frontend basemap: empty ⇒ hosted CARTO dark; `offline` ⇒ self-contained canvas; any URL ⇒ replacement style JSON. |

### Aircraft, APRS & vessels

| Variable | Default | Purpose |
| --- | --- | --- |
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
| `AETHER_AIS` | `0` | Enable the AISStream adapter. |
| `AETHER_AIS_API_KEY` | _(empty)_ | **Required** — your AISStream key. Empty ⇒ `offline`. |
| `AETHER_AIS_TLS` | `1` | `wss` (real). Set `0` only for the plain-`ws` fake feeder. |
| `AETHER_AIS_HOST` / `_PORT` / `_PATH` | `stream.aisstream.io` / `443` / `/v0/stream` | AISStream endpoint. |

Each network/AIS source also takes a per-adapter `*_LAT` / `*_LON` / `*_RADIUS_NM` that defaults to the
station and overrides it when set (multi-AOI deployments).

### Environmental layers (M5)

| Variable | Default | Purpose |
| --- | --- | --- |
| `AETHER_USGS` | `0` | Enable the USGS earthquake adapter (no key). |
| `AETHER_USGS_FEED_URL` | `…/all_hour.geojson` | USGS summary feed, or `fake` for the no-hardware feeder. |
| `AETHER_USGS_MIN_MAGNITUDE` | `0.0` | Drop quakes below this magnitude (`0` ⇒ show all in AOI). |
| `AETHER_SONDEHUB` | `0` | Enable the SondeHub radiosonde adapter (no key). |
| `AETHER_SONDEHUB_API_BASE` | `https://api.v2.sondehub.org` | SondeHub REST base, or `fake`. |
| `AETHER_SONDEHUB_RECENCY_S` | `3600` | Only sondes heard within this window are returned. |
| `AETHER_FIRMS` | `0` | Enable the NASA FIRMS active-fire adapter. |
| `AETHER_FIRMS_MAP_KEY` | _(empty)_ | **Required** — your FIRMS map key. Empty ⇒ `offline`. `fake` ⇒ feeder. |
| `AETHER_FIRMS_SOURCE` | `VIIRS_SNPP_NRT` | FIRMS source (e.g. `VIIRS_NOAA20_NRT`, `MODIS_NRT`). |
| `AETHER_FIRMS_DAY_RANGE` | `1` | Area-API look-back window in days (1–5). |
| `AETHER_FIRMS_MIN_CONFIDENCE` | _(empty)_ | Min confidence class: `""` / `low` / `nominal` / `high`. |
| `AETHER_GLM` | `0` | Enable the NOAA GLM lightning adapter (benchmark-gated; needs the `[lightning]` extra). |
| `AETHER_GLM_SATELLITE` | `G19` | `G19` (GOES-East) / `G18` (GOES-West), or `fake` for the feeder. |
| `AETHER_GLM_FLASH_TTL_S` | `600` | On-map lifetime of a transient flash before it ages off. |
| `AETHER_GLM_GOOD_QUALITY_ONLY` | `0` | Keep only `good_quality` flashes (else emit all, carry the flag). |

### Persistence, retention, history & replay (M4)

| Variable | Default | Purpose |
| --- | --- | --- |
| `AETHER_PERSIST` | `0` | Persist tracks to SQLite. A sibling consumer — never gates serving live state. |
| `AETHER_DB_PATH` | `aether.db` | SQLite store path (`-wal`/`-shm` sidecars sit alongside). |
| `AETHER_PERSIST_QUEUE_MAX` | `10000` | Bounded write queue; a full queue drops records, never back-pressures the bus. |
| `AETHER_RETENTION_DAYS` | `30` | Retention window. Observations older than this are deleted each sweep. |
| `AETHER_DB_MAX_GB` | `0` (off) | Size budget (DB + WAL). Over it, the storage-pressure ladder reclaims space. |
| `AETHER_MIN_FREE_DISK_GB` | `0` (off) | Free-disk floor. Crossing it is critical pressure; aether sheds its own oldest data. |
| `AETHER_RETENTION_INTERVAL_S` | `3600` | How often the retention sweep runs. |
| `AETHER_DB_HIGH_WATER` / `_CRITICAL_WATER` | `0.85` / `0.95` | Fractions of `DB_MAX_GB` where the ladder engages / escalates. |
| `AETHER_HISTORY_MAX_POINTS` | `10000` | Cap on points returned by one `/api/v2/tracks/{id}/history` request (response flags `truncated`). |
| `AETHER_REPLAY_MAX_RECORDS` | `5000` | Cap on records one replay session reconstructs (flags `truncated`). Replay requires persist. |
| `AETHER_REPLAY_MAX_WINDOW_H` | `168` | Max `[start, end)` span (hours) one replay request may ask for. |

### Notifications (M4)

| Variable | Default | Purpose |
| --- | --- | --- |
| `AETHER_NOTIFY_BROWSER_MIN_SEVERITY` | `info` | Min severity to deliver on the browser channel (else `suppressed`). |
| `AETHER_NOTIFY_EMAIL_MIN_SEVERITY` | `info` | Min severity to deliver email. |
| `AETHER_NOTIFY_DISCORD_MIN_SEVERITY` | `info` | Min severity to deliver Discord. |
| `AETHER_SMTP_HOST` | _(empty)_ | SMTP server. Set (with `EMAIL_FROM`/`EMAIL_TO`) to wire the email channel; empty ⇒ `unconfigured`. |
| `AETHER_SMTP_PORT` | `587` | SMTP port. |
| `AETHER_SMTP_TLS` | `starttls` | `starttls`, `ssl` (465), or `none`. |
| `AETHER_SMTP_USERNAME` / `AETHER_SMTP_PASSWORD` | _(empty)_ | SMTP login. The password is a secret — never logged or echoed by an API. |
| `AETHER_EMAIL_FROM` / `AETHER_EMAIL_TO` | _(empty)_ | Sender / recipient addresses. |
| `AETHER_DISCORD_WEBHOOK_URL` | _(empty)_ | Discord webhook. Set to wire the Discord channel; empty ⇒ `unconfigured`. A secret — redacted to scheme+host in logs/API. |

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
SQLite (WAL) · React + Vite + TypeScript · MapLibre GL JS · Tailscale Serve for private access.

## Layout

```
src/aether/
  schema/      schema v2 record union, geometry, provenance, validation, alert-rule + geofence models
  bus/         MQTT client, topics, no-hardware demo publisher
  state/       in-memory live state + sequence numbering
  fusion/      identity fusion engine, source precedence, freshness/expiry
  adapters/    local ADS-B (readsb) + APRS (Dire Wolf KISS); network ADS-B (adsb.fi) with AOI
               tiling; APRS-IS; AIS (AISStream); USGS, SondeHub, FIRMS, GLM lightning; military
               classification; and a fake feeder for every source
  alerts/      alert-rule engine, conditions, contextual operators, geo math, templates, notify drivers
  persist/     SQLite database, migrations, history writer, retention manager, geofence + alert-rule stores
  replay/      replay session reconstruction + player
  backend/     FastAPI app, /api/config, websocket hub + subscribe filtering, alert/geofence/replay APIs
frontend/      React + Vite + TS COP shell (MapLibre): map, layer control, display filters, source
               health, track list, event/alert feed, TOI watchlist + details, alerts panel
config/        Dire Wolf receive-only iGate example
deploy/        Mosquitto broker config
docs/          local APRS iGate setup; GLM lightning benchmark
scripts/       local check parity + git hooks + GLM benchmark
tests/         schema / parser / fusion / persistence / alert / path tests
```

## License

[GPL-3.0](LICENSE). aether is a private, single-operator project; the repo is public but ships no operator
identity or credentials.

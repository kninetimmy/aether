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
open Internet data into one coherent, dark, high-density map. Planned sources include:

- **Local SDR (receive-only RF):** 1090 MHz ADS-B/Mode-S via `readsb`; 144.39 MHz APRS via Dire Wolf.
- **Network feeds:** wider-area ADS-B from an open provider, APRS-IS, AIS vessels, SondeHub radiosondes,
  lightning, NASA FIRMS active-fire detections, USGS earthquakes, FAA TFR/NOTAM, and CelesTrak orbital data
  with locally propagated overhead-object positions and pass predictions.
- **Operator layers:** tracks of interest, geofences, filters, alerts, history, and replay.

The default area of interest is a **500-nautical-mile radius** around the configured home station,
operator-adjustable. Local and Internet observations of the same identity **fuse into one** record via
strict identity keys, and the UI can always collapse to local-only.

### Receive-only, private by default

The APRS station operates as a **receive-only RF iGate**: valid RF packets may be gated *to* APRS-IS, but
the system never transmits, beacons, digipeats, acks over RF, or creates an Internet-to-RF path. The app and
broker bind loopback; remote access is via **Tailscale Serve only, never Funnel**. The repo carries no
secrets, callsign, or station coordinates — each operator supplies their own config.

---

## Status

Early **Milestone 1 (COP core)** — in-memory live state, no persistence yet. Working today:

- **Schema v2** — a Pydantic v2 discriminated record union (track / geo-feature / event / alert /
  source-status) with provenance, correlation keys, and observed/received/published timestamps.
- **MQTT bus** — records flow over `aether/v2/...` topics (Mosquitto).
- **FastAPI backend** — in-memory live state at `/api/state`, plus a sequence-numbered websocket `/ws/v2`
  (snapshot then deltas, with gap detection and resync).
- **MapLibre frontend** — React + Vite + TypeScript COP shell that renders the full record union live,
  through a centralized presentation registry; map, layer control, source-health, track list, and
  event/alert panels.
- **No-hardware demo source** — a simulated mixed-record publisher that exercises the full path
  (adapter → bus → state → websocket → UI) so the COP renders without any radios.

See the milestone roadmap (M0–M7) in [`CLAUDE.md`](CLAUDE.md) §4 and the exit criteria in `PRD.md` §32–33.

---

## Run it (no hardware required)

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

A real deployment sets `AETHER_DEMO_SOURCE=0` and runs source adapters instead of the in-process demo.

**Frontend** — needs Node:

```bash
cd frontend
npm install
npm run dev                                                   # Vite dev server, connects to /ws/v2
```

### Local SDR adapters (no radio needed)

Each local source ships a fake feeder so its full path (adapter → bus → state →
websocket → UI) runs with no SDR. Turn off the demo source and run a feeder +
its adapter:

```bash
# Local ADS-B (readsb) — fake aircraft.json feeder + adapter
python -m aether.adapters.readsb_fake_feeder /tmp/aircraft.json &              # writes a data file only
AETHER_DEMO_SOURCE=0 AETHER_LOCAL_ADSB=1 \
    AETHER_LOCAL_ADSB_SOURCE=/tmp/aircraft.json \
    uvicorn aether.backend.main:app --app-dir src

# Local APRS (Dire Wolf) — fake KISS server + adapter
python -m aether.adapters.aprs_fake_feeder 127.0.0.1 8001 &                    # fake KISS frames only; never transmits
AETHER_DEMO_SOURCE=0 AETHER_LOCAL_APRS=1 \
    AETHER_LOCAL_APRS_HOST=127.0.0.1 AETHER_LOCAL_APRS_PORT=8001 \
    uvicorn aether.backend.main:app --app-dir src
```

The APRS station is **receive-only**: see [`docs/local-aprs-igate.md`](docs/local-aprs-igate.md)
and the sample [`config/direwolf.conf.example`](config/direwolf.conf.example) for the
real-radio setup and the no-transmit guardrail.

### Network ADS-B + fusion (no radio, no live API)

The network adapter sweeps the AOI from an open provider (default `adsb.fi`); a
`fake` provider stands in for the no-hardware path. Run it **alongside** local
ADS-B and the same airframe seen by both fuses into one track — local RF
privileged, the Internet feed filling the gaps:

```bash
# Local ADS-B fake feeder (the local leg) + both adapters; network uses the fake provider.
python -m aether.adapters.readsb_fake_feeder /tmp/aircraft.json &              # writes a data file only
AETHER_DEMO_SOURCE=0 \
    AETHER_LOCAL_ADSB=1 AETHER_LOCAL_ADSB_SOURCE=/tmp/aircraft.json \
    AETHER_NETWORK_ADSB=1 AETHER_NETWORK_ADSB_PROVIDER=fake \
    uvicorn aether.backend.main:app --app-dir src
```

A real deployment sets `AETHER_NETWORK_ADSB_PROVIDER=adsb.fi` and supplies the AOI
center via `AETHER_NETWORK_ADSB_LAT` / `AETHER_NETWORK_ADSB_LON` (the repo carries
no station coordinates); a >250 NM `AETHER_NETWORK_ADSB_RADIUS_NM` is tiled into
provider-compliant requests automatically.

### AIS vessels (no live API)

The AIS adapter streams vessel tracks from [AISStream.io](https://aisstream.io) over
a secure WebSocket, subscribed to a bounding box around the AOI, merging each
vessel's separate position and static/voyage messages into one track by MMSI. A fake
WebSocket feeder stands in for the no-hardware path (plain `ws`, no key):

```bash
# Fake AISStream WebSocket server + adapter (static + dynamic merge by MMSI)
python -m aether.adapters.ais_fake_feeder 127.0.0.1 8765 &                     # canned vessel frames only
AETHER_DEMO_SOURCE=0 AETHER_AIS=1 AETHER_AIS_TLS=0 \
    AETHER_AIS_HOST=127.0.0.1 AETHER_AIS_PORT=8765 AETHER_AIS_API_KEY=demo \
    AETHER_AIS_LAT=38.5 AETHER_AIS_LON=-74.5 \
    uvicorn aether.backend.main:app --app-dir src
```

A real deployment leaves `AETHER_AIS_TLS=1` (the `wss://stream.aisstream.io`
endpoint), supplies a free [AISStream](https://aisstream.io) API key via
`AETHER_AIS_API_KEY`, and the AOI center via `AETHER_AIS_LAT` / `AETHER_AIS_LON` (the
repo carries no key or coordinates; an enabled-but-unconfigured adapter reports
`offline`, never connects anonymously). **Limitations:** AIS positions are *reported
broadcasts*, not verified navigation truth (AIS-FR-006); AISStream enforces per-key
rate limits, so the adapter holds a single subscription within the configured area;
this is a network-only feed (no local-RF leg), so every vessel is network provenance.

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

Python 3.11+ async · Pydantic v2 · Mosquitto (local MQTT) · FastAPI (REST + WebSocket) · React + Vite +
TypeScript · MapLibre GL JS · Tailscale Serve for private access.

## Layout

```
src/aether/
  schema/      schema v2 record union, geometry, provenance, validation
  bus/         MQTT client, topics, no-hardware demo publisher
  state/       in-memory live state + sequence numbering
  backend/     FastAPI app, websocket hub, wire protocol
frontend/      React + Vite + TS COP shell (MapLibre)
deploy/        Mosquitto broker config
scripts/       local check parity + git hooks
tests/         parser/schema/path tests
```

## License

[GPL-3.0](LICENSE). aether is a private, single-operator project; the repo is public but ships no operator
identity or credentials.

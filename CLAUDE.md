# aether — Agent Guide

> **What this is:** the always-loaded operating guide for an AI coding agent (Claude Code / OpenCode) on
> `aether`. It carries the session protocol, the condensed decision list, the milestone roadmap, and how to
> verify work. **The full product + architecture authority is `PRD.md`** — read it before implementing
> any milestone. This file is the index and the rules; it defers to the PRD for detail and must never
> contradict it.
>
> **Loading:** Claude Code auto-loads `CLAUDE.md`, OpenCode auto-loads `AGENTS.md`. Keep them identical —
> `cp AGENTS.md CLAUDE.md` (or symlink) so both tools see the same guide.

---

## 0. Starting a session (do this first, every time)

1. Read this file, then **read `PRD.md`** (the authority).
2. **Load memory from memhub** (§8): `recall`, `status`, `list_tasks`, then `locate`/`search` — before
   grepping or loading large files. Follow memhub's own server instructions for mid-session routing.
3. Inspect the repo and **reconcile it against §3 (Current status) and the PRD milestones (§4)**. The repo
   implements the COP through **M3 (network fusion)**; **M4 (persistence/alerts/history)** is the target.
   **Code is ground truth for current state; the PRD is ground truth for intent.** Note any drift in your
   first message.
4. Take the **next unchecked milestone slice in order** (§4 / PRD §32). Order is deliberate — build one
   tested vertical slice at a time; never implement multiple milestones in one uncontrolled pass.
5. Before writing code, state: the milestone slice, the files you expect to change, and the exit criteria
   you're satisfying (from PRD §32/§33).
6. Preserve every decision in §5. To change one, stage a `propose_decision` and ask the maintainer; update
   the PRD (or a decision record) when a settled decision actually changes.
7. As you work, record memory in memhub: `task_add`/`task_done` directly; choices and durable facts as
   **staged** `propose_decision`/`propose_fact` (maintainer `review accept` makes them durable).
8. A change is complete only when its tests pass **and** the §6 no-hardware verification still passes.

---

## 1. What aether is

A private, local-first, browser-based **common operating picture (COP)** for a Raspberry Pi 5 home station.
Local SDR receivers are the trusted first-party sources — ADS-B (1090) via `readsb`, APRS (144.39) via Dire
Wolf, **receive-only on RF** (valid RF packets may be gated *to* APRS-IS; no transmit, beacon, digipeat, RF
acks, or Internet-to-RF path). On top of that it fuses open Internet feeds (network ADS-B, APRS-IS, AIS,
SondeHub, lightning, FIRMS, USGS, FAA TFR/NOTAM, CelesTrak orbital) into one time-aware map with provenance,
alerts, geofences, watchlists, and replay. Every record is tagged with provenance so the operator always
knows **what their own radios received vs. what only an Internet feed reported**, and can collapse to
local-only with one filter. Full vision and requirements: **`PRD.md`**.

---

## 2. Authority & docs map

- **`PRD.md`** — product + architecture authority (the what/why, milestones, requirements). Wins on
  conflict.
- **`AGENTS.md` / `CLAUDE.md`** (this file) — lean always-loaded operating guide. Defers to the PRD.
- **memhub (MCP)** — runtime memory; recall to read, staged writes to record. Routing rules live in its
  server instructions.
- **Code** — ground truth for *current* state.
- `ARCHITECTURE.md` from the earlier brief is **retired** (folded into PRD §13–31); delete it from the repo.

---

## 3. Current status

Built and verified — milestones **M1 (COP core) → M5 (environmental layers)** are complete (PRD §32 exit
criteria met); **M6 (airspace & orbital)** is in progress. SQLite persistence landed in M4 as a sibling
bus consumer that never gates serving live state (§5). The v1 `Entity`/`Event` skeleton has been
superseded by schema v2.

- **Schema v2** (PRD §14): the discriminated record union — track / geo-feature / event / alert /
  source-status — with provenance, `correlation_key`, and observed/received/published timestamps.
- **M1 — COP core:** MQTT v2 topics; FastAPI live state (`/api/state`, runtime `/api/config`);
  sequence-numbered websocket `/ws/v2` (snapshot+deltas, gap detection/resync, per-connection `subscribe`
  filtering); React+Vite+TS MapLibre shell with a centralized presentation registry; in-process demo source.
- **M2 — Local RF baseline:** readsb `aircraft.json` adapter + emergency-squawk events; Dire Wolf KISS
  APRS adapter; receive-only iGate config (`config/direwolf.conf.example`, `docs/local-aprs-igate.md`);
  local badges + source health.
- **M3 — Network fusion:** network ADS-B (adsb.fi) with 500 NM AOI tiling; APRS-IS display adapter; AIS
  (AISStream); the fusion engine (local + Internet observations of one identity collapse into a single
  track by strict identity key, with source precedence and freshness/expiry); military Mode-S
  classification (provider bit + operator-supplied ICAO blocks); UI display filters + TOI watchlist.
- **M4 — Alerts & history:** SQLite WAL + migrations with their consumers — retention manager (disk
  limits), persist-cadence sampling, track-history read API, geofence CRUD, alert-rule CRUD + stateful
  engine (contextual operators: geofence/distance/elevation/count/changed) with ack/resolve + `/test`,
  notification dispatch (dashboard/browser/SMTP/Discord), replay timeline (replay can't fire live alerts),
  and the alerts UI.
- **M5 — Environmental layers:** USGS earthquakes (GeoJSON → earthquake features, M5.1);
  SondeHub radiosonde telemetry (REST → radiosonde tracks, M5.2); NASA FIRMS active-fire (Area-API CSV →
  fire-detection features, capability-gated on a map key, M5.3); earthquake alerts (geo-features drive the
  alert engine, M5.4) + FIRMS fire-detection alert template (M5.5); NOAA GLM lightning (GOES Open-Data
  L2/LCFA NetCDF → lightning-flash features, benchmark-gated/`netCDF4`-optional, M5.6); client-side map
  clustering for dense point layers (LIGHTNING-FR-006, M5.7).
- **M6 — Airspace & orbital (in progress):** FAA TFR adapter (`faa_tfr.py`, M6.1) — two-step poll of the
  official service (`tfrapi/exportTfrList` JSON list → `download/detail_<n>.xml` `<XNOTAM-Update>` detail),
  decimal-deg/hemisphere coords + local-`codeTimeZone`→UTC times, `abdMergedArea` vertices → `Polygon`/
  `MultiPolygon` `GeoFeatureRecord`s (`feature_type="tfr"`), AOI filter + revision dedupe + bounded
  detail-fetch budget + optional `states` pre-filter; unparseable geometry → a textual `EventRecord`
  (never an invented shape, §18.10); FAA attribution + "not a flight-planning product" caveat.

Every source ships a fake/replay feeder, so the full path (adapter → bus → state → websocket → UI) runs
with tests green and no hardware (PRD §6, §34).

**Next:** continue M6 — FAA NOTAM (capability-gated, §18.11) and/or CelesTrak GP sync + SGP4 propagation +
pass prediction (§18.12, §11.14). **SGP4/orbital is ultracode-gated** (maintainer approval before any
multi-agent run). A natural M6 follow-up to M6.1 is TFR-into-geofence / TFR-becomes-active alerts (PRD §32
triggers #15/#16). **Deferred:** M5.2b (SondeHub predicted landing + descending-balloon alert,
SONDE-FR-006/007) pending verification of the live `/predictions` payload — shipping an unverified parser
would fail *silently*, violating the fail-visibly guardrail.

---

## 4. Roadmap — PRD milestones (take the next unchecked, in order; exit criteria in PRD §32–33)

- **M0 — Revector & preserve:** PRD in repo as authority; earlier brief kept for history; AGENTS/CLAUDE
  updated; schema v2 design; migration plan from v1; updated roadmap. *Exit:* no agent can mistake the old
  receive-only scope for the current product.
- **M1 — COP core:** schema v2; MQTT v2 topics; FastAPI live state; source status; sequence-numbered
  websocket; MapLibre shell; layer registry; demo source. **No persistence here** (in-memory live state).
  *Exit:* mixed tracks/features/events/alerts/status render from simulated data.
- **M2 — Local RF baseline:** readsb + APRS adapters; Dire Wolf receive-only iGate config; local badges +
  source health; emergency-squawk templates. *Exit:* both SDRs run; valid RF APRS gated to APRS-IS; no RF
  transmit path; local data on the map.
- **M3 — Network fusion:** network ADS-B provider adapter + aircraft fusion; APRS-IS display adapter + APRS
  fusion; AISStream; 500 NM AOI + provider tiling; filters + TOI watchlist. *Exit:* local/network duplicates
  appear once with correct provenance.
- **M4 — Alerts & history:** **SQLite WAL + migrations (introduced here, with its consumers);** alert-rule
  CRUD + engine; dashboard/browser/SMTP/Discord notifications; 30-day retention; track history; replay
  timeline; geofences. *Exit:* edit/test/trigger/ack/resolve rules; replay can't fire live alerts; disk
  limits enforced.
- **M5 — Environmental layers:** SondeHub, USGS, NASA FIRMS, NOAA GLM lightning (benchmark-gated);
  clustering + environmental alerts. *Exit:* correct age/attribution/caveats; failures isolated.
- **M6 — Airspace & orbital:** FAA TFR; capability-gated NOTAM; CelesTrak GP sync + SGP4 propagation + pass
  predictions; satellite watchlist/alerts. *Exit:* TFR geometry/validity visible; NOTAM honest without
  creds; passes predicted; element age shown.
- **M7 — Hardening & release:** Pi benchmark; 7-day soak; security/accessibility passes; backup/restore;
  upgrade/migration + attribution docs; redacted debug bundle; release packaging. *Exit:* fresh install
  works; optional integrations fail gracefully; no secrets/location in history.

---

## 5. Settled decisions (condensed from PRD §2/§6/§8 — do not relitigate without the maintainer)

- **No RF transmission, ever** — no APRS Internet-to-RF gating, beaconing, digipeating, RF acks, PTT,
  transmitter control, or SDR retuning from the dashboard. (RF→APRS-IS gating of locally-heard packets *is*
  allowed; that's receive-only.)
- **Local RF vs network is first-class provenance.** Records carry provenance; local and Internet
  observations of the same identity **fuse into one** via strict identity keys (no proximity-only merges);
  the UI can always show local-only.
- **Schema v2** (PRD §14): discriminated record union — track / geo-feature / event / alert / source-status,
  with provenance list, `correlation_key`, three timestamps (observed/received/published), and
  predicted/derived/confidence labeling. Supersedes v1 `Entity`/`Event`.
- Normalize at the adapter edge; keep the backend generic (no per-source branching); source-specific UI in
  one centralized presentation registry; unknown sources get a generic fallback.
- **Persistence (SQLite) lands at M4 with its consumers**, never gates serving live state.
- Areas/polygons (TFR, alerts, fire, lightning) are `GeoFeatureRecord`s on overlay layers — not tracks.
- Open Internet feeds are **read-only and permitted/public**; respect each provider's terms, rate limits,
  and attribution; no restricted/classified/proprietary/access-controlled data; no bypassing provider limits.
- **CelesTrak orbital tracking + pass prediction is in scope** (propagating elements to plot overhead
  objects) — this is *tracking*, not satellite reception. Still **no NOAA APT / Meteor RF reception** (those
  APT birds were decommissioned by Aug 2025; maintainer rejected polarized/tracking RX antennas).
- Stack: Python 3.11+ async; Mosquitto (local); FastAPI REST+WS; React+Vite+TS; MapLibre GL; Pydantic v2.
- One RTL-SDR per continuous RF service; address by serial; **no RF antenna switch.**
- Private by default: app + broker bind loopback; **Tailscale Serve only, never Funnel**; no public exposure.
- Repo is public and **carries no secrets, callsign, or station coordinates**; each operator supplies their
  own config. GPL-3.0 code (e.g. NWS-Alert-Dashboard) is consumed at arm's length over a protocol, never
  vendored in.
- HackRF One + KrakenSDR are reserved for a separate radar project; don't design around them.
- Waterfall (if added) is view-only on its own dongle; OpenWebRX+ first.
- **Cyber threat intelligence is out of scope** — non-geospatial; a separate project if ever, never a map
  layer. The only bridge is watchlist *enrichment* of a track.
- Honest labeling: predicted=predicted, stale=stale, inferred military classification is never stated as
  certain; FIRMS is a thermal detection, not a confirmed fire; the product is not authoritative for any
  life-safety/operational use.

---

## 6. Run & verify (no hardware — the gate for every change)

Current COP core (M1) — the in-process demo source publishes mixed schema-v2 records onto the bus, so the
full path renders with no radios:

```bash
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\Activate.ps1
pip install -e ".[dev]"
docker compose up -d                                          # local MQTT broker
uvicorn aether.backend.main:app --host 127.0.0.1 --port 8000  # backend + in-process demo source
curl -s localhost:8000/api/state | python -m json.tool        # expect mixed records (track/feature/event/alert/status)
cd frontend && npm install && npm run dev                     # MapLibre COP shell on /ws/v2 (separate terminal)
scripts/check.sh                                              # ruff + mypy (strict) + pytest — expect green
```

A real deployment sets `AETHER_DEMO_SOURCE=0` and runs source adapters instead of the in-process demo.

As the COP lands, every source ships a fake/replay feeder and the same rule holds: **simulated data must
exercise the full path (adapter → bus → state → websocket → UI) with tests green, before hardware or live
APIs.** Definition of done for a new source: PRD §34.

---

## 7. Guardrails (from PRD §37 + isolation rules)

- The PRD is the product authority; don't silently reintroduce the old receive-only-display scope, and don't
  add RF transmission.
- Verify current provider documentation before implementing an adapter; don't invent undocumented API
  fields. Keep integrations optional and capability-gated; a missing key/feed degrades visibly, never
  crashes the app.
- Protect the schema: before changing it, explain why, whether it's compatible, what else must change, and
  which tests guard it; bump `schema_version` and update all consumers together.
- Isolate failures: one malformed record, dead decoder, slow client, or downed feed must not take down the
  backend, other adapters, or other browsers.
- Security: loopback/tailnet binds only; never Funnel; no secrets, callsign, or coordinates in the repo or
  history; redact debug bundles.
- Small, reviewable slices: inspect → name files → smallest coherent change → tests → full suite → no-hw
  verify → record in memhub → update status.

---

## 8. Memory (memhub)

memhub (https://github.com/kninetimmy/memhub) is the durable per-repo memory, running as a **connected MCP
server** (`memhub serve`, stdio) across Claude Code / Codex / OpenCode.

- **Two layers:** tracked/committed/static guardrail files (`AGENTS.md`, `CLAUDE.md`, `PRD.md`) that
  memhub never edits; and gitignored, machine-local `.memhub/` (SQLite store + regenerated views under
  `.memhub/rendered/`), reconstructed per machine via memhub's own sync.
- **Routing rules live in memhub's MCP server instructions** — follow them; don't duplicate here.
- **Read** with `recall`/`locate`/`search`/`list_*` before grepping. **Write** durable claims via staged
  `propose_decision`/`propose_fact` → maintainer `review accept`; direct `task_add`/`task_done`/
  `record_command`/`doc_add`/`log_session_note` are fine.
- **Setup is orthogonal:** MCP registration is once per machine (PRD/README install); `/memhub init` is once
  per repo. Neither touches the tracked guardrail files.

---

## 9. Quick reference

```
MQTT broker     127.0.0.1:1883            topics per PRD §23      backend subscribes the source tree
Backend         127.0.0.1:8000            /api/health  /api/state  /ws   (full API: PRD §21–22)
Decoder ports   readsb SBS 30003 (Beast 30005) · Dire Wolf AGW 8000 / KISS 8001
Frequencies     ADS-B 1090 · UAT 978 · APRS 144.39 · ACARS ~131 · ISM 433   (MHz)
Default AOI     500 NM radius about the home station (tiled for low-limit providers); operator-adjustable
Provenance      every record: local (my antenna) vs network (Internet feed); fused by strict identity key
Provider refs   PRD §38 (adsb.lol/adsb.fi/OpenSky, APRS-IS, AISStream, SondeHub, FIRMS, USGS, GLM,
                FAA TFR/NOTAM, CelesTrak) — re-verify at build time
```

For everything else — schema v2 fields, fusion/correlation, AOI/tiling, adapter framework, per-source
architecture, persistence/retention, alert engine, API, websocket, MQTT topics, UI, security, deployment,
repo structure, testing, milestones, and acceptance criteria — read **`PRD.md`**.

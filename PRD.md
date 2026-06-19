# aether COP — Product Requirements Document

> **Document status:** Authoritative product and architecture requirements  
> **Version:** 2.0-draft  
> **Date:** 2026-06-14  
> **Project:** `aether`  
> **Product class:** Local-first, browser-based common operating picture (COP)  
> **Primary deployment:** Raspberry Pi 5, private access through Tailscale Serve  
> **Primary operator:** One private user  
> **Distribution model:** Open-source GitHub repository; each operator supplies their own credentials, station identity, and configuration

> **How this fits the repo's other docs (read this first):**
> - **`PRD.md` (this file) is the product + architecture authority** — the "what" and "why", the
>   milestones, and the requirements. When anything conflicts, this wins.
> - **`AGENTS.md` / `CLAUDE.md` is the lean always-loaded operating guide** — session protocol, the
>   condensed decision list, the milestone roadmap, and how to verify a change. It defers to this PRD for
>   detail and must not contradict it.
> - **memhub (MCP) is runtime memory** — recall/locate to read, staged `propose_*` → maintainer `review
>   accept` to write. Routing rules live in memhub's server instructions.
> - **Code is ground truth for current state.** The repo currently holds the older v1 ADS-B skeleton; this
>   PRD is the target. The standalone `ARCHITECTURE.md` from the earlier brief is **retired** — its content
>   is superseded by §13–31 here; delete it to avoid two architecture specs.

---

## 1. Executive summary

`aether` is being re-scoped from a small receive-only SDR dashboard into a **real-time geospatial common operating picture** that fuses local radio observations, open Internet feeds, environmental events, airspace restrictions, orbital predictions, and configurable alerts into one coherent browser interface.

The system will run continuously on a Raspberry Pi 5 and use two dedicated SDR receivers:

1. One receiver for local 1090 MHz ADS-B/Mode-S through `readsb`.
2. One receiver for 144.39 MHz APRS through Dire Wolf.

The APRS station will operate as a **receive-only RF iGate**: valid packets heard over RF may be forwarded to APRS-IS, but the system will not transmit APRS packets over RF, beacon, digipeat, acknowledge messages over RF, or create an Internet-to-RF path.

The dashboard will combine:

- Locally received ADS-B and Mode-S aircraft.
- Wider-area aircraft data from an open network provider.
- Emergency squawks and reported or address-classified military Mode-S tracks.
- Locally received APRS stations, objects, messages, weather, and telemetry.
- APRS-IS traffic within a configurable geographic area.
- AIS vessel traffic.
- SondeHub radiosonde and balloon telemetry.
- Lightning observations from a legally accessible provider.
- NASA FIRMS active-fire and thermal-anomaly detections.
- USGS earthquakes.
- FAA TFRs and geospatially usable NOTAMs.
- CelesTrak orbital data with locally propagated overhead-object positions and pass predictions.
- User-defined tracks of interest, geofences, filters, and alerts.

The default area of interest is a **500-nautical-mile radius centered on the configured home station**, with user-adjustable source, range, altitude, classification, age, and viewport filters. Sources with lower request limits will divide the area into smaller provider-compliant queries and deduplicate the results.

The user interface will be modern, dark, high-density, and tactical in visual character without pretending to be an official military command system. It will emphasize source provenance, data age, uncertainty, and whether a track was received locally or only reported by an Internet source.

The system is a hobbyist situational-awareness tool. It is not an authoritative aviation, maritime, emergency-management, orbital-safety, weather-warning, or navigation product.

---

## 2. Scope revector from the earlier `aether`

This document supersedes the previous product scope where the two conflict.

### 2.1 Decisions retained

The following earlier architecture decisions remain valid:

- Raspberry Pi 5 as the primary always-on host.
- Python 3.11+ and an async-first backend.
- Pydantic v2 normalized schemas.
- Mosquitto as the local message bus.
- FastAPI for REST and WebSocket services.
- React, Vite, and TypeScript for the frontend.
- MapLibre GL JS for the map.
- Tailscale Serve for private HTTPS/WSS access.
- Separate processes for decoders and adapters.
- Adapter-side normalization.
- Hardware-free replay fixtures and tests.
- One continuously assigned RTL-SDR per radio service.
- Loopback-only application and broker listeners by default.
- No Tailscale Funnel.
- Generic source integration rather than scattered source-specific backend logic.
- Sequence-numbered WebSocket state updates and explicit resynchronization.
- Failure isolation, bounded queues, reconnect logic, and defensive parsing.

### 2.2 Decisions changed

The following previous restrictions are intentionally changed:

| Previous scope | New requirement |
|---|---|
| Receive-only data display with no iGate operation | APRS RF-to-Internet iGate operation is required |
| No satellite tracking | CelesTrak-based orbital tracking and pass prediction are required |
| Persistence deferred | Rolling history, replay, and alerts require SQLite — introduced at **Milestone 4 with its consumers** (history/replay/retention/alerts), not in the COP core; live state never depends on it |
| Point-oriented entity/event model | Schema v2 must support tracks, points, lines, polygons, events, alerts, and source status |
| Primarily local SDR sources | Local SDR sources and open Internet feeds are equal first-class adapters |
| Optional simple dashboard | A full COP interface with layers, filters, TOIs, alerts, and replay is required |
| No source-specific fusion | Aircraft and APRS observations must fuse across local and Internet sources |

### 2.3 Decisions that remain prohibited

The revector does **not** authorize RF transmission.

The project must not add:

- APRS Internet-to-RF gating.
- APRS beaconing.
- APRS digipeating.
- RF message acknowledgements.
- Radio PTT control.
- Transmitter control.
- Remote SDR retuning from the dashboard.
- Public unauthenticated exposure.
- Tailscale Funnel.
- Collection or use of restricted, classified, proprietary, stolen, or access-controlled government data.
- Attempts to identify aircraft through unsupported behavioral speculation.
- Claims that inferred military classification is certain.
- Raw IQ streaming to the browser.
- Automatic operational decisions based on this dashboard.

---

## 3. Product vision

Create a private, local-first COP that gives one operator a coherent answer to five questions:

1. **What is moving around me?**
2. **What environmental or airspace events are developing?**
3. **Which tracks or events are unusual or important?**
4. **Which information did my own radios receive versus an Internet feed?**
5. **What changed recently, and what triggered an alert?**

The product should feel like one integrated operating picture, not ten unrelated maps embedded in a webpage.

---

## 4. Problem statement

Open geospatial data is fragmented across protocol-specific applications and websites:

- Aircraft are shown in ADS-B viewers.
- APRS packets are shown in APRS clients or APRS-IS sites.
- AIS ships are shown in maritime trackers.
- Balloons are shown in SondeHub.
- Fire detections are shown in FIRMS.
- Earthquakes are shown in USGS products.
- TFRs and NOTAMs are shown in FAA products.
- Satellites are shown in orbital trackers.
- Lightning is shown in weather products.

This creates several problems:

- The operator cannot see spatial or temporal relationships between source types.
- The same aircraft or APRS station may appear twice when local and network feeds are viewed separately.
- There is no unified alert engine.
- Source freshness, outages, and provenance are not obvious.
- Historical review requires multiple services.
- Local SDR data is not visually distinguished from remotely sourced observations.
- Existing public maps are not designed around the operator’s own station, geofences, watchlists, or tailnet.

`aether` will solve this by normalizing each source into a common model, fusing related observations, preserving provenance, and presenting one time-aware geospatial picture.

---

## 5. Product goals

### 5.1 Primary goals

1. Run continuously on a Raspberry Pi 5 with two SDR receivers.
2. Display local ADS-B and APRS observations with low latency.
3. Forward valid locally received APRS packets to APRS-IS through Dire Wolf.
4. Display APRS-IS traffic for a configurable area without confusing it with local RF reception.
5. Fuse local and Internet aircraft observations into one track per identity.
6. Fuse local and APRS-IS observations into one APRS entity per callsign/object identity.
7. Integrate AIS, SondeHub, lightning, FIRMS, USGS, FAA airspace notices, and CelesTrak.
8. Present all source types on one performant browser map.
9. Provide editable filters, geofences, watchlists, and alert rules.
10. Retain approximately 30 days of useful history, constrained by disk limits.
11. Support replay across multiple source types on a shared timeline.
12. Deliver alerts through the dashboard, browser notifications, email, and Discord when configured.
13. Remain private by default through Tailscale Serve.
14. Be safe to publish on GitHub with no embedded credentials or personal station data.
15. Remain testable without radio hardware or live API access.

### 5.2 Secondary goals

- Provide source health and latency visibility.
- Support data exports for personal analysis.
- Make adding a new source primarily an adapter task.
- Support additional operators who clone the repository and supply their own configuration.
- Preserve a path to additional local receivers without requiring them.
- Support desktop-first use and a useful mobile/PWA view.
- Allow source-specific presentation without source-specific core state logic.

---

## 6. Non-goals

The first complete release will not:

- Replace FAA, USGS, NOAA, NASA, Coast Guard, NWS, CelesTrak, or other authoritative products.
- Provide flight planning, maritime navigation, emergency dispatch, weather warning, or spaceflight safety.
- Identify classified missions, payloads, units, routes, intentions, or activities.
- Infer military identity only from motion, route, altitude, or callsign appearance.
- Guarantee continuous worldwide coverage from volunteer-fed networks.
- Store or redistribute entire third-party global datasets.
- Provide multi-tenant hosting.
- Provide a public SaaS.
- Provide native iOS or Android applications.
- Operate as a bidirectional APRS iGate.
- Replace Dire Wolf’s tested iGate behavior with a custom gateway implementation.
- Render every cataloged orbital object globally at one-second cadence.
- Preserve every high-rate raw observation for 30 days.
- Treat every FIRMS thermal anomaly as a confirmed wildfire.
- Treat every NOAA GLM observation as a cloud-to-ground strike.
- Treat every emergency squawk as a confirmed real-world emergency.
- Treat provider-reported military status as authoritative.
- Scrape websites where a documented or permitted data interface is unavailable.
- Bypass rate limits, authentication, anti-bot controls, or terms of use.

---

## 7. Users and deployment model

### 7.1 Primary user

A technically capable hobbyist running:

- A Raspberry Pi 5.
- Two RTL-SDR-class receivers.
- A 1090 MHz ADS-B antenna.
- A 2-meter APRS antenna.
- Tailscale.
- A private tailnet.
- A licensed amateur-radio callsign for APRS-IS iGate operation.

### 7.2 Secondary users

Other hobbyists may clone the GitHub repository and deploy their own installation.

The repository must therefore:

- Include no maintainer-specific callsign, coordinates, email, tailnet name, hostnames, API keys, or webhook URLs.
- Provide `.env.example` and documented configuration.
- Make integrations optional.
- Start successfully when optional credentials are absent.
- Clearly show which integrations are disabled, unavailable, degraded, or misconfigured.
- Include replay data so the interface can be evaluated without live services.

### 7.3 Authentication model

Initial deployment is single-user and tailnet-only.

The application will not implement a username/password account system in v1. Access control is provided by:

- Loopback-only service binding.
- Tailscale Serve.
- Tailnet device and identity controls.
- Origin and host validation for mutating browser requests.

The architecture must not prevent a future multi-user layer, but multi-user accounts are not a v1 requirement.

---

## 8. Product principles

### 8.1 Provenance is always visible

Every fused track or feature must preserve:

- Contributing sources.
- Most recent source.
- Last local RF reception time, if any.
- Observation age.
- Provider-reported fields.
- Derived or inferred fields.
- Confidence or classification reason where applicable.

### 8.2 Local reception is privileged, not exclusive

A fresh local observation has priority for dynamic aircraft or APRS fields because it was received by the operator’s own station. Internet data may:

- Fill missing fields.
- Continue a track after local reception is lost.
- Supply metadata.
- Expand coverage beyond local RF range.

### 8.3 Derived data must be labeled

Examples:

- Satellite locations are propagated predictions, not live sensor observations.
- Military classification may be provider-reported, address-block-based, or both.
- Fire points are thermal detections, not confirmed perimeters.
- NOAA GLM reports total lightning flashes, not necessarily ground strikes.
- A TFR or NOTAM record may be parsed from source geometry, not manually verified.

### 8.4 Stale data must never look live

Every visible object must have an age or validity state:

- Live.
- Delayed.
- Stale.
- Expired.
- Predicted.
- Historical/replay.

### 8.5 Optional integrations must fail independently

A broken AIS, FIRMS, or FAA adapter must not affect local ADS-B, APRS, the backend, or other adapters.

### 8.6 The Pi is an appliance

Prefer:

- Bounded memory.
- Bounded queues.
- Bounded retention.
- Predictable polling.
- Simple process boundaries.
- Good logs.
- Automatic restart.
- Conservative dependencies.
- Hardware-free tests.

Avoid:

- Heavy distributed infrastructure.
- Kubernetes.
- A separate external database server.
- Unbounded event streams.
- Browser-side secret handling.
- Constant full-catalog recomputation.

---

## 9. Operating environment and assumptions

### 9.1 Host

Target:

- Raspberry Pi 5.
- 8 GB RAM preferred.
- 64-bit Raspberry Pi OS or compatible Debian-based Linux.
- Reliable storage, preferably SSD rather than a heavily written microSD card.
- Correct system time through NTP.
- Always-on network connection, with graceful offline operation.

### 9.2 Station location

The application requires a configured observer/home location:

```text
latitude
longitude
altitude_m
display_name
```

The repository must not contain the operator’s real coordinates.

The initial private deployment is expected to be near Frederick, Maryland, but all source queries and calculations must use configuration rather than hard-coded coordinates.

### 9.3 SDR assignments

| Receiver | Service | Frequency | Decoder |
|---|---|---:|---|
| SDR 1 | ADS-B / Mode-S | 1090 MHz | `readsb` |
| SDR 2 | APRS | 144.39 MHz in North America | Dire Wolf |

Each receiver must be selected by stable USB serial or explicit device mapping.

### 9.4 Default area of interest

- Center: configured station position.
- Radius: 500 nautical miles.
- Adjustable: yes.
- Display filter: may be narrower than ingest area.
- Provider request subdivision: allowed and required when a provider caps request radius.
- Maximum allowed range: configurable.
- Global layers: allowed only when practical and explicitly selected.

### 9.5 Time

- Store timestamps in UTC.
- Display UTC and local time.
- Allow replay in either time basis.
- Clearly label provider event time, adapter receive time, and server ingest time.
- Show time synchronization health.

---

## 10. Data-source capability matrix

| Source | Transport | Default mode | Geographic filtering | Credentials | Core record type |
|---|---|---|---|---|---|
| Local ADS-B | `readsb` JSON or network output | Continuous | RF coverage | No | Track |
| Network ADS-B | Provider REST API | Polling | Radius/tiled radius | Provider-dependent | Track |
| Local APRS | Dire Wolf KISS/AGW | Continuous | RF coverage | No | Track/Event |
| APRS iGate | Dire Wolf to APRS-IS | Continuous outbound Internet | RF-heard packets | Callsign/passcode | Gateway action/status |
| APRS-IS display | TCP client | Continuous | Server-side radius/viewport filters | Callsign/passcode | Track/Event |
| AIS | AISStream WebSocket | Continuous | Bounding box | API key | Track/Event |
| SondeHub | Presigned MQTT WebSocket or REST | Streaming preferred | Radius/client filtering | No or provider-defined | Track/Event |
| Lightning | NOAA GOES GLM baseline | Near-real-time file ingestion | Client-side AOI | No | GeoFeature |
| Fire detections | NASA FIRMS API | Polling | Bounding box | Free map key | GeoFeature/Event |
| Earthquakes | USGS GeoJSON | Polling | Feed or API spatial query | No | GeoFeature/Event |
| TFR | FAA TFR XML/detail data | Polling | Local spatial filtering | No | GeoFeature/Event |
| Geospatial NOTAM | FAA-supported interface | Polling/streaming | Query parameters | Registration/key may be required | GeoFeature/Event |
| Orbital objects | CelesTrak GP data | Periodic sync + local propagation | Observer visibility/filtering | No | Predicted Track/Event |

---

## 11. Functional requirements

### 11.1 Core COP

**COP-FR-001**  
The application shall display all enabled geospatial sources on one MapLibre map.

**COP-FR-002**  
The application shall distinguish moving tracks, static points, polygons, lines, and predicted orbital tracks.

**COP-FR-003**  
The application shall support independent layer visibility controls.

**COP-FR-004**  
The application shall support filter combinations without requiring source restart.

**COP-FR-005**  
The application shall preserve a stable selection when a selected entity receives updates.

**COP-FR-006**  
The application shall show source age and health.

**COP-FR-007**  
The application shall support a tracks-of-interest watchlist.

**COP-FR-008**  
The application shall support user-created geofences.

**COP-FR-009**  
The application shall support map viewport, station-radius, source, type, age, altitude, speed, classification, and watchlist filters where applicable.

**COP-FR-010**  
The application shall restore the operator’s last saved view and display preferences.

### 11.2 Local ADS-B

**ADSB-FR-001**  
The system shall ingest local `readsb` aircraft data.

**ADSB-FR-002**  
The system shall preserve local reception metadata such as last-seen age, message count, signal/RSSI where available, position source, and receiver identity.

**ADSB-FR-003**  
The system shall identify 7500, 7600, and 7700 squawk values as alertable conditions.

**ADSB-FR-004**  
The UI shall label these values as reported squawks, not independently verified emergencies.

**ADSB-FR-005**  
A locally received aircraft shall display a prominent but restrained local-RF indicator.

**ADSB-FR-006**  
The system shall retain local receiver health and decoder statistics.

### 11.3 External aircraft network

**NETADSB-FR-001**  
The system shall provide a network-aircraft adapter interface.

**NETADSB-FR-002**  
The initial preferred adapter shall use an ADS-B Exchange v2-compatible open provider, with `adsb.fi` as the default candidate and `adsb.lol` or OpenSky as fallback implementations.

**NETADSB-FR-003**  
The provider shall be selectable through configuration.

**NETADSB-FR-004**  
Provider request limits shall be respected.

**NETADSB-FR-005**  
When a provider limits a radius request below the configured 500 NM area, the adapter shall divide the area into overlapping compliant queries and deduplicate returned aircraft.

**NETADSB-FR-006**  
The system shall not poll faster than the provider permits.

**NETADSB-FR-007**  
The system shall back off on HTTP 429, 5xx responses, timeouts, or provider maintenance.

### 11.4 Aircraft fusion

**FUSION-FR-001**  
Local and network aircraft with the same reliable ICAO identity shall appear as one track.

**FUSION-FR-002**  
Fresh local dynamic fields shall take priority.

**FUSION-FR-003**  
Network fields may fill data absent from the local observation.

**FUSION-FR-004**  
When local data becomes stale, the fused track may continue from a fresh network observation.

**FUSION-FR-005**  
The UI shall display all contributing sources and the active source for each important field.

**FUSION-FR-006**  
Non-ICAO, randomized, or ambiguous identities shall not be merged without a reliable correlation key.

**FUSION-FR-007**  
Fusion decisions shall be deterministic and testable.

### 11.5 Military Mode-S classification

**MIL-FR-001**  
The system shall support provider-reported military classification.

**MIL-FR-002**  
The system shall support configurable known military ICAO address-block rules.

**MIL-FR-003**  
The system shall show classification basis:

- Provider-reported.
- Address-block match.
- Both.
- Unknown.

**MIL-FR-004**  
No movement, callsign, route, or appearance heuristic shall classify an aircraft as military in v1.

**MIL-FR-005**  
The UI shall avoid certainty language where classification is not authoritative.

### 11.6 Local APRS and iGate

**APRS-FR-001**  
Dire Wolf shall decode local APRS RF traffic.

**APRS-FR-002**  
The dashboard adapter shall consume Dire Wolf KISS or AGW output.

**APRS-FR-003**  
Dire Wolf’s built-in iGate function shall forward valid eligible RF packets to APRS-IS.

**APRS-FR-004**  
The project shall not implement custom packet-gating logic when Dire Wolf can perform the function.

**APRS-FR-005**  
No Internet-to-RF path shall be configured.

**APRS-FR-006**  
No PTT, RF beacon, digipeating, or RF acknowledgement shall be configured.

**APRS-FR-007**  
The system shall display position packets, objects/items, messages, weather, and telemetry when parsable.

**APRS-FR-008**  
Malformed APRS packets shall not terminate the adapter.

**APRS-FR-009**  
The dashboard shall display APRS path, symbol, comment, packet type, and local reception metadata where available.

**APRS-FR-010**  
The iGate status shall include APRS-IS connection state and counts of eligible, gated, rejected, duplicate, and malformed packets where Dire Wolf exposes them.

### 11.7 APRS-IS display

**APRSIS-FR-001**  
A separate APRS-IS client shall ingest network APRS traffic for display.

**APRSIS-FR-002**  
The client shall use server-side filters.

**APRSIS-FR-003**  
The default filter shall cover the configured station area of interest.

**APRSIS-FR-004**  
The backend may update the APRS-IS filter from a debounced viewport request, bounded by configured maximum area and rate limits.

**APRSIS-FR-005**  
Local and Internet observations of the same callsign or object shall fuse.

**APRSIS-FR-006**  
The UI shall show whether the latest packet was heard locally, received only through APRS-IS, or both.

### 11.8 AIS

**AIS-FR-001**  
The system shall connect to AISStream through WebSocket.

**AIS-FR-002**  
The subscription shall use geographic bounding boxes covering the configured area.

**AIS-FR-003**  
The adapter shall merge vessel dynamic reports and static/voyage data by MMSI.

**AIS-FR-004**  
The adapter shall support common Class A, Class B, static, safety, and long-range message categories needed for the COP.

**AIS-FR-005**  
The UI shall support vessel-type, navigational-status, speed, name, MMSI, destination, and watchlist filters.

**AIS-FR-006**  
AIS data shall be labeled as reported broadcasts and not guaranteed navigation truth.

### 11.9 SondeHub

**SONDE-FR-001**  
The system shall ingest active radiosonde telemetry.

**SONDE-FR-002**  
Streaming through the SondeHub presigned MQTT WebSocket shall be preferred when stable.

**SONDE-FR-003**  
REST polling shall be available as a fallback.

**SONDE-FR-004**  
The system shall filter sondes by configured area and recency.

**SONDE-FR-005**  
The track shall expose serial, sonde type, altitude, speed, heading, vertical rate, ascent/descent state, last uploader, and prediction data when available.

**SONDE-FR-006**  
The UI shall show predicted landing points as predictions, not observations.

**SONDE-FR-007**  
A configurable alert shall support descending balloons within a selected radius or predicted landing area.

### 11.10 Lightning

**LIGHTNING-FR-001**  
The source architecture shall support multiple lightning providers.

**LIGHTNING-FR-002**  
The open baseline shall use NOAA GOES Geostationary Lightning Mapper Level 2 data when the Pi resource benchmark is acceptable.

**LIGHTNING-FR-003**  
NOAA GLM data shall be labeled as total-lightning flash observations.

**LIGHTNING-FR-004**  
The UI shall not label GLM flashes as confirmed cloud-to-ground strikes.

**LIGHTNING-FR-005**  
The adapter shall download or consume only the newest required data and discard observations outside the configured AOI before publishing.

**LIGHTNING-FR-006**  
The map shall cluster or aggregate lightning at lower zoom levels.

**LIGHTNING-FR-007**  
The source interface shall allow a future credentialed point-strike provider without changing the core schema.

### 11.11 NASA FIRMS

**FIRMS-FR-001**  
The adapter shall query the NASA FIRMS Area API using a user-supplied map key.

**FIRMS-FR-002**  
The query shall use a bounding box around the configured AOI.

**FIRMS-FR-003**  
The adapter shall use near-real-time VIIRS data by default, with source selection configurable.

**FIRMS-FR-004**  
The map shall label records as active-fire or thermal-anomaly detections.

**FIRMS-FR-005**  
The system shall not call each detection a confirmed wildfire.

**FIRMS-FR-006**  
Duplicate detections across sensors or repeated queries shall be handled deterministically.

**FIRMS-FR-007**  
The adapter shall honor FIRMS transaction limits and cache responses.

### 11.12 USGS earthquakes

**USGS-FR-001**  
The adapter shall use USGS GeoJSON feeds or the official catalog API.

**USGS-FR-002**  
The system shall poll no more frequently than the source update cadence.

**USGS-FR-003**  
The map shall display magnitude, depth, time, review status, significance, felt reports, tsunami flag, and alert level when available.

**USGS-FR-004**  
USGS event IDs shall be used for deduplication and updates.

**USGS-FR-005**  
The system shall support configurable magnitude and distance alert thresholds.

### 11.13 TFRs and NOTAMs

**AIRSPACE-FR-001**  
The system shall ingest current FAA TFR records and geometry from official FAA sources.

**AIRSPACE-FR-002**  
TFRs shall be represented as time-bounded geospatial features.

**AIRSPACE-FR-003**  
The system shall ingest other NOTAMs only when obtained through a permitted official interface.

**AIRSPACE-FR-004**  
Only NOTAMs with usable geometry shall be drawn as map geometry.

**AIRSPACE-FR-005**  
Non-geometric NOTAMs associated with facilities in the selected area may appear in a filtered textual panel.

**AIRSPACE-FR-006**  
The system shall retain the original NOTAM text and source reference.

**AIRSPACE-FR-007**  
The UI shall clearly state that the display is not a flight-planning product.

**AIRSPACE-FR-008**  
The NOTAM adapter may remain disabled until the operator obtains required FAA credentials or access.

### 11.14 Orbital objects

**ORBIT-FR-001**  
The system shall retrieve CelesTrak general perturbations data.

**ORBIT-FR-002**  
The implementation shall support current JSON or CSV formats and shall not depend exclusively on legacy fixed-width TLE format.

**ORBIT-FR-003**  
A maintained SGP4 implementation shall propagate positions locally.

**ORBIT-FR-004**  
The system shall cache orbital elements and expose their age.

**ORBIT-FR-005**  
The system shall not request data more frequently than needed.

**ORBIT-FR-006**  
The catalog shall support all available categories with filters for:

- Active payloads.
- Crewed spacecraft.
- Weather satellites.
- Amateur-radio satellites.
- Navigation constellations.
- Publicly cataloged military/government objects.
- Rocket bodies.
- Debris.
- Owner/operator.
- Constellation.
- Launch year.
- Watchlist.

**ORBIT-FR-007**  
The default live display shall show objects above a configurable observer elevation threshold, initially 10 degrees.

**ORBIT-FR-008**  
The operator shall be able to adjust the elevation threshold.

**ORBIT-FR-009**  
The system shall precompute upcoming passes for watched or selected categories.

**ORBIT-FR-010**  
The UI shall label orbital positions as predicted.

**ORBIT-FR-011**  
The system shall not propagate every cataloged object at one-second cadence.

### 11.15 History and replay

**HISTORY-FR-001**  
The system shall target 30 days of rolling history.

**HISTORY-FR-002**  
Retention shall be constrained by configured database size and free-disk limits.

**HISTORY-FR-003**  
The system shall use source-specific sampling and downsampling.

**HISTORY-FR-004**  
The system shall preserve alerts, significant events, and source outages at higher fidelity than ordinary track points.

**HISTORY-FR-005**  
The UI shall provide a shared replay timeline across enabled sources.

**HISTORY-FR-006**  
Replay mode shall be visually distinct from live mode.

**HISTORY-FR-007**  
The UI shall support pause, play, speed, step, jump-to-time, and return-to-live.

### 11.16 Alerts and notifications

**ALERT-FR-001**  
Alert rules shall be editable through the UI.

**ALERT-FR-002**  
Rules shall be stored locally.

**ALERT-FR-003**  
Rules shall support enable/disable, severity, source scope, conditions, geofence, schedule, cooldown, deduplication, and channels.

**ALERT-FR-004**  
Supported channels shall include:

- Dashboard alert center.
- In-browser notification while the application is connected.
- Optional sound.
- Email through configured SMTP.
- Discord webhook.

**ALERT-FR-005**  
Secrets shall never be returned to the browser.

**ALERT-FR-006**  
An alert shall have lifecycle states:

- Open.
- Acknowledged.
- Resolved.
- Suppressed.
- Delivery failed.

**ALERT-FR-007**  
The system shall prevent alert storms through transition detection, cooldowns, and per-rule deduplication.

**ALERT-FR-008**  
Rule templates shall be provided but may be disabled by default.

---

## 12. Default alert-rule templates

The application shall include editable templates for:

1. Aircraft squawk 7500.
2. Aircraft squawk 7600.
3. Aircraft squawk 7700.
4. Locally received aircraft reported or address-classified as military.
5. Watchlisted aircraft received locally.
6. Aircraft enters or exits a geofence.
7. Aircraft descends below a configured altitude inside a geofence.
8. APRS emergency packet.
9. APRS watchlisted callsign heard locally.
10. New APRS message to a configured callsign.
11. Lightning flash within a configured radius.
12. Lightning rate exceeds a configured count and time window.
13. New FIRMS detection within a configured radius.
14. Earthquake above magnitude threshold and within distance threshold.
15. New TFR intersecting a configured geofence.
16. Existing TFR becomes active.
17. Watched satellite rises above configured elevation.
18. Watched satellite reaches maximum elevation.
19. Watched satellite pass ends.
20. Balloon enters descent within a configured radius.
21. Balloon predicted landing point enters a geofence.
22. AIS vessel with configured MMSI enters a geofence.
23. AIS distress-related or configured safety message.
24. Source offline or stale beyond a configured duration.
25. Database disk budget warning.
26. System time synchronization warning.

Templates must use conservative wording and never imply more certainty than the underlying data.

---

## 13. System architecture

### 13.1 Logical architecture

```text
                         LOCAL RADIO PATHS

 RTL-SDR #1 ── readsb ───────────────┐
                                     │
 RTL-SDR #2 ── Dire Wolf ────────────┼── source adapters
                    │                │
                    └── APRS-IS iGate│
                                     │
                         INTERNET SOURCES
                                     │
 ADS-B provider ─────────────────────┤
 APRS-IS display feed ───────────────┤
 AISStream ──────────────────────────┤
 SondeHub ───────────────────────────┤
 NOAA GOES GLM ──────────────────────┤
 NASA FIRMS ─────────────────────────┤
 USGS ───────────────────────────────┤
 FAA TFR / NOTAM ────────────────────┤
 CelesTrak ──────────────────────────┘
                                     │
                                     ▼
                      normalized schema v2 records
                                     │
                                     ▼
                          Mosquitto message bus
                                     │
                         ┌───────────┴───────────┐
                         ▼                       ▼
                  fusion/live state        persistence writer
                  alert evaluation         SQLite WAL database
                         │                       │
                         └───────────┬───────────┘
                                     ▼
                          FastAPI REST + WebSocket
                                     │
                                     ▼
                      React + TypeScript + MapLibre
                                     │
                                     ▼
                           Tailscale Serve HTTPS/WSS
```

### 13.2 Process boundaries

Recommended long-running processes:

- Mosquitto.
- `readsb`.
- Dire Wolf.
- One adapter process per source.
- FastAPI backend.
- Persistence writer may be an internal backend task initially.
- Frontend served as static assets by the backend or a small local web server.
- Tailscale Serve.

Every adapter must be restartable without restarting the backend.

### 13.3 Why MQTT remains

MQTT provides:

- Decoder and Internet-source isolation.
- Simple source-specific topics.
- Replay/test publishers.
- Independent restart.
- Low operational burden on a Pi.
- Future local consumers.
- Clear observability.

The backend remains the authoritative fused-state owner. MQTT retained track records are not the source of truth.

---

## 14. Normalized schema v2

The earlier `Entity` and `Event` model is insufficient for polygonal airspace, temporal environmental features, predicted orbital objects, alert lifecycle, and source health.

Schema v2 shall use a discriminated record union.

### 14.1 Common record fields

```python
class RecordBase(BaseModel):
    schema_version: Literal[2] = 2
    kind: str
    id: str
    source: str
    observed_at: datetime
    received_at: datetime
    published_at: datetime
    correlation_key: str | None = None
    provenance: list[Provenance] = []
    tags: list[str] = []
    attributes: dict[str, Any] = {}
```

Rules:

- All timestamps are timezone-aware UTC.
- `observed_at` is the source event time.
- `received_at` is adapter receipt time.
- `published_at` is normalized-record publication time.
- `id` is stable and namespaced.
- `correlation_key` is used for fusion.
- `attributes` holds source-native fields not promoted into the common schema.
- Oversized untrusted payloads are rejected or truncated.

### 14.2 Provenance

```python
class Provenance(BaseModel):
    source: str
    provider: str | None
    receiver_id: str | None
    observed_at: datetime
    received_at: datetime
    local_rf: bool = False
    derived: bool = False
    confidence: Literal["high", "medium", "low", "unknown"] = "unknown"
    fields: list[str] = []
```

### 14.3 Track record

Represents a moving or potentially moving object.

```python
class TrackRecord(RecordBase):
    kind: Literal["track"] = "track"
    track_type: Literal[
        "aircraft",
        "vessel",
        "aprs_station",
        "aprs_object",
        "radiosonde",
        "orbital_object",
        "other"
    ]
    label: str | None
    geometry: GeoJSONPoint | None
    altitude_m: float | None
    speed_mps: float | None
    heading_deg: float | None
    vertical_rate_mps: float | None
    locally_received: bool
    classification: Classification | None
    valid_until: datetime | None
    predicted: bool = False
```

### 14.4 GeoFeature record

Represents a point, line, polygon, or multi-geometry with a validity window.

```python
class GeoFeatureRecord(RecordBase):
    kind: Literal["feature"] = "feature"
    feature_type: Literal[
        "lightning_flash",
        "lightning_cluster",
        "fire_detection",
        "earthquake",
        "tfr",
        "notam_geometry",
        "predicted_landing",
        "geofence",
        "other"
    ]
    geometry: GeoJSONGeometry
    valid_from: datetime | None
    valid_until: datetime | None
    severity: str | None
    label: str | None
```

### 14.5 Event record

```python
class EventRecord(RecordBase):
    kind: Literal["event"] = "event"
    event_type: str
    subject_id: str | None
    summary: str
    message: str | None
    geometry: GeoJSONGeometry | None
    severity: str | None
```

### 14.6 Alert record

```python
class AlertRecord(RecordBase):
    kind: Literal["alert"] = "alert"
    rule_id: str
    subject_id: str | None
    state: Literal["open", "acknowledged", "resolved", "suppressed", "delivery_failed"]
    severity: Literal["info", "low", "medium", "high", "critical"]
    title: str
    summary: str
    triggered_at: datetime
    acknowledged_at: datetime | None
    resolved_at: datetime | None
    delivery_status: dict[str, str]
```

### 14.7 Source-status record

```python
class SourceStatusRecord(RecordBase):
    kind: Literal["source_status"] = "source_status"
    status: Literal["starting", "connected", "degraded", "stale", "offline", "disabled"]
    last_success_at: datetime | None
    last_record_at: datetime | None
    lag_s: float | None
    records_received: int
    records_rejected: int
    error_code: str | None
    error_summary: str | None
```

### 14.8 Canonical units

- Latitude/longitude: decimal degrees, WGS 84.
- Altitude: meters.
- Speed: meters per second.
- Vertical rate: meters per second.
- Heading/course: degrees clockwise from true north.
- Distance: meters internally.
- Radius display: nautical miles, statute miles, or kilometers by user preference.
- Time: UTC internally.
- Frequency: Hz.
- Energy: source-native value with explicit unit metadata unless normalized safely.

---

## 15. Identity, correlation, and fusion

### 15.1 Stable identity keys

Recommended keys:

```text
aircraft:icao:<hex>
aircraft:other:<provider-id>
aprs:station:<CALLSIGN-SSID>
aprs:object:<OBJECT-NAME>
ais:vessel:<MMSI>
sonde:<SERIAL>
orbit:norad:<catalog-number>
earthquake:usgs:<event-id>
tfr:faa:<notam-id>
notam:faa:<notam-id>
firms:<sensor>:<acq-time>:<rounded-position>
lightning:glm:<satellite>:<flash-id>:<start-time>
```

### 15.2 Aircraft field precedence

Suggested default:

1. Fresh local `readsb` dynamic observation.
2. Fresh network dynamic observation.
3. Cached prior fused value within field TTL.
4. Null.

Metadata may use provider/database values even while local dynamics are active.

### 15.3 APRS field precedence

1. Fresh local RF packet.
2. Fresh APRS-IS packet.
3. Cached prior value within APRS TTL.

A packet’s RF path and Internet path shall remain separate provenance entries.

### 15.4 Freshness windows

Freshness is source and field specific.

Example defaults, configurable:

| Source | Live | Stale | Expire |
|---|---:|---:|---:|
| Local ADS-B | 0–5 s | 5–30 s | 60 s |
| Network ADS-B | 0–15 s | 15–60 s | 120 s |
| AIS dynamic | 0–60 s | 1–10 min | 30 min |
| APRS mobile | 0–5 min | 5–30 min | 2 h |
| APRS fixed/weather | 0–30 min | 30 min–6 h | 24 h |
| Sonde | 0–2 min | 2–10 min | 30 min |
| Lightning | Provider-specific | Provider-specific | 2 h default |
| FIRMS | Based on acquisition time | 24–48 h | Configurable |
| Orbital object | Predicted | Element-age warning | Element-age cutoff |

These are initial product defaults, not universal protocol truths.

### 15.5 Conflict handling

When sources disagree:

- Preserve each observation.
- Choose a fused value deterministically.
- Record the winning source for the field.
- Expose the conflict in debug details when materially different.
- Never average positions from unrelated observation times.
- Never merge uncertain identities merely because positions are close.

---

## 16. Geographic filtering and area management

### 16.1 Area-of-interest model

The backend shall manage:

- A persistent default station-centered circle.
- Optional saved circles.
- Optional saved polygons.
- Current browser viewport.
- Alert geofences.
- Source-specific maximum areas.
- Provider-specific request subdivisions.

### 16.2 Default AOI

```text
center = configured station
radius = 500 NM
```

The user may adjust the display and ingest radius.

### 16.3 Viewport-driven feeds

For sources that support dynamic subscriptions:

1. The browser sends a debounced viewport.
2. The backend validates the requested area.
3. The backend intersects it with configured maximum limits.
4. The adapter changes its upstream filter only when the change is material.
5. The backend retains a default station-centered subscription when no browser is connected.

### 16.4 Provider tiling

When an API accepts a smaller radius than the desired AOI:

- Generate a deterministic query grid.
- Add overlap to avoid edge gaps.
- Rate-limit the grid.
- Cache each tile.
- Deduplicate by stable identity.
- Avoid synchronized request bursts.
- Prefer fewer requests at lower zoom or when the UI is idle.

### 16.5 Client-side filtering

Display filters do not necessarily change upstream ingestion.

The UI shall support:

- Source.
- Track/feature type.
- Range.
- Altitude.
- Speed.
- Age.
- Local-only.
- Network-only.
- Military classification basis.
- Squawk.
- APRS packet type.
- Vessel type/status.
- Magnitude.
- Fire confidence.
- Satellite category/elevation.
- Alert state/severity.
- Watchlist membership.

---

## 17. Adapter framework

### 17.1 Common adapter contract

Each adapter shall implement:

```python
async def records(self) -> AsyncIterator[Record]:
    ...
```

The adapter runner owns:

- Startup.
- Configuration validation.
- Source connection.
- Retry with jittered exponential backoff.
- MQTT connection.
- Serialization.
- Source status publication.
- Graceful shutdown.
- Metrics.
- Log context.

### 17.2 Adapter rules

Every adapter must:

- Validate source responses.
- Reject impossible coordinates.
- Normalize timestamps.
- Apply payload-size limits.
- Handle partial messages.
- Avoid clearing known fields with missing values.
- Respect upstream terms and rate limits.
- Use conditional requests where available.
- Back off on provider errors.
- Publish source status.
- Include fixture data.
- Include parser tests.
- Avoid live Internet calls in ordinary CI.
- Be optional.

### 17.3 Streaming adapters

Streaming adapters must:

- Detect silent/stalled connections.
- Use heartbeats or last-message timeouts.
- Reconnect with jitter.
- Resubscribe after reconnect.
- Avoid duplicate processing after reconnect.
- Bound internal buffers.
- Drop superseded position updates before dropping important events.

### 17.4 Polling adapters

Polling adapters must:

- Add request jitter.
- Cache ETag and Last-Modified.
- Use timeouts.
- Avoid overlapping polls.
- Honor Retry-After.
- Track source lag.
- Keep last successful state during temporary failure while marking it stale.

---

## 18. Source-specific architecture

### 18.1 Local ADS-B adapter

Preferred initial input:

- `readsb` `aircraft.json` for simple one-second snapshots and richer fields.
- Optional Beast/SBS input may remain for lower-latency or compatibility.

The adapter shall:

- Read snapshots atomically.
- Merge partial records.
- Normalize units.
- Preserve `seen`, `seen_pos`, message count, RSSI, position type, squawk, emergency state, category, registration/type metadata, and database flags when available.
- Publish at most one ordinary update per aircraft per configured interval unless a critical field changes.
- Immediately publish emergency-squawk transitions.
- Publish receiver health from `receiver.json` or statistics output when available.

### 18.2 Network ADS-B provider interface

```python
class AircraftProvider(Protocol):
    async def fetch_region(self, region: GeoRegion) -> list[AircraftObservation]:
        ...
```

Initial implementations:

- `AdsbFiProvider`.
- `AdsbLolProvider`.
- `OpenSkyProvider`.

Selection policy:

- Prefer the provider that offers stable open access and sufficient fields.
- Keep the response parser provider-specific.
- Convert all providers into the same observation model.
- Do not let provider-specific fields leak into core fusion logic except through typed capabilities or `attributes`.

### 18.3 APRS local adapter and iGate

Dire Wolf responsibilities:

- Audio demodulation.
- AX.25/APRS decoding.
- CRC validation.
- APRS-IS login.
- Eligible RF-to-Internet packet gating.
- Duplicate and loop protections provided by Dire Wolf/APRS-IS behavior.

`aether` responsibilities:

- Consume KISS or AGW output for display.
- Parse AX.25 and APRS fields as needed.
- Normalize track and event records.
- Read logs or status where practical for iGate health.
- Never send packets back to KISS/AGW for transmission.

Configuration shall require explicit receive-only iGate settings.

### 18.4 APRS-IS display adapter

The APRS-IS adapter shall:

- Connect to a Tier 2 rotate address or configured server.
- Use the user’s callsign and passcode.
- Use port 14580 or current recommended filtered port.
- Apply a server-side geographic filter.
- Parse TNC2 lines.
- Ignore server comments except for health/status.
- Deduplicate packets.
- Avoid re-gating APRS-IS packets to APRS-IS.
- Mark observations as network-only unless matched with local RF provenance.

### 18.5 AIS adapter

The AIS adapter shall:

- Connect to AISStream’s secure WebSocket.
- Send the API key and one or more bounding boxes.
- Reconnect and resubscribe.
- Parse supported message envelopes.
- Merge dynamic and static vessel fields by MMSI.
- Maintain separate update TTLs for fast dynamic and slow static data.
- Preserve message type and source timestamp.
- Avoid displaying stale static data as a recent position.

### 18.6 SondeHub adapter

Preferred path:

1. Obtain a presigned SondeHub MQTT WebSocket URL.
2. Subscribe to active telemetry.
3. Filter by AOI and recency.
4. Query REST for history or missed data after reconnect.

Fallback path:

- Poll `/sondes` with latitude, longitude, distance, and recency.
- Query individual sonde history when selected.
- Query landing predictions only for selected or watched sondes.

### 18.7 Lightning adapter

NOAA GLM baseline workflow:

```text
NOAA Open Data object listing
  → newest GLM Level 2 files
  → download once
  → parse flash records
  → AOI filter
  → normalize
  → cluster for display
```

Requirements:

- Benchmark CPU, storage, network, and parser memory on the Pi before enabling continuous operation.
- Use GOES-East appropriate to the configured region.
- Track file and satellite identity to prevent duplicate ingestion.
- Retain flash-level records only as permitted by the storage budget.
- Aggregate older observations into time/space bins.
- Provide a provider abstraction for future commercial or participant lightning feeds.

### 18.8 FIRMS adapter

Workflow:

```text
configured AOI
  → bounding box
  → FIRMS Area API
  → selected NRT sensor datasets
  → parse CSV
  → normalize acquisition date/time
  → deduplicate
  → publish feature/event
```

Do not query the entire world for the private station use case.

### 18.9 USGS adapter

Preferred workflow:

- Use an appropriate recent GeoJSON feed for low-cost updates.
- Spatially filter locally.
- Use the official query API when radius or historical selection requires it.
- Update existing events when USGS updates the event record.
- Preserve source URL and detail endpoint.

### 18.10 FAA TFR adapter

Workflow:

- Fetch the official FAA TFR list.
- Detect new, changed, cancelled, and expired records.
- Fetch record detail/XML.
- Parse geometry and altitude/time metadata.
- Normalize to GeoJSON.
- Validate polygon/radius geometry.
- Preserve original text and identifiers.
- Mark malformed geometry as a textual event instead of inventing a shape.

### 18.11 FAA NOTAM adapter

The NOTAM adapter shall be capability-gated.

States:

- Disabled: no credentials/access.
- Configured: credentials present but not tested.
- Connected.
- Degraded.
- Unauthorized.

It shall:

- Use official FAA-supported access.
- Query by location, time, classification, and geometry when supported.
- Draw only explicit or reliably supplied geometry.
- Preserve text.
- Avoid ad hoc natural-language geometry guessing in v1.
- Use a textual facility panel for non-geometric records.

### 18.12 CelesTrak orbital adapter

Data path:

```text
CelesTrak GP JSON/CSV
  → local orbital-element cache
  → metadata/category index
  → pass candidate generation
  → SGP4 propagation
  → observer azimuth/elevation/range
  → predicted track records
```

Performance strategy:

- Sync element sets on a conservative cadence.
- Precompute candidate passes.
- Propagate selected/watchlisted/currently visible objects at high cadence.
- Propagate broader categories at lower cadence.
- Do not send off-screen or below-horizon objects unless explicitly requested.
- Cache pass predictions.
- Recompute when elements or observer settings change.

---

## 19. Live state, persistence, and retention

### 19.1 Live state

The backend shall hold current fused state in memory:

```text
tracks
features
recent events
open alerts
source status
revision/sequence
```

### 19.2 SQLite

SQLite shall use:

- WAL mode.
- Versioned migrations.
- Foreign keys.
- A single bounded async write queue.
- Batch inserts.
- Indexed time and entity queries.
- Incremental vacuum strategy where appropriate.
- Backup instructions.

### 19.3 Suggested tables

```text
schema_migrations
tracks_current
features_current
observations
events
alerts
alert_deliveries
alert_rules
geofences
watchlist
source_status_samples
orbital_elements
orbital_passes
saved_views
settings
daily_aggregates
```

### 19.4 Retention policy

Target:

- 30 days.

Constraints:

- `AETHER_DB_MAX_GB`.
- `AETHER_MIN_FREE_DISK_GB`.
- Per-source retention.
- Per-source sampling.
- High-water and critical-water marks.

When storage pressure occurs:

1. Delete expired temporary/raw diagnostic payloads.
2. Downsample old high-rate track observations.
3. Delete old low-value source-health samples.
4. Delete oldest ordinary observations before deleting alerts or major events.
5. Shorten effective retention below 30 days if necessary.
6. Emit a system alert and health warning.

### 19.5 Sampling defaults

Initial configurable guidance:

- Local ADS-B: persist one point every 5 seconds while moving; immediate on significant state change.
- Network ADS-B: one point every 15 seconds.
- Watchlisted/emergency aircraft: higher fidelity while alert is active.
- AIS: one point every 30 seconds unless state changes.
- APRS: persist every unique packet/event; collapse repeated identical positions where appropriate.
- Sonde: persist each meaningful telemetry update.
- Lightning: flash-level short retention, then spatial/temporal aggregates.
- FIRMS: persist unique detections and updates.
- Earthquakes: persist each event and revision.
- TFR/NOTAM: persist lifecycle revisions.
- Satellites: persist elements and pass events; reconstruct ordinary positions instead of storing every propagated point.

### 19.6 Replay

Replay shall query normalized history and emit a virtual state stream.

Replay must:

- Have an independent sequence domain or explicit replay session ID.
- Not trigger live notification delivery.
- Optionally show which historical alerts occurred.
- Display a visible “REPLAY” banner.
- Avoid mutating current live state.

---

## 20. Alert engine design

### 20.1 Rule model

```json
{
  "id": "rule-aircraft-7700",
  "name": "Emergency squawk 7700",
  "enabled": true,
  "severity": "high",
  "subject_types": ["aircraft"],
  "condition": {
    "field": "squawk",
    "operator": "equals",
    "value": "7700"
  },
  "transition": "enter",
  "geofence_id": null,
  "cooldown_s": 900,
  "channels": ["dashboard", "browser", "email", "discord"],
  "schedule": null,
  "quiet_hours": null
}
```

### 20.2 Operators

Support:

- Equals/not equals.
- In/not in.
- Greater/less than.
- Changed to/from.
- Entered/exited geofence.
- Exists/does not exist.
- Source became stale/offline.
- Count within time window.
- Distance from station/geofence.
- Elevation threshold crossed.
- Classification basis.
- Local-RF true/false.
- Watchlist true/false.

### 20.3 Evaluation

The engine shall evaluate:

- Fused state transitions.
- New events.
- Source status transitions.
- Scheduled orbital pass events.
- Aggregated environmental windows.

The engine shall not trigger repeatedly on every unchanged update.

### 20.4 Notification delivery

Each delivery driver shall be independent.

#### Dashboard

- Immediate alert-center entry.
- Toast for high-priority events.
- Optional sound.
- Persistent acknowledgement state.

#### Browser

MVP:

- Use the browser Notifications API while the application is open and connected.
- Request permission only after explicit user action.
- Do not repeatedly prompt.

Future optional enhancement:

- Background Web Push with VAPID.
- Must be documented as involving browser push infrastructure rather than being purely local.

#### Email

- SMTP host, port, TLS mode, sender, recipient, and credentials from environment/secrets file.
- Retry transient errors.
- Store delivery result without storing the SMTP password.

#### Discord

- Webhook URL from environment/secrets file.
- Use concise embeds/messages.
- Retry rate-limited or transient failures.
- Redact webhook URLs from logs and API responses.

### 20.5 Alert hygiene

- Cooldowns.
- Grouping.
- Dedup keys.
- State transitions.
- Acknowledgement.
- Auto-resolution where possible.
- Quiet hours.
- Per-channel severity threshold.
- Test-notification function.
- Rule preview against recorded sample data.

---

## 21. Backend API

All routes are versioned under `/api/v2`.

### 21.1 Health and sources

```http
GET /api/v2/health
GET /api/v2/sources
GET /api/v2/sources/{source_id}
POST /api/v2/sources/{source_id}/test
```

Health includes:

- Application uptime.
- MQTT state.
- Database state and size.
- Free disk.
- Current sequence.
- Track/feature/event counts.
- WebSocket clients.
- Source status and last success.
- NTP/time warning if detectable.
- Alert-delivery driver status.

### 21.2 Live state

```http
GET /api/v2/state
GET /api/v2/tracks
GET /api/v2/features
GET /api/v2/events
GET /api/v2/alerts
```

Common query parameters:

- `bbox`.
- `center`.
- `radius_nm`.
- `sources`.
- `types`.
- `updated_after`.
- `age_max_s`.
- `limit`.
- `cursor`.

### 21.3 Details and history

```http
GET /api/v2/tracks/{track_id}
GET /api/v2/tracks/{track_id}/history
GET /api/v2/features/{feature_id}
GET /api/v2/events/{event_id}
GET /api/v2/alerts/{alert_id}
```

### 21.4 Alerts

```http
GET    /api/v2/alert-rules
POST   /api/v2/alert-rules
PATCH  /api/v2/alert-rules/{rule_id}
DELETE /api/v2/alert-rules/{rule_id}
POST   /api/v2/alert-rules/{rule_id}/test
POST   /api/v2/alerts/{alert_id}/acknowledge
POST   /api/v2/alerts/{alert_id}/resolve
POST   /api/v2/notifications/test
```

### 21.5 Geofences, watchlists, and views

```http
GET/POST/PATCH/DELETE /api/v2/geofences
GET/POST/PATCH/DELETE /api/v2/watchlist
GET/POST/PATCH/DELETE /api/v2/saved-views
```

### 21.6 Replay and export

```http
POST /api/v2/replay/sessions
GET  /api/v2/replay/sessions/{session_id}
DELETE /api/v2/replay/sessions/{session_id}
GET  /api/v2/export
```

Export formats:

- GeoJSON.
- CSV.
- JSON.

Exports must be bounded by time, area, source, and maximum record count.

### 21.7 Settings

```http
GET /api/v2/settings
PATCH /api/v2/settings
GET /api/v2/integrations
```

The browser receives only:

- Integration enabled state.
- Credential configured/missing state.
- Last connection test.
- Capabilities.
- Setup guidance.

The browser never receives credential values.

---

## 22. WebSocket protocol

### 22.1 Connection

```http
GET /ws/v2
```

### 22.2 Client subscription

```json
{
  "type": "subscribe",
  "bbox": [-80.0, 35.0, -74.0, 42.0],
  "sources": ["local_adsb", "network_adsb", "aprs", "ais"],
  "track_types": ["aircraft", "vessel", "aprs_station"],
  "include_events": true,
  "include_alerts": true
}
```

`bbox` is GeoJSON order `[minLon, minLat, maxLon, maxLat]` (longitude first). `null`
`bbox` means unbounded; `null` `sources`/`track_types` means all. The client
debounces this frame (~300 ms) on viewport/filter change and re-sends it on every
(re)connect.

Server-side handling (per connection, M3.6b — display-stream filtering only, PRD
§16.3 interpretation (a)):

- Before any subscribe, the connection gets a **default station-scoped filter** from
  the canonical `AETHER_STATION_LAT`/`_LON`/`_RADIUS_NM`. An unconfigured 0,0 station
  degrades to **unbounded** (never a zero-area null-island box).
- A subscribe `bbox` is validated (4 finite WGS84 floats; `minLat <= maxLat`;
  `minLon > maxLon` allowed for an antimeridian viewport) and **intersected with the
  station max AOI** — a client may narrow but not widen beyond the station scope. A
  malformed subscribe is logged and the **prior filter kept**; it never drops the
  connection. The subscribe receive loop is rate-limited so subscribe-spam cannot
  starve the send loop.
- `matches`: `source_status` **always** passes (health reaches every client); alerts
  are gated by `include_alerts`; events by `include_events` AND (no geometry OR bbox
  hit); tracks by `source` ∈ `sources` AND `track_type` ∈ `track_types` AND (geometry
  `null` OR point-in-bbox — `null` passes so a just-acquired track isn't hidden);
  features by `source` AND geometry-intersects-bbox.
- A **track that moves out of the viewport** relies on the client's own staleness GC
  for now (no synthetic remove is emitted); a real `remove` for an id the connection
  was already sent is **force-forwarded** regardless of the filter so a filtered
  client never strands a ghost track.

### 22.3 Snapshot

```json
{
  "type": "snapshot",
  "seq": 1841,
  "cseq": 0,
  "tracks": [],
  "features": [],
  "events": [],
  "alerts": [],
  "source_status": []
}
```

A snapshot is filtered by the connection's current filter. Every subscribe (initial,
widened bbox, reconnect) is a **resync point**: the server re-anchors a fresh filtered
snapshot at the current global `seq` and resets `cseq` to 0. Because each subscribe
re-snapshots, a widened bbox backfills previously-filtered records for free.

### 22.4 Deltas

```json
{"type":"track_upsert","seq":1842,"cseq":1,"record":{}}
{"type":"feature_upsert","seq":1843,"cseq":2,"record":{}}
{"type":"event","seq":1844,"cseq":3,"record":{}}
{"type":"alert_upsert","seq":1845,"cseq":4,"record":{}}
{"type":"source_status","seq":1846,"cseq":5,"record":{}}
{"type":"remove","seq":1847,"cseq":6,"kind":"track","id":"aircraft:icao:abcdef"}
```

Every server→client frame carries **two** counters (M3.6b, additive — no
`schema_version` bump):

- `seq` — the **global** mutation counter (the REST/snapshot anchor). It bumps on
  every backend mutation, so a per-connection-filtered client legitimately sees it
  **skip**.
- `cseq` — a **per-connection contiguous** counter. A snapshot resets it to 0; each
  delta the server actually sends this connection increments it by exactly 1. Frames
  the connection's filter rejects receive **no** `cseq` (no false gap); a real
  drop-oldest under backpressure leaves a `cseq` gap exactly when frames were truly
  dropped.

### 22.5 Resynchronization

Clients gap-detect on **`cseq`** (not `seq` — a skipped `seq` is "filtered/expected",
a skipped `cseq` is "dropped/real"). If a client sees a `cseq` gap:

1. Stop applying deltas.
2. Mark display stale.
3. Request a new snapshot or reconnect (re-send the last subscribe).
4. Replace local authoritative state (the fresh snapshot re-anchors `cseq` to 0).
5. Resume.

### 22.6 Backpressure

Each client receives a bounded queue.

The server may:

- Coalesce superseded track updates.
- Coalesce source-health updates.
- Drop old noncritical map updates.
- Preserve alerts, removals, and important events.
- Disconnect a client that cannot recover.

One slow browser must not block ingestion.

---

## 23. MQTT topic design

Recommended:

```text
aether/v2/records/<source>
aether/v2/status/<source>
aether/v2/system/events
```

Examples:

```text
aether/v2/records/local_adsb
aether/v2/records/network_adsb
aether/v2/records/local_aprs
aether/v2/records/aprsis
aether/v2/records/ais
aether/v2/records/sondehub
aether/v2/records/firms
aether/v2/records/usgs
aether/v2/records/faa_tfr
aether/v2/records/celestrak
```

Delivery guidance:

- High-rate positions: QoS 0.
- Significant events and source status: QoS 1 where useful.
- Current source status may be retained.
- Track records are not retained by MQTT.
- MQTT is loopback-bound unless explicitly secured otherwise.

---

## 24. User interface and experience

### 24.1 Design direction

The interface shall be:

- Dark.
- Modern.
- Tactical in visual character.
- High-density but readable.
- Map-first.
- Restrained rather than theatrical.
- Responsive.
- Accessible.

Avoid:

- Fake CRT effects.
- Excessive neon.
- Constant animation.
- Decorative radar sweeps.
- Unreadable tiny text.
- Red/green-only status communication.
- Symbols that imply official identification.

### 24.2 Desktop layout

Recommended structure:

```text
┌──────────────────────────────────────────────────────────────┐
│ UTC / LOCAL │ LIVE/REPLAY │ source health │ alerts │ search │
├───────────────┬───────────────────────────────┬──────────────┤
│ Layers &      │                               │ Selection /  │
│ filters       │           MAP                 │ TOI details  │
│               │                               │ provenance   │
│ saved views   │                               │ history      │
├───────────────┴───────────────────────────────┴──────────────┤
│ Timeline / event feed / replay controls                      │
└──────────────────────────────────────────────────────────────┘
```

Panels shall be collapsible.

### 24.3 Map behavior

- Use GeoJSON sources and WebGL layers.
- Do not create one DOM marker per object.
- Rotate aircraft/vessel symbols by heading.
- Use clustering for dense point layers.
- Use level-of-detail rules.
- Show track trails only when selected, watched, or zoomed sufficiently.
- Show uncertainty/stale appearance.
- Use distinct styles for live observed versus predicted.
- Use hatching or transparency for future/inactive TFRs.
- Use age fade carefully without making old data invisible before expiration.

### 24.4 Track details

Aircraft detail should include:

- Callsign.
- ICAO hex.
- Registration/type where available.
- Altitude, speed, heading, vertical rate.
- Squawk.
- Emergency state.
- Military classification basis.
- Local RF last seen.
- Network last seen.
- Signal/RSSI when local.
- Contributing sources.
- Track history.
- Watchlist and alert actions.

APRS detail should include:

- Callsign/object.
- Position.
- Symbol.
- Comment.
- Course/speed.
- Packet type.
- Path.
- Local RF last heard.
- APRS-IS last heard.
- Messages/events.
- Watchlist and alert actions.

Vessel detail should include:

- Name.
- MMSI.
- Type.
- Navigational status.
- Course/speed/heading.
- Destination/ETA when available.
- Last update.
- Watchlist and geofence actions.

Orbital detail should include:

- Name.
- Catalog number.
- Object type.
- Owner/country metadata where available.
- Element epoch/age.
- Azimuth/elevation/range.
- Rise, culmination, and set.
- Maximum elevation.
- Predicted ground track.
- Watchlist.

### 24.5 Layer control

Each layer entry shall show:

- Name.
- Visibility.
- Live count.
- Health.
- Age.
- Filter-active indicator.
- Credential/setup warning.
- Last update.

### 24.6 Tracks of interest

TOI functions:

- Add/remove.
- Assign label.
- Assign priority.
- Choose alert template.
- Filter map to TOIs.
- Show last seen.
- Show local reception.
- Show upcoming satellite passes.
- Export history.

### 24.7 Search

Search shall cover:

- ICAO.
- Callsign.
- Registration.
- MMSI.
- Vessel name.
- APRS callsign/object.
- Sonde serial.
- NORAD catalog number.
- Satellite name.
- TFR/NOTAM identifier.
- USGS event.
- Coordinates.

### 24.8 Mobile/PWA

Mobile is supported but desktop is primary.

Mobile layout:

- Full-screen map.
- Bottom sheet for layers/details/events.
- Large alert acknowledgement controls.
- PWA manifest and app shell caching.
- Explicit disconnected/stale mode.
- No suggestion that cached data is current.

### 24.9 Accessibility

- Keyboard navigation for core controls.
- Color is not the only status channel.
- Scalable text.
- High contrast.
- Reduced-motion preference.
- Screen-reader labels.
- Symbols accompanied by text in detail panels.
- Time and units clearly labeled.

---

## 25. Configuration and secrets

### 25.1 Configuration layers

1. Safe code defaults.
2. Version-controlled non-secret sample configuration.
3. Local `.env` or environment file.
4. Local database user settings.
5. Browser-local display preferences where appropriate.

### 25.2 Example environment variables

```text
AETHER_HOME_LAT
AETHER_HOME_LON
AETHER_HOME_ALT_M
AETHER_HOME_NAME
AETHER_DEFAULT_RADIUS_NM=500

AETHER_MQTT_HOST=127.0.0.1
AETHER_MQTT_PORT=1883
AETHER_DB_PATH=/var/lib/aether/aether.sqlite
AETHER_DB_MAX_GB
AETHER_MIN_FREE_DISK_GB

AETHER_READSB_URL
AETHER_DIREWOLF_HOST
AETHER_DIREWOLF_PORT
AETHER_DIREWOLF_TRANSPORT

AETHER_APRS_CALLSIGN
AETHER_APRS_PASSCODE
AETHER_APRSIS_SERVER
AETHER_APRSIS_PORT=14580

AETHER_AIRCRAFT_PROVIDER=adsbfi
AETHER_AIRCRAFT_POLL_S

AETHER_AISSTREAM_API_KEY
AETHER_FIRMS_MAP_KEY

AETHER_FAA_NOTAM_API_KEY
AETHER_FAA_NOTAM_BASE_URL

AETHER_SMTP_HOST
AETHER_SMTP_PORT
AETHER_SMTP_USERNAME
AETHER_SMTP_PASSWORD
AETHER_EMAIL_FROM
AETHER_EMAIL_TO

AETHER_DISCORD_WEBHOOK_URL

AETHER_LOG_LEVEL
AETHER_WS_QUEUE_SIZE
```

### 25.3 Secret rules

- Never commit `.env`.
- Never log full secrets.
- Redact URLs containing secret tokens.
- Never send secrets to the frontend.
- Use file permissions appropriate for a service account.
- Integration-test endpoints return status, not credential content.
- GitHub issues and debug bundles must redact secrets and exact home coordinates by default.

---

## 26. Security and privacy

### 26.1 Network exposure

- Backend binds to loopback.
- MQTT binds to loopback.
- Tailscale Serve exposes HTTPS/WSS.
- No Funnel.
- No direct router port forwarding.
- No public listener in default examples.

### 26.2 Browser security

- Validate Host and Origin.
- Use secure cookies only if cookies are introduced.
- Apply CSRF protection to state-changing browser requests if cookie-based trust is used.
- Restrict CORS.
- Set CSP appropriate to map tiles and configured providers.
- Do not use `eval`.
- Sanitize source text before rendering.
- Treat callsigns, comments, NOTAM text, vessel names, and provider fields as untrusted.

### 26.3 Tailscale identity

Where Tailscale Serve identity headers are available and trustworthy in the deployment:

- Record identity in audit logs for settings changes.
- Do not trust spoofable headers if the app can be reached directly.
- Keep direct access loopback-only.

### 26.4 Privacy controls

- Exact station location is local configuration.
- Public screenshots/export may optionally blur station location.
- Raw APRS messages can contain personal information; display and retention must be configurable.
- Provide an option to disable message-body persistence while keeping metadata.
- Do not expose the dashboard publicly by default.
- Export requires explicit user action.

### 26.5 Audit log

Record:

- Settings changes.
- Rule changes.
- Alert acknowledgements.
- Integration tests.
- Source enable/disable.
- Retention changes.
- Exports.

Do not record secrets.

---

## 27. Reliability and performance requirements

### 27.1 Reliability

- Local ADS-B and APRS continue during Internet outages.
- External sources become stale/offline independently.
- Backend survives broker restart.
- Adapters survive backend restart.
- Source reconnect uses exponential backoff and jitter.
- Database write congestion does not block live state ingestion.
- Browser reconnect resynchronizes state.
- Malformed records do not crash services.
- Systemd restarts failed processes.
- Shutdown drains important queued events within a bounded interval.

### 27.2 Performance targets

Initial targets on Raspberry Pi 5:

- Local ADS-B display latency: under 2 seconds at p95.
- Local APRS display latency: under 3 seconds at p95.
- Alert creation from an ingested qualifying record: under 2 seconds at p95.
- WebSocket snapshot for ordinary 500 NM state: under 3 seconds.
- API health response: under 500 ms at p95 under normal load.
- Frontend interaction: maintain responsive pan/zoom with 5,000 active point features through clustering and WebGL layers.
- No unbounded memory growth during a 7-day soak test.
- Database size remains within configured budget.
- Sustained CPU should retain headroom for decoders and OS.

These are engineering targets and may be adjusted after measured Pi benchmarks.

### 27.3 Degradation policy

When overloaded:

1. Coalesce track updates.
2. Reduce external-source polling frequency.
3. Reduce broad satellite propagation cadence.
4. Aggregate lightning.
5. Disable nonessential history writes temporarily.
6. Preserve local RF, alerts, source health, and critical events.
7. Expose degraded status.

---

## 28. Observability

### 28.1 Logs

Structured logs should include:

- Component.
- Source.
- Record type.
- Record ID when safe.
- Provider.
- Connection state.
- Retry delay.
- HTTP status.
- Parser error category.
- Count summaries.
- Queue depth.
- Database latency.
- Notification delivery result.

High-rate position updates shall not be logged at info level.

### 28.2 Metrics

Minimum metrics:

- Records received by source/kind.
- Records rejected by source/reason.
- Current live tracks/features.
- Fusion merges.
- Fusion conflicts.
- Adapter reconnects.
- Upstream request latency.
- Source lag.
- MQTT connection state.
- WebSocket clients.
- WebSocket queue drops/coalesces.
- Database queue depth.
- Database write latency.
- Database size/free disk.
- Alerts opened/resolved.
- Notification successes/failures.
- iGate eligible/gated/rejected count where available.

### 28.3 Source health UI

Each source shall show:

- Enabled/disabled.
- Connected/offline.
- Last successful request/message.
- Last record time.
- Lag.
- Current count.
- Error summary.
- Credential state.
- Test button where safe.
- Provider attribution.

---

## 29. Deployment

### 29.1 Recommended deployment model

- Native `systemd` for `readsb` and Dire Wolf.
- Native `systemd` for adapters and backend.
- Mosquitto native or Docker Compose.
- Static frontend built during deployment and served locally.
- Tailscale Serve as the only remote entry point.

Full containerization is optional and must not make USB/audio device access harder.

### 29.2 Service order

```text
network-online
  → mosquitto
  → backend
  → readsb / direwolf
  → local adapters
  → internet adapters
  → tailscale serve
```

Adapters must tolerate dependencies arriving in a different order.

### 29.3 Service account

Use a dedicated `aether` service account where practical.

Permissions:

- Read decoder outputs.
- Read required device/audio interfaces through groups.
- Read secrets file.
- Write application state and database.
- No unnecessary root privileges.

### 29.4 Backup

Back up:

- SQLite database.
- Non-secret configuration.
- Alert rules.
- Geofences.
- Watchlist.
- Saved views.

Do not back up committed repository files as if they were state.

Use SQLite-safe backup procedures.

### 29.5 Upgrades

- Versioned database migrations.
- Schema version compatibility checks.
- Backup before migration.
- Rollback instructions.
- Release notes for provider API changes.
- Health check after upgrade.

---

## 30. Repository structure

```text
aether/
├── PRD.md
├── PROJECT.md
├── README.md
├── AGENTS.md
├── CLAUDE.md
├── pyproject.toml
├── docker-compose.yml
├── .env.example
├── .gitattributes
├── .gitignore
├── frontend/
│   ├── package.json
│   ├── vite.config.ts
│   ├── src/
│   │   ├── api/
│   │   ├── app/
│   │   ├── components/
│   │   ├── features/
│   │   │   ├── alerts/
│   │   │   ├── filters/
│   │   │   ├── map/
│   │   │   ├── replay/
│   │   │   ├── settings/
│   │   │   ├── sources/
│   │   │   └── tracks/
│   │   ├── map/
│   │   │   ├── layers/
│   │   │   ├── presentationRegistry.ts
│   │   │   └── style/
│   │   ├── state/
│   │   └── types/
│   └── public/
├── src/aether/
│   ├── config.py
│   ├── schema/
│   │   ├── records.py
│   │   ├── geometry.py
│   │   ├── provenance.py
│   │   └── validation.py
│   ├── adapters/
│   │   ├── base.py
│   │   ├── registry.py
│   │   ├── local_adsb.py
│   │   ├── network_adsb.py
│   │   ├── local_aprs.py
│   │   ├── aprsis.py
│   │   ├── aisstream.py
│   │   ├── sondehub.py
│   │   ├── lightning_glm.py
│   │   ├── firms.py
│   │   ├── usgs.py
│   │   ├── faa_tfr.py
│   │   ├── faa_notam.py
│   │   └── celestrak.py
│   ├── providers/
│   │   ├── aircraft/
│   │   ├── lightning/
│   │   └── notam/
│   ├── protocols/
│   │   ├── kiss.py
│   │   ├── ax25.py
│   │   ├── aprs.py
│   │   └── ais.py
│   ├── fusion/
│   │   ├── engine.py
│   │   ├── identity.py
│   │   ├── precedence.py
│   │   └── freshness.py
│   ├── state/
│   │   ├── live.py
│   │   ├── expiry.py
│   │   └── sequence.py
│   ├── alerts/
│   │   ├── engine.py
│   │   ├── rules.py
│   │   ├── lifecycle.py
│   │   └── channels/
│   ├── persistence/
│   │   ├── db.py
│   │   ├── migrations.py
│   │   ├── writer.py
│   │   ├── retention.py
│   │   └── queries.py
│   ├── orbit/
│   │   ├── elements.py
│   │   ├── propagation.py
│   │   └── passes.py
│   ├── backend/
│   │   ├── main.py
│   │   ├── api/
│   │   ├── websocket.py
│   │   ├── subscriptions.py
│   │   └── lifespan.py
│   └── observability/
├── migrations/
├── deploy/
│   ├── systemd/
│   ├── mosquitto/
│   └── tailscale/
├── docs/
│   ├── architecture/
│   ├── deployment/
│   ├── integrations/
│   ├── operations/
│   ├── security/
│   └── attribution/
├── samples/
│   ├── adsb/
│   ├── aprs/
│   ├── ais/
│   ├── sondehub/
│   ├── lightning/
│   ├── firms/
│   ├── usgs/
│   ├── faa/
│   └── celestrak/
├── tools/
│   ├── replay.py
│   ├── fake_sources.py
│   ├── benchmark_pi.py
│   └── redact_debug_bundle.py
└── tests/
    ├── unit/
    ├── contract/
    ├── integration/
    ├── replay/
    └── fixtures/
```

---

## 31. Testing strategy

### 31.1 Unit tests

Test:

- Schema validation.
- Geometry validation.
- Unit conversion.
- Timestamp normalization.
- Source parsers.
- Identity keys.
- Fusion precedence.
- Freshness.
- Expiry.
- Alert operators.
- Alert transitions.
- Retention decisions.
- Orbital element parsing.
- Pass calculations against known fixtures.
- TFR geometry parsing.
- APRS loop prevention.
- AIS dynamic/static merge.

### 31.2 Contract tests

Each provider adapter shall have recorded-response contract fixtures.

Contract tests must detect:

- Field rename.
- Missing required field.
- Unexpected null.
- Timestamp format change.
- Error envelope.
- Rate-limit envelope.
- Authentication failure.
- Empty result.

### 31.3 Integration tests

Required flows:

1. Fake `readsb` → adapter → MQTT → fusion → backend → WebSocket.
2. Fake Dire Wolf KISS → APRS adapter → backend.
3. Local APRS packet and APRS-IS duplicate → one fused entity.
4. Local and network aircraft → one fused track with provenance.
5. Alert rule → alert lifecycle → notification-driver stub.
6. Database write → history query → replay.
7. Source disconnect/reconnect.
8. Broker restart.
9. Slow WebSocket client.
10. Disk-pressure retention behavior.

### 31.4 No-hardware demo

The repository shall include a demo mode that produces:

- Several aircraft.
- Emergency squawk transition.
- Military classification examples.
- APRS stations/messages.
- AIS vessel.
- Radiosonde.
- Earthquake.
- FIRMS detections.
- Lightning flashes.
- TFR polygon.
- Satellite pass.
- Source outage.
- Alert notifications.

### 31.5 Hardware acceptance

On the Pi:

- Verify stable USB device assignment.
- Verify `readsb` recovery.
- Verify Dire Wolf decode.
- Verify receive-only iGate configuration.
- Verify no PTT or RF transmit path.
- Verify Tailscale access.
- Verify database write endurance.
- Run a 7-day soak test.
- Measure CPU, RAM, disk, network, and source lag.
- Verify no unbounded queues.

### 31.6 CI rules

- No required live provider credentials.
- No required radio hardware.
- No secrets.
- Recorded fixtures.
- Lint, type-check, unit tests, integration smoke tests, frontend build.
- Optional scheduled live-contract checks may run only with repository secrets and must not expose responses containing sensitive configuration.

---

## 32. Milestones

### Milestone 0 — Revector and preserve

Deliver:

- This PRD in the repository.
- Previous project document retained for history.
- Clear supersession notes.
- Updated `AGENTS.md` and `CLAUDE.md`.
- Schema v2 design.
- Migration plan from existing code.
- Updated roadmap.

Exit criteria:

- No agent can reasonably mistake the old receive-only scope for the current product.

### Milestone 1 — COP core

Deliver:

- Schema v2.
- MQTT v2 topics.
- FastAPI live state.
- Source status.
- Sequence-numbered WebSocket.
- Basic MapLibre shell.
- Layer registry.
- Demo source.

Exit criteria:

- Mixed tracks, features, events, alerts, and status render from simulated data.
- (Persistence is intentionally **not** here — live state is in-memory; SQLite arrives in Milestone 4 with
  history/replay/alerts, its first real consumers.)

### Milestone 2 — Local RF baseline

Deliver:

- Local `readsb` adapter.
- Local APRS adapter.
- Dire Wolf receive-only iGate documentation/config.
- Aircraft/APRS details.
- Local badges.
- Local source health.
- Emergency squawk templates.

Exit criteria:

- Both SDRs operate simultaneously.
- Valid APRS RF packets are gated to APRS-IS.
- There is no RF transmit path.
- Local aircraft and APRS data appear in the browser.

### Milestone 3 — Network fusion

Deliver:

- Network ADS-B provider adapter.
- Aircraft fusion.
- Military classification basis.
- APRS-IS display adapter.
- APRS fusion.
- AISStream adapter.
- 500 NM AOI and provider tiling.
- Filters and TOI watchlist.

Exit criteria:

- Local/network duplicates appear once with correct provenance.
- AIS and APRS-IS operate within configured AOI.

### Milestone 4 — Alerts and history

Deliver:

- SQLite WAL and migrations (introduced here, with its first consumers).
- Alert-rule CRUD UI.
- Rule engine.
- Dashboard/browser notifications.
- SMTP and Discord drivers.
- 30-day retention manager.
- Track history.
- Replay timeline.
- Geofences.

Exit criteria:

- User can edit, test, trigger, acknowledge, and resolve rules.
- Replay cannot trigger live notifications.
- Disk limits are enforced.

### Milestone 5 — Environmental layers

Deliver:

- SondeHub.
- USGS.
- NASA FIRMS.
- NOAA GLM benchmark and adapter if viable.
- Clustering and environmental alerts.

Exit criteria:

- Each source displays correct age, attribution, and semantic caveats.
- Source failures are isolated.

### Milestone 6 — Airspace and orbital layers

Deliver:

- FAA TFR.
- FAA NOTAM capability-gated adapter.
- CelesTrak GP sync.
- SGP4 propagation.
- Pass predictions.
- Satellite watchlist and alerts.

Exit criteria:

- TFR geometry and validity are visible.
- NOTAM behavior is honest when credentials are absent.
- Watched satellite passes are predicted and alerted.
- Element age is visible.

### Milestone 7 — Hardening and release

Deliver:

- Pi benchmark.
- 7-day soak.
- Security review.
- Accessibility pass.
- Backup/restore.
- Upgrade/migration documentation.
- Attribution and terms documentation.
- Debug bundle with redaction.
- Release packaging.

Exit criteria:

- Fresh install works from documented instructions.
- Optional integrations fail gracefully.
- No secrets or personal location are present in repository history.

---

## 33. Acceptance criteria

The project is product-complete for the defined release when all of the following are true.

### 33.1 Core

- One browser map displays all enabled source classes.
- Filters update without page reload.
- Source status is visible.
- Stale data is clearly marked.
- Sequence gaps force resynchronization.
- A slow browser does not block ingestion.

### 33.2 Local radio

- Local ADS-B positions update reliably.
- Local APRS packets decode reliably.
- Dire Wolf gates eligible local RF packets to APRS-IS.
- No configured path can transmit RF.
- Local observations are visually distinguishable.

### 33.3 Fusion

- Same-identity local/network aircraft fuse.
- Same-identity local/APRS-IS entities fuse.
- Per-field provenance is available.
- Ambiguous identities are not falsely merged.

### 33.4 External sources

- Aircraft provider supports the configured AOI through one or more compliant queries.
- AIS uses bounding-box subscription.
- SondeHub tracks and predictions are labeled correctly.
- FIRMS data is labeled as detection/anomaly data.
- USGS event updates do not duplicate.
- Lightning source semantics are accurate.
- TFR geometry and time validity are represented.
- NOTAM source clearly indicates unavailable credentials if not configured.
- Satellite positions are labeled predicted.

### 33.5 Alerts

- User can create, edit, disable, delete, and test a rule.
- Alert transitions do not spam.
- Dashboard and browser channels work.
- Email and Discord work when configured.
- Failed delivery is visible.
- Replay does not deliver live notifications.

### 33.6 History

- Retention targets 30 days.
- Disk limits override time retention safely.
- Selected tracks show history.
- Multi-source replay works.
- Live and replay modes cannot be confused.

### 33.7 Security

- Services bind safely.
- Tailscale Serve is the documented remote path.
- No Funnel.
- No credentials reach the frontend.
- Logs redact secrets.
- Public repository contains no real station identity or coordinates.

---

## 34. Definition of done for any new source

A source is not complete until:

1. It has a documented lawful/permitted data path.
2. It has an adapter.
3. It has schema mapping.
4. It publishes source health.
5. It has parser fixtures.
6. It has unit tests.
7. It has reconnect/backoff tests.
8. It has deduplication behavior.
9. It has TTL/freshness behavior.
10. It has map presentation.
11. It has a detail presentation or generic fallback.
12. It has filters.
13. It has attribution.
14. It has documented limitations.
15. It has no secrets in code or fixtures.
16. It can be disabled without affecting the application.
17. It is included in demo/replay when practical.
18. It does not silently change the meaning of an existing field.

---

## 35. Risks and mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| Volunteer ADS-B provider changes access | Loss of wide-area tracks | Provider interface, fallback adapters, visible degradation |
| 500 NM request exceeds provider radius | Incomplete area | Tiled requests, deduplication, configurable smaller AOI |
| High network-aircraft volume | Pi/browser load | Poll limits, coalescing, AOI filters, WebGL |
| APRS-IS volume too high | Memory/network load | Server-side filters, max AOI, packet dedup |
| Receive-only iGate misconfiguration | Unintended RF path or poor network behavior | Dire Wolf-only gateway, explicit config audit, no PTT hardware/path |
| AISStream outage or API change | AIS unavailable | Isolated adapter, contract fixtures, provider abstraction |
| NOAA GLM processing too heavy | Pi CPU/network load | Benchmark gate, AOI filtering, aggregation, optional alternate provider |
| FIRMS mistaken for confirmed fire | Misleading display | Mandatory thermal-detection wording and source details |
| NOTAM access requires approval | Missing layer | Capability-gated adapter, TFR first, clear status |
| CelesTrak format transition | New objects missing | JSON/CSV support, avoid TLE-only design |
| Full orbital catalog too expensive | CPU overload | Candidate pass computation, category filters, adaptive cadence |
| SQLite growth | Disk exhaustion | Size budget, downsampling, retention alerts |
| Browser push expectations | User expects closed-app alerts | Document active-browser MVP; optional Web Push later |
| Source timestamps differ | Incorrect ordering | Preserve three timestamps, clock-skew warnings |
| False identity fusion | Misleading track | Strict keys, no proximity-only merge |
| API keys leaked | Account/service abuse | Environment secrets, redaction, no frontend exposure |
| Tailnet assumption broken | Unauthorized access | Loopback bind, origin checks, no public docs default |
| Data used operationally | Safety risk | Prominent disclaimers and source links |

---

## 36. Open deployment inputs

These are not unresolved product decisions. They are installation-specific values that the operator must supply:

- Exact station latitude, longitude, and altitude.
- APRS callsign and APRS-IS passcode.
- `readsb` endpoint.
- Dire Wolf endpoint and transport.
- AISStream API key.
- NASA FIRMS map key.
- FAA NOTAM access/key if available.
- SMTP service and recipient.
- Discord webhook.
- Database storage budget.
- Minimum free-disk threshold.
- Preferred basemap provider/style.
- Desired enabled default alert templates.
- Desired default satellite categories.
- Desired display units.
- Tailscale Serve hostname.

---

## 37. Implementation guardrails for coding agents

- This PRD is the product authority.
- Do not silently reintroduce the old receive-only scope.
- Do not add RF transmission.
- Do not bypass provider restrictions.
- Do not invent undocumented API fields.
- Verify current provider documentation before implementation.
- Keep integrations optional.
- Do not expose secrets.
- Do not hard-code maintainer coordinates or callsign.
- Do not implement all milestones in one uncontrolled pass.
- Build one tested vertical slice at a time.
- Preserve source provenance.
- Label derived data.
- Prefer measured Pi performance over assumptions.
- Keep the backend generic.
- Keep source-specific parsing in adapters/providers.
- Keep source-specific UI styling in a centralized presentation registry.
- Update this PRD or an explicit decision record when a settled decision changes.

---

## 38. Reference interfaces and primary documentation

Implementation must re-verify these sources at build time because external interfaces may change.

### Existing project authority

- Previous `aether` project brief retained in the repository for architecture history.

### Aircraft

- `adsb.fi` open-data API:  
  https://github.com/adsbfi/opendata
- `adsb.lol` API documentation:  
  https://api.adsb.lol/
- OpenSky REST API:  
  https://openskynetwork.github.io/opensky-api/rest.html
- `readsb` JSON formats:  
  https://github.com/wiedehopf/readsb/blob/dev/README-json.md

### APRS

- APRS-IS server-side filters:  
  https://www.aprs-is.net/javAPRSFilter.aspx
- APRS-IS iGate design:  
  https://www.aprs-is.net/igating.aspx
- Dire Wolf documentation repository:  
  https://github.com/wb2osz/direwolf-doc

### AIS

- AISStream API reference:  
  https://aisstream.io/documentation

### SondeHub

- SondeHub infrastructure/OpenAPI definition:  
  https://github.com/projecthorus/sondehub-infra/blob/main/swagger.yaml

### Fire and earthquakes

- NASA FIRMS API:  
  https://firms.modaps.eosdis.nasa.gov/api/
- FIRMS API tutorial/map-key details:  
  https://firms.modaps.eosdis.nasa.gov/content/academy/data_api/firms_api_use.html
- USGS earthquake GeoJSON:  
  https://earthquake.usgs.gov/earthquakes/feed/v1.0/geojson.php

### Lightning

- NOAA GLM Level 2 dataset:  
  https://www.ncei.noaa.gov/access/metadata/landing-page/bin/iso?id=gov.noaa.ncdc:C01527
- NOAA GOES Open Data on AWS:  
  https://registry.opendata.aws/noaa-goes/
- NOAA GOES terrestrial weather access:  
  https://www.ncei.noaa.gov/products/goes-terrestrial-weather-abi-glm

### FAA

- FAA TFR site and XML list:  
  https://tfr.faa.gov/
- FAA Data Portal:  
  https://www.faa.gov/data
- FAA SWIM:  
  https://www.faa.gov/air_traffic/technology/swim
- FAA FNS reference implementation:  
  https://github.com/faa-swim/fns-client

### Orbital data

- CelesTrak GP formats:  
  https://www.celestrak.org/NORAD/documentation/gp-data-formats.php
- CelesTrak current GP element sets:  
  https://www.celestrak.org/NORAD/elements/

---

## 39. Final product statement

`aether` shall be a private, open-source, local-first common operating picture that combines the operator’s own RF observations with permitted public data sources, fuses duplicate identities, preserves provenance and uncertainty, records useful history, and produces configurable alerts through a modern browser interface.

Its defining characteristics are:

- **One map.**
- **Many independent sources.**
- **Local RF clearly distinguished from network data.**
- **No RF transmission.**
- **Editable alerts and geofences.**
- **Thirty-day rolling history within a hard storage budget.**
- **Predicted data labeled as predicted.**
- **Stale data labeled as stale.**
- **Safe private access through Tailscale.**
- **A repository others can deploy without inheriting the maintainer’s secrets or identity.**

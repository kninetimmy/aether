"""Process configuration from the environment (PRD §22 env table).

Only the keys M1 actually needs live here — the MQTT broker endpoint and the
no-hardware demo toggle. Later milestones extend this with home location, DB
paths, and per-source credentials; each is read at the edge that uses it, never
sprinkled through the backend. Everything has a safe loopback default so the
no-hardware demo runs with no environment set.
"""

import os
from dataclasses import dataclass, field

#: Default broker endpoint — loopback only (PRD §23: "MQTT is loopback-bound").
DEFAULT_MQTT_HOST = "127.0.0.1"
DEFAULT_MQTT_PORT = 1883

#: Local ADS-B (`readsb`) snapshot location — a file path or http(s) URL to
#: ``aircraft.json``. The common readsb/tar1090 layout serves it over HTTP; a
#: bare path reads the on-disk snapshot directly. Only used when enabled.
DEFAULT_LOCAL_ADSB_SOURCE = "http://127.0.0.1:8080/data/aircraft.json"
#: How often the poller reads a fresh snapshot (PRD §17.4).
DEFAULT_LOCAL_ADSB_POLL_S = 1.0
#: At most one ordinary update per aircraft per this window (PRD §18.1); an
#: emergency-squawk transition bypasses it.
DEFAULT_LOCAL_ADSB_THROTTLE_S = 1.0
#: Per-request timeout for URL snapshots (PRD §17.4 "use timeouts").
DEFAULT_LOCAL_ADSB_TIMEOUT_S = 5.0

#: Local APRS (Dire Wolf KISS) endpoint — loopback only, the KISSPORT Dire Wolf
#: serves (default 8001). aether only *reads* this socket (receive-only, PRD §18.3).
DEFAULT_LOCAL_APRS_HOST = "127.0.0.1"
DEFAULT_LOCAL_APRS_PORT = 8001
#: At most one ordinary update per station per this window (PRD §18.1).
DEFAULT_LOCAL_APRS_THROTTLE_S = 1.0
#: Connect timeout for the KISS socket (PRD §17.4 "use timeouts").
DEFAULT_LOCAL_APRS_TIMEOUT_S = 5.0

#: Network ADS-B provider for Internet fusion (PRD §18.2). ``adsb.fi`` is the
#: default open provider; ``fake`` selects the in-process no-hardware feeder.
DEFAULT_NETWORK_ADSB_PROVIDER = "adsb.fi"
#: AOI center. **Deliberately the null-island placeholder** — the repo carries no
#: station coordinates (PRD §2/§37); the operator supplies their home position via
#: ``AETHER_NETWORK_ADSB_LAT``/``_LON``. Left at the default the AOI simply covers
#: open ocean and finds nothing, which fails *visibly* rather than leaking a location.
DEFAULT_NETWORK_ADSB_LAT = 0.0
DEFAULT_NETWORK_ADSB_LON = 0.0
#: Default AOI radius (NM) — the PRD §16.2 home-station default, tiled below the
#: provider's per-query cap (PRD §16.4).
DEFAULT_NETWORK_ADSB_RADIUS_NM = 500.0
#: How often a full AOI sweep runs (PRD §17.4). Slower than the 1 s local poll: a
#: network feed lags more, and a tiled sweep is several polite requests.
DEFAULT_NETWORK_ADSB_POLL_S = 5.0
#: Minimum spacing between per-tile requests within one sweep — the provider
#: politeness limit (PRD §17.4, §38 "respect rate limits"). adsb.fi asks ~1 req/s.
DEFAULT_NETWORK_ADSB_RATE_LIMIT_S = 1.0
#: Per-request timeout for a provider query (PRD §17.4 "use timeouts").
DEFAULT_NETWORK_ADSB_TIMEOUT_S = 10.0

#: Military ICAO 24-bit address blocks for the address-block classification basis
#: (PRD §11.5 MIL-FR-002). **Deliberately empty** — the repo ships the *mechanism*,
#: not a baked-in allocation table that could silently mislabel civil airframes if
#: stale/wrong (honest-labeling decision). The operator supplies verified ranges via
#: ``AETHER_MIL_ICAO_BLOCKS`` as comma-separated ``start-end`` hex pairs, e.g.
#: ``"adf7c8-afffff, 43c000-43cfff"``; parsed at the adapter edge by
#: :func:`aether.adapters.mil_classify.parse_ranges`. Empty → that basis stays inert
#: and only the provider ``dbFlags`` bit classifies (MIL-FR-001).
DEFAULT_MIL_ICAO_BLOCKS = ""

#: APRS-IS display feed for Internet APRS fusion (PRD §18.4). RECEIVE-ONLY: aether
#: only reads the feed (passcode ``-1`` cannot transmit) — there is no RF path here.
#: ``rotate.aprs2.net`` is the Tier-2 rotate address (public infrastructure, not a
#: secret); port 14580 is the user-defined-filter feed port.
DEFAULT_APRS_IS_HOST = "rotate.aprs2.net"
DEFAULT_APRS_IS_PORT = 14580
#: Operator-supplied APRS-IS login. **Callsign is deliberately empty** — the repo
#: carries no callsign (PRD §2/§37); enable the adapter and set
#: ``AETHER_APRS_IS_CALLSIGN`` to your own. Enabled + empty fails *visibly* as an
#: ``offline`` source status, never as the maintainer's identity.
DEFAULT_APRS_IS_CALLSIGN = ""
#: ``-1`` = receive-only login (cannot inject packets to APRS-IS). The operator may
#: set a real passcode, but aether never transmits regardless (PRD §2, §18.4).
DEFAULT_APRS_IS_PASSCODE = "-1"
#: AOI center for the server-side range filter. **Null-island placeholder** — the
#: repo carries no station coordinates (PRD §2/§37); left at the default the filter
#: covers open ocean and finds nothing, failing visibly rather than leaking a
#: location (same stance as network ADS-B). Operator supplies via
#: ``AETHER_APRS_IS_LAT``/``_LON``.
DEFAULT_APRS_IS_CENTER_LAT = 0.0
DEFAULT_APRS_IS_CENTER_LON = 0.0
#: Default AOI radius (NM) — the PRD §16.2 home-station default; converted to km for
#: the APRS-IS ``r/lat/lon/dist`` range filter at the adapter edge.
DEFAULT_APRS_IS_RADIUS_NM = 500.0
#: At most one ordinary update per station per this window (PRD §18.1).
DEFAULT_APRS_IS_THROTTLE_S = 1.0
#: Connect timeout for the APRS-IS socket (PRD §17.4 "use timeouts").
DEFAULT_APRS_IS_TIMEOUT_S = 10.0
#: Reconnect if no line — not even a ``#`` keepalive (~20 s apart) — arrives within
#: this window: the stalled-connection guard (PRD §17.3).
DEFAULT_APRS_IS_STALL_S = 60.0

#: AIS vessel feed via AISStream.io secure WebSocket (PRD §18.5). RECEIVE-ONLY: the
#: subscription is the only thing aether sends; there is no RF path. The host/path
#: are AISStream's public stream endpoint (not secrets); ``wss`` (TLS) is the real
#: transport — the no-hardware fake feeder flips ``AETHER_AIS_TLS=0`` for plain ws.
DEFAULT_AIS_HOST = "stream.aisstream.io"
DEFAULT_AIS_PORT = 443
DEFAULT_AIS_PATH = "/v0/stream"
DEFAULT_AIS_TLS = True
#: Operator-supplied AISStream API key. **Deliberately empty** — the repo carries no
#: credentials (PRD §2/§37); enable the adapter and set ``AETHER_AIS_API_KEY`` to
#: your own. Enabled + empty fails *visibly* as an ``offline`` source status (the key
#: travels only in the subscription body and is never logged), never anonymously.
DEFAULT_AIS_API_KEY = ""
#: AOI center for the AISStream bounding-box subscription. **Null-island placeholder**
#: — the repo carries no station coordinates (PRD §2/§37); left at the default the box
#: covers open ocean and finds nothing, failing visibly rather than leaking a location
#: (same stance as network ADS-B / APRS-IS). Operator supplies via
#: ``AETHER_AIS_LAT``/``_LON``.
DEFAULT_AIS_CENTER_LAT = 0.0
DEFAULT_AIS_CENTER_LON = 0.0
#: Default AOI radius (NM) — the PRD §16.2 home-station default; converted to a
#: lat/lon bounding box for the AISStream subscription at the adapter edge.
DEFAULT_AIS_RADIUS_NM = 500.0
#: At most one ordinary update per vessel per this window (PRD §18.1).
DEFAULT_AIS_THROTTLE_S = 1.0
#: Connect/handshake timeout for the AISStream WebSocket (PRD §17.4 "use timeouts").
#: Liveness after connect is the WebSocket ping/pong the client maintains, so there
#: is no data-silence stall: a quiet AOI legitimately sends nothing.
DEFAULT_AIS_TIMEOUT_S = 10.0

#: USGS earthquake feed (M5.1, PRD §11.12). Public-domain GeoJSON — no key, no terms
#: gate; aether only *fetches* within the feed cadence. The default is the ``all_hour``
#: summary (small, regenerates ~every minute); operators may point at ``all_day`` /
#: ``2.5_day`` / ``significant_month`` etc. ``fake``/``demo`` selects the no-hardware
#: feeder. The AOI center defaults to the station; quakes outside the radius or below
#: ``min_magnitude`` are dropped at the adapter edge.
DEFAULT_USGS_FEED_URL = "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/all_hour.geojson"
#: Poll no faster than the feed regeneration cadence (USGS-FR-002); summary feeds
#: refresh about once a minute.
DEFAULT_USGS_POLL_S = 60.0
DEFAULT_USGS_TIMEOUT_S = 10.0
DEFAULT_USGS_RADIUS_NM = 500.0
#: 0.0 ⇒ no magnitude floor (show everything in the AOI).
DEFAULT_USGS_MIN_MAGNITUDE = 0.0

#: SondeHub radiosonde feed (M5.2, PRD §11.9). Read-only crowd-sourced telemetry — no
#: key, no terms gate; aether only *fetches* within the source cadence. The default is
#: the public v2 REST base (the documented fallback to the preferred MQTT-WebSocket
#: stream, SONDE-FR-002/003); the adapter appends ``/sondes?lat&lon&distance&last`` for
#: the AOI. ``fake``/``demo`` selects the no-hardware feeder. The AOI center defaults to
#: the station; sondes outside the radius are dropped at the adapter edge.
DEFAULT_SONDEHUB_API_BASE = "https://api.v2.sondehub.org"
#: Poll cadence; sonde frames arrive every few seconds upstream, but a ~30 s poll of the
#: AOI map is ample for the COP and gentle on the public API (SONDE-FR-004 recency is
#: bounded server-side by ``last``).
DEFAULT_SONDEHUB_POLL_S = 30.0
DEFAULT_SONDEHUB_TIMEOUT_S = 10.0
DEFAULT_SONDEHUB_RADIUS_NM = 500.0
#: ``last`` window (seconds): only sondes heard within this span are returned — drops
#: landed/expired flights (SONDE-FR-004). One hour covers an active ascent+descent.
DEFAULT_SONDEHUB_RECENCY_S = 3600.0

#: NASA FIRMS active-fire feed (M5.3, PRD §11.11). Capability-gated: the Area API
#: needs a user-supplied **map key** (``AETHER_FIRMS_MAP_KEY``) — without it the adapter
#: degrades *visibly* (an ``offline`` source status), never crashes (FIRMS-FR-001). The
#: default base is the public FIRMS host; the adapter appends
#: ``/api/area/csv/<key>/<source>/<w,s,e,n>/<day_range>`` for the AOI bounding box
#: (FIRMS-FR-002). Near-real-time VIIRS (Suomi-NPP) is the default source, configurable
#: to any FIRMS source (FIRMS-FR-003). ``fake``/``demo`` (as base *or* key) selects the
#: no-hardware feeder. Detections outside the AOI radius or below ``min_confidence`` are
#: dropped at the adapter edge.
DEFAULT_FIRMS_API_BASE = "https://firms.modaps.eosdis.nasa.gov"
#: Near-real-time VIIRS by default (FIRMS-FR-003); operators may select
#: ``VIIRS_NOAA20_NRT`` / ``VIIRS_NOAA21_NRT`` / ``MODIS_NRT`` / ``LANDSAT_NRT`` etc.
DEFAULT_FIRMS_SOURCE = "VIIRS_SNPP_NRT"
#: Area-API look-back window in days (the endpoint accepts 1..5). One day of detections
#: is ample for a live COP and keeps each query small (FIRMS-FR-007 transaction limits).
DEFAULT_FIRMS_DAY_RANGE = 1
#: FIRMS NRT latency is hours and the feed updates only a few times a day; a 15-minute
#: poll keeps the map fresh while staying far under the 5000-tx/10-min limit (FIRMS-FR-007).
DEFAULT_FIRMS_POLL_S = 900.0
DEFAULT_FIRMS_TIMEOUT_S = 15.0
DEFAULT_FIRMS_RADIUS_NM = 500.0
#: Minimum detection confidence class to keep: "" (none) / "low" / "nominal" / "high".
#: Empty ⇒ show every detection in the AOI; honest labeling carries the class regardless.
DEFAULT_FIRMS_MIN_CONFIDENCE = ""

#: NOAA GOES GLM lightning feed (M5.6, PRD §11.10, benchmark-gated — see
#: ``docs/glm-benchmark.md``, verdict *acceptable* on the Pi 5). Off by default; opt in with
#: ``AETHER_GLM=1``. The live provider reads GLM L2 (LCFA) NetCDF from NOAA's public GOES
#: Open Data on AWS (no key), so the only gate is the optional ``netCDF4`` parser
#: (``pip install "aether[lightning]"``) — missing ⇒ one ``offline`` status, never a crash
#: (LIGHTNING-FR-002). ``fake``/``demo`` (as satellite *or* S3 base) selects the no-hardware
#: feeder, which needs no parser. ``G19`` is the current GOES-East (use ``G18`` for West).
DEFAULT_GLM_SATELLITE = "G19"
#: Empty ⇒ the bucket host is derived from the satellite; set to ``fake`` for the feeder.
DEFAULT_GLM_S3_BASE = ""
DEFAULT_GLM_RADIUS_NM = 500.0
#: Poll cadence. GLM files publish every 20 s; a 60 s poll fetches the ~3 new files each
#: pass (complete coverage at ~1.6 GB/day per satellite — the benchmark's bandwidth note).
DEFAULT_GLM_POLL_S = 60.0
#: Per-poll file cap so a reconnect after an outage catches up to *live* rather than
#: replaying hours of backlog (LIGHTNING-FR-005 "only the newest required").
DEFAULT_GLM_MAX_FILES_PER_POLL = 12
#: On-map lifetime of a transient flash; it ages off via the live-state expiry sweep so an
#: active storm does not accumulate flashes without bound (10 min by default).
DEFAULT_GLM_FLASH_TTL_S = 600.0
DEFAULT_GLM_TIMEOUT_S = 30.0
#: Keep only ``flash_quality_flag == good_quality`` flashes when set; default emits all and
#: carries the quality flag as an attribute (honest labeling over silent filtering).
DEFAULT_GLM_GOOD_QUALITY_ONLY = False

#: FAA Temporary Flight Restrictions (M6.1, PRD §11.13/§18.10). Off by default; opt in
#: with ``AETHER_FAA_TFR=1``. Read-only public data — no key — so the only "gate" is
#: politeness: the adapter polls the light list, then fetches at most
#: ``max_details_per_poll`` detail XMLs for *new/changed* TFRs, draining a nationwide
#: list over a few polls instead of one burst. ``AETHER_FAA_TFR_BASE_URL=fake`` selects
#: the no-hardware feeder. ``states`` (CSV, e.g. ``FL,GA``) pre-filters the list to the
#: operator's region to cut detail fetches; empty ⇒ all states (the AOI radius still
#: does the real filtering after each detail is parsed).
DEFAULT_FAA_TFR_BASE_URL = "https://tfr.faa.gov"
#: TFRs change on the order of minutes-to-hours; a 5-minute poll keeps the map fresh
#: while staying gentle on the FAA service.
DEFAULT_FAA_TFR_POLL_S = 300.0
DEFAULT_FAA_TFR_TIMEOUT_S = 15.0
DEFAULT_FAA_TFR_RADIUS_NM = 500.0
#: Per-poll detail-fetch budget (politeness + catch-up bound).
DEFAULT_FAA_TFR_MAX_DETAILS_PER_POLL = 60

#: FAA NOTAM API (M6.4, PRD §11.13/§18.11). Capability-gated (AIRSPACE-FR-008): the API
#: needs operator-supplied ``client_id``/``client_secret``. ``fake`` (base or either
#: credential) selects the no-hardware feeder. The ``locationRadius`` query is capped at
#: the FAA-documented 100 NM maximum, so the default query radius is that cap (the wider
#: AOI is honored by the geometry, not the point query).
DEFAULT_FAA_NOTAM_BASE_URL = "https://external-api.faa.gov"
DEFAULT_FAA_NOTAM_RADIUS_NM = 100.0
#: NOTAMs change on the order of minutes-to-hours; a 5-minute poll keeps the map fresh.
DEFAULT_FAA_NOTAM_POLL_S = 300.0
DEFAULT_FAA_NOTAM_TIMEOUT_S = 15.0
#: Page size requested (FAA max is 1000); paginated up to the per-poll page budget.
DEFAULT_FAA_NOTAM_PAGE_SIZE = 50
DEFAULT_FAA_NOTAM_MAX_PAGES_PER_POLL = 5

#: CelesTrak orbital tracking (M6.5, PRD §11.14/§18.12). Off by default; opt in with
#: ``AETHER_CELESTRAK=1``. Read-only public GP data (no key) — the gate is the optional
#: ``sgp4`` propagator (``pip install "aether[orbital]"``): missing ⇒ one ``offline`` status,
#: never a crash (the GLM/FIRMS stance). ``AETHER_CELESTRAK_BASE_URL=fake`` selects the
#: no-hardware feeder. The GP service refreshes only ~every 2 h, so the sync cadence default
#: is a conservative 6 h (§38 rate limit — a tight loop firewalls the IP). Objects are
#: propagated on a fast cadence and filtered to those above ``min_elevation_deg`` (ORBIT-FR-007).
DEFAULT_CELESTRAK_BASE_URL = "https://celestrak.org"
#: Default GP groups: crewed/uncrewed stations, the full active catalog, and amateur-radio
#: birds. The ``active`` group is large — Pi-heavy at a fast propagate cadence; full
#: multi-tier cadence (ORBIT-FR-011) is deferred to M6.6 (see docs/orbital-celestrak.md).
DEFAULT_CELESTRAK_GROUPS = ("stations", "active", "amateur")
#: Sync no faster than CelesTrak's 2 h refresh; 6 h is safe and gentle (§38).
DEFAULT_CELESTRAK_SYNC_S = 21600.0
#: Propagate the synced set on this cadence; 15 s is a smooth-enough track without churn.
DEFAULT_CELESTRAK_PROPAGATE_S = 15.0
#: Emit only objects currently above this elevation (deg) over the observer (ORBIT-FR-007).
DEFAULT_CELESTRAK_MIN_ELEVATION_DEG = 10.0
#: On-map freshness of a propagated position; it ages off via the live-state expiry sweep so a
#: stalled adapter does not leave a frozen object. Short — positions are re-emitted each tick.
DEFAULT_CELESTRAK_VALID_S = 30.0
DEFAULT_CELESTRAK_TIMEOUT_S = 15.0
#: Observer altitude above the WGS-84 ellipsoid (metres) for the look-angle origin.
DEFAULT_CELESTRAK_OBSERVER_ALT_M = 0.0

#: Persistence store (M4, PRD §19). SQLite path; the WAL sidecars (`-wal`/`-shm`)
#: sit alongside it. Live state never depends on this store (PRD §5), so it is off
#: by default and a slow/failed disk only backs up the writer's own queue.
DEFAULT_DB_PATH = "aether.db"
#: Bounded persistence write queue (PRD §19.2 "single bounded async write queue");
#: a full queue drops records rather than back-pressuring the bus.
DEFAULT_PERSIST_QUEUE_MAX = 10000

#: Per-source persist-cadence sampling (PRD §19.5). On by default: a coarse
#: persist-time gate admitting at most one observation per ``(source, identity)``
#: per the cadence below, bounding DB growth *proactively* (the edge ThrottleGate
#: only caps the bus at ~1/identity/s; the retention manager only reclaims space
#: *reactively*). Off ⇒ full-fidelity capture (the M4.1 behavior). Emergency-tagged
#: tracks always persist regardless of cadence.
DEFAULT_PERSIST_SAMPLE = True
#: Per-source minimum seconds between persisted observations of one identity
#: (PRD §19.5). ``0`` disables the time gate for that source — "persist every
#: unique packet" for APRS (already edge-throttled + de-duplicated), and the safe
#: default for any untuned source (incl. the in-process demo), so aether never
#: silently thins a feed it wasn't told how to sample.
DEFAULT_PERSIST_SAMPLE_LOCAL_ADSB_S = 5.0
DEFAULT_PERSIST_SAMPLE_NETWORK_ADSB_S = 15.0
DEFAULT_PERSIST_SAMPLE_AIS_S = 30.0
#: Covers both APRS sources (``local_aprs`` + ``aprs_is``): every unique packet.
DEFAULT_PERSIST_SAMPLE_APRS_S = 0.0
#: Fallback cadence for sources without a tuned knob above (demo, sonde, future
#: feeds). ``0`` ⇒ persist all, keeping the no-hardware demo path full-fidelity.
DEFAULT_PERSIST_SAMPLE_DEFAULT_S = 0.0

#: Retention window (PRD §19.4 target = 30 days). Observations older than this are
#: deleted on each retention sweep; storage pressure may shorten the *effective*
#: window below this (ladder step 5). Set very high to effectively disable time
#: retention — bounded retention is still the PRD intent, so it never goes off.
DEFAULT_RETENTION_DAYS = 30
#: Hard size budget for the SQLite store (main file + ``-wal``/``-shm`` sidecars),
#: in GiB (PRD §19.4 ``AETHER_DB_MAX_GB``). ``0`` disables the size cap, leaving
#: only time retention. When exceeded, the storage-pressure ladder reclaims space.
DEFAULT_DB_MAX_GB = 0.0
#: Minimum free space to keep on the store's filesystem, in GiB (PRD §19.4
#: ``AETHER_MIN_FREE_DISK_GB``). ``0`` disables the free-disk floor. Crossing it is
#: treated as critical pressure — aether sheds its own oldest data and warns, but
#: cannot reclaim space held by other files (honest-labeling).
DEFAULT_MIN_FREE_DISK_GB = 0.0
#: How often the retention sweep runs (PRD §19.4). One hour balances responsiveness
#: against sweep cost; each sweep is exception-isolated so a bad pass never wedges
#: the loop, and it never gates serving live state (PRD §5).
DEFAULT_RETENTION_INTERVAL_S = 3600.0
#: Fractions of ``AETHER_DB_MAX_GB`` at which the storage-pressure ladder engages
#: (high-water) and escalates to deleting non-expired data + shortening retention
#: (critical-water) — PRD §19.4 "High-water and critical-water marks".
DEFAULT_DB_HIGH_WATER = 0.85
DEFAULT_DB_CRITICAL_WATER = 0.95

#: Per-channel minimum severity to deliver a notification (M4.7, PRD §20.5). An
#: alert is delivered on a channel only when its severity meets the channel's
#: threshold; below it the channel resolves to ``suppressed``. ``info`` (the default)
#: delivers everything. ``dashboard`` has no threshold — the alert centre records
#: every alert. Honored for browser/email/discord by the dispatcher.
DEFAULT_NOTIFY_BROWSER_MIN_SEVERITY = "info"
DEFAULT_NOTIFY_EMAIL_MIN_SEVERITY = "info"
DEFAULT_NOTIFY_DISCORD_MIN_SEVERITY = "info"

#: Server-side notification-driver defaults (M4.7b, PRD §20.4). The email driver is
#: wired only when host + both addresses are set; discord only when its webhook URL
#: is set — an unwired channel resolves to ``unconfigured`` (visible, never a crash).
#: 587 + STARTTLS is the de-facto SMTP submission default; ``ssl`` (465) and ``none``
#: are the other supported TLS modes. The SMTP password and webhook URL are secrets:
#: env/secrets only, kept out of ``repr``, and never logged or echoed by an API.
DEFAULT_SMTP_PORT = 587
DEFAULT_SMTP_TLS = "starttls"

#: Hard cap on observations returned by one ``/api/v2/tracks/{id}/history`` request
#: (M4.3, PRD §21.3/§11.15). The request's ``limit`` query param defaults to and is
#: clamped to this, bounding response size + read cost on the Pi (PRD §37); the
#: response flags ``truncated`` when the cap is hit so a capped trail is never
#: mistaken for a complete one (no silent caps). The read is served on a fresh
#: read-only connection per request and never gates serving live state (PRD §5).
DEFAULT_HISTORY_MAX_POINTS = 10000

#: Bounds on one replay window (M4.8, PRD §19.6/§21.6). ``replay_max_records`` caps the
#: reconstructed buffer ``POST /api/v2/replay/sessions`` returns (the request's
#: ``max_records`` defaults to and is clamped to this; ``truncated`` flags a hit so a
#: capped buffer is never mistaken for the whole window). ``replay_max_window_h`` caps
#: the ``[start, end)`` span a single request may ask for. Both bound response size +
#: read/reconstruct cost on the Pi (PRD §37); the read AND the record reconstruction
#: are served in a worker thread on a fresh read-only connection per request, so a
#: large window never gates serving live state (PRD §5). The record cap is set for the
#: Pi profile: a full buffer reconstructs + JSON-encodes to a payload the browser holds
#: in memory, so 5000 (~5–6 MiB) keeps both the loop hand-off and the browser bounded;
#: raise ``AETHER_REPLAY_MAX_RECORDS`` on a roomier host. Active only when persist is on
#: (replay is unavailable — 503 — otherwise).
DEFAULT_REPLAY_MAX_RECORDS = 5000
DEFAULT_REPLAY_MAX_WINDOW_H = 168


#: Canonical station location (PRD §5/§16.2). The ONE home position the whole app
#: shares: the default per-connection websocket bbox (PRD §16.3a), the frontend
#: range-from-station filter origin (served via ``/api/config``), and the per-adapter
#: AOI centers below, which now all derive from this instead of duplicating their own
#: ``*_LAT``/``_LON`` keys (resolves the duplication noted in §M3.6 / former
#: config.py:185-186). **Deliberately the null-island placeholder** — the repo carries
#: NO station coordinates (PRD §2/§5/§37); the operator supplies their home position
#: via ``AETHER_STATION_LAT``/``_LON``. Left at 0,0 every consumer degrades VISIBLY:
#: the ws default bbox becomes UNBOUNDED (never a degenerate zero-area null-island
#: box), the range filter disables, and the AOI sweeps cover open ocean and find
#: nothing — failing loudly rather than leaking a location.
DEFAULT_STATION_LAT = 0.0
DEFAULT_STATION_LON = 0.0
#: Default station AOI radius (NM) — the PRD §16.2 home-station default.
DEFAULT_STATION_RADIUS_NM = 500.0


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    """Resolved runtime settings; build with :meth:`from_env`."""

    mqtt_host: str = DEFAULT_MQTT_HOST
    mqtt_port: int = DEFAULT_MQTT_PORT
    #: Run the in-process demo publisher alongside the backend (M1 no-hardware
    #: gate). A real deployment leaves this off and runs source adapters instead.
    demo_source: bool = True

    #: Canonical station location (PRD §5/§16.2). The single home position shared by
    #: the websocket default bbox, the frontend range filter (via ``/api/config``),
    #: and every per-adapter AOI center below (which default to it). 0,0 ⇒ unbounded
    #: ws default + disabled range filter (no committed coordinates, PRD §5).
    station_lat: float = DEFAULT_STATION_LAT
    station_lon: float = DEFAULT_STATION_LON
    station_radius_nm: float = DEFAULT_STATION_RADIUS_NM

    #: Run the local ADS-B (`readsb`) adapter alongside the backend. Off by
    #: default — opt in once an `aircraft.json` source is reachable (M2.1).
    local_adsb: bool = False
    local_adsb_source: str = DEFAULT_LOCAL_ADSB_SOURCE
    local_adsb_poll_s: float = DEFAULT_LOCAL_ADSB_POLL_S
    local_adsb_throttle_s: float = DEFAULT_LOCAL_ADSB_THROTTLE_S
    local_adsb_timeout_s: float = DEFAULT_LOCAL_ADSB_TIMEOUT_S

    #: Run the local APRS (Dire Wolf KISS) adapter alongside the backend. Off by
    #: default — opt in once Dire Wolf's KISS port is reachable (M2.2b). aether
    #: only reads this socket; Dire Wolf does the RX->APRS-IS gating (PRD §18.3).
    local_aprs: bool = False
    local_aprs_host: str = DEFAULT_LOCAL_APRS_HOST
    local_aprs_port: int = DEFAULT_LOCAL_APRS_PORT
    local_aprs_throttle_s: float = DEFAULT_LOCAL_APRS_THROTTLE_S
    local_aprs_timeout_s: float = DEFAULT_LOCAL_APRS_TIMEOUT_S

    #: Run the network ADS-B adapter alongside the backend. Off by default — opt in
    #: once an AOI center is set; its records fuse with local ADS-B by ICAO (M3.2).
    network_adsb: bool = False
    network_adsb_provider: str = DEFAULT_NETWORK_ADSB_PROVIDER
    network_adsb_center_lat: float = DEFAULT_NETWORK_ADSB_LAT
    network_adsb_center_lon: float = DEFAULT_NETWORK_ADSB_LON
    network_adsb_radius_nm: float = DEFAULT_NETWORK_ADSB_RADIUS_NM
    network_adsb_poll_s: float = DEFAULT_NETWORK_ADSB_POLL_S
    network_adsb_rate_limit_s: float = DEFAULT_NETWORK_ADSB_RATE_LIMIT_S
    network_adsb_timeout_s: float = DEFAULT_NETWORK_ADSB_TIMEOUT_S

    #: Operator-supplied military ICAO address blocks (raw config string; parsed at
    #: the ADS-B edge). Shared by both the local and network ADS-B adapters so they
    #: classify identically (PRD §11.5 MIL-FR-002).
    mil_icao_blocks: str = DEFAULT_MIL_ICAO_BLOCKS

    #: Run the APRS-IS display adapter alongside the backend. Off by default — opt in
    #: once a callsign is set; its records fuse with local APRS by callsign/object
    #: identity (M3.4, PRD §18.4). RECEIVE-ONLY: passcode -1 cannot transmit. The
    #: per-adapter center now defaults to the canonical ``station_lat``/``_lon``
    #: (unified in M3.6b); an explicit ``AETHER_APRS_IS_LAT``/``_LON`` still overrides.
    aprs_is: bool = False
    aprs_is_host: str = DEFAULT_APRS_IS_HOST
    aprs_is_port: int = DEFAULT_APRS_IS_PORT
    aprs_is_callsign: str = DEFAULT_APRS_IS_CALLSIGN
    aprs_is_passcode: str = DEFAULT_APRS_IS_PASSCODE
    aprs_is_center_lat: float = DEFAULT_APRS_IS_CENTER_LAT
    aprs_is_center_lon: float = DEFAULT_APRS_IS_CENTER_LON
    aprs_is_radius_nm: float = DEFAULT_APRS_IS_RADIUS_NM
    aprs_is_throttle_s: float = DEFAULT_APRS_IS_THROTTLE_S
    aprs_is_timeout_s: float = DEFAULT_APRS_IS_TIMEOUT_S
    aprs_is_stall_s: float = DEFAULT_APRS_IS_STALL_S

    #: Run the AIS (AISStream.io) vessel adapter alongside the backend. Off by
    #: default — opt in once an API key + AOI are set (M3.5, PRD §18.5). RECEIVE-ONLY:
    #: a network-only Internet feed, no RF path. ``ais_tls`` is True for the real
    #: ``wss`` endpoint; the no-hardware fake feeder runs plain ``ws``.
    ais: bool = False
    ais_host: str = DEFAULT_AIS_HOST
    ais_port: int = DEFAULT_AIS_PORT
    ais_path: str = DEFAULT_AIS_PATH
    ais_tls: bool = DEFAULT_AIS_TLS
    ais_api_key: str = DEFAULT_AIS_API_KEY
    ais_center_lat: float = DEFAULT_AIS_CENTER_LAT
    ais_center_lon: float = DEFAULT_AIS_CENTER_LON
    ais_radius_nm: float = DEFAULT_AIS_RADIUS_NM
    ais_throttle_s: float = DEFAULT_AIS_THROTTLE_S
    ais_timeout_s: float = DEFAULT_AIS_TIMEOUT_S

    #: Run the USGS earthquake adapter alongside the backend (M5.1, PRD §11.12). Off by
    #: default — opt in with ``AETHER_USGS=1``. Read-only public GeoJSON, no key. The AOI
    #: center defaults to the station; ``usgs_feed_url=fake`` selects the no-hardware feeder.
    usgs: bool = False
    usgs_feed_url: str = DEFAULT_USGS_FEED_URL
    usgs_center_lat: float = DEFAULT_STATION_LAT
    usgs_center_lon: float = DEFAULT_STATION_LON
    usgs_radius_nm: float = DEFAULT_USGS_RADIUS_NM
    usgs_min_magnitude: float = DEFAULT_USGS_MIN_MAGNITUDE
    usgs_poll_s: float = DEFAULT_USGS_POLL_S
    usgs_timeout_s: float = DEFAULT_USGS_TIMEOUT_S

    #: Run the SondeHub radiosonde adapter alongside the backend (M5.2, PRD §11.9). Off
    #: by default — opt in with ``AETHER_SONDEHUB=1``. Read-only public REST, no key. The
    #: AOI center defaults to the station; ``sondehub_api_base=fake`` selects the
    #: no-hardware feeder.
    sondehub: bool = False
    sondehub_api_base: str = DEFAULT_SONDEHUB_API_BASE
    sondehub_center_lat: float = DEFAULT_STATION_LAT
    sondehub_center_lon: float = DEFAULT_STATION_LON
    sondehub_radius_nm: float = DEFAULT_SONDEHUB_RADIUS_NM
    sondehub_recency_s: float = DEFAULT_SONDEHUB_RECENCY_S
    sondehub_poll_s: float = DEFAULT_SONDEHUB_POLL_S
    sondehub_timeout_s: float = DEFAULT_SONDEHUB_TIMEOUT_S

    #: Run the NASA FIRMS active-fire adapter alongside the backend (M5.3, PRD §11.11). Off
    #: by default — opt in with ``AETHER_FIRMS=1``. Capability-gated on a map key
    #: (``firms_map_key``); a missing key degrades to an ``offline`` status, never a crash.
    #: The AOI center defaults to the station; ``firms_api_base=fake`` (or ``firms_map_key=fake``)
    #: selects the no-hardware feeder.
    firms: bool = False
    firms_map_key: str = ""
    firms_api_base: str = DEFAULT_FIRMS_API_BASE
    firms_source: str = DEFAULT_FIRMS_SOURCE
    firms_day_range: int = DEFAULT_FIRMS_DAY_RANGE
    firms_center_lat: float = DEFAULT_STATION_LAT
    firms_center_lon: float = DEFAULT_STATION_LON
    firms_radius_nm: float = DEFAULT_FIRMS_RADIUS_NM
    firms_min_confidence: str = DEFAULT_FIRMS_MIN_CONFIDENCE
    firms_poll_s: float = DEFAULT_FIRMS_POLL_S
    firms_timeout_s: float = DEFAULT_FIRMS_TIMEOUT_S

    #: Run the NOAA GOES GLM lightning adapter alongside the backend (M5.6, PRD §11.10). Off
    #: by default — opt in with ``AETHER_GLM=1``. Benchmark-gated (``docs/glm-benchmark.md``):
    #: viable on the Pi 5 at ~1.6 GB/day. Capability-gated on the optional ``netCDF4`` parser
    #: (missing ⇒ ``offline`` status, never a crash). ``glm_satellite=fake`` (or
    #: ``glm_s3_base=fake``) selects the no-hardware feeder. The AOI center defaults to the
    #: station; transient flashes age off after ``glm_flash_ttl_s``.
    glm: bool = False
    glm_satellite: str = DEFAULT_GLM_SATELLITE
    glm_s3_base: str = DEFAULT_GLM_S3_BASE
    glm_center_lat: float = DEFAULT_STATION_LAT
    glm_center_lon: float = DEFAULT_STATION_LON
    glm_radius_nm: float = DEFAULT_GLM_RADIUS_NM
    glm_poll_s: float = DEFAULT_GLM_POLL_S
    glm_max_files_per_poll: int = DEFAULT_GLM_MAX_FILES_PER_POLL
    glm_flash_ttl_s: float = DEFAULT_GLM_FLASH_TTL_S
    glm_timeout_s: float = DEFAULT_GLM_TIMEOUT_S
    glm_good_quality_only: bool = DEFAULT_GLM_GOOD_QUALITY_ONLY

    #: FAA TFR airspace overlay (M6.1, PRD §11.13/§18.10). Off by default — opt in with
    #: ``AETHER_FAA_TFR=1``. Read-only public data (no key); ``faa_tfr_base_url=fake``
    #: selects the no-hardware feeder. ``faa_tfr_states`` (CSV) optionally pre-filters the
    #: nationwide list to the operator's region; the AOI center defaults to the station.
    faa_tfr: bool = False
    faa_tfr_base_url: str = DEFAULT_FAA_TFR_BASE_URL
    faa_tfr_center_lat: float = DEFAULT_STATION_LAT
    faa_tfr_center_lon: float = DEFAULT_STATION_LON
    faa_tfr_radius_nm: float = DEFAULT_FAA_TFR_RADIUS_NM
    faa_tfr_poll_s: float = DEFAULT_FAA_TFR_POLL_S
    faa_tfr_timeout_s: float = DEFAULT_FAA_TFR_TIMEOUT_S
    faa_tfr_max_details_per_poll: int = DEFAULT_FAA_TFR_MAX_DETAILS_PER_POLL
    faa_tfr_states: tuple[str, ...] = ()

    #: FAA NOTAM airspace overlay (M6.4, PRD §11.13/§18.11). Off by default and
    #: capability-gated (AIRSPACE-FR-008): the FAA API needs operator-supplied
    #: ``client_id``/``client_secret``. Missing creds → one ``disabled`` status, never a
    #: crash; a 401/403 → one ``offline`` status. ``faa_notam_base_url=fake`` (or either
    #: credential = ``fake``) selects the no-hardware feeder. The query radius is capped at
    #: the FAA 100 NM maximum; the AOI center defaults to the station.
    faa_notam: bool = False
    faa_notam_base_url: str = DEFAULT_FAA_NOTAM_BASE_URL
    faa_notam_client_id: str = ""
    faa_notam_client_secret: str = ""
    faa_notam_center_lat: float = DEFAULT_STATION_LAT
    faa_notam_center_lon: float = DEFAULT_STATION_LON
    faa_notam_radius_nm: float = DEFAULT_FAA_NOTAM_RADIUS_NM
    faa_notam_page_size: int = DEFAULT_FAA_NOTAM_PAGE_SIZE
    faa_notam_poll_s: float = DEFAULT_FAA_NOTAM_POLL_S
    faa_notam_timeout_s: float = DEFAULT_FAA_NOTAM_TIMEOUT_S
    faa_notam_max_pages_per_poll: int = DEFAULT_FAA_NOTAM_MAX_PAGES_PER_POLL

    #: Run the CelesTrak orbital adapter alongside the backend (M6.5, PRD §11.14). Off by
    #: default — opt in with ``AETHER_CELESTRAK=1``. Read-only public GP data (no key);
    #: capability-gated on the optional ``sgp4`` propagator (missing ⇒ ``offline`` status,
    #: never a crash). ``celestrak_base_url=fake`` selects the no-hardware feeder. The observer
    #: defaults to the canonical station location; positions are predicted and labelled so.
    celestrak: bool = False
    celestrak_base_url: str = DEFAULT_CELESTRAK_BASE_URL
    celestrak_groups: tuple[str, ...] = DEFAULT_CELESTRAK_GROUPS
    celestrak_observer_lat: float = DEFAULT_STATION_LAT
    celestrak_observer_lon: float = DEFAULT_STATION_LON
    celestrak_observer_alt_m: float = DEFAULT_CELESTRAK_OBSERVER_ALT_M
    celestrak_min_elevation_deg: float = DEFAULT_CELESTRAK_MIN_ELEVATION_DEG
    celestrak_sync_s: float = DEFAULT_CELESTRAK_SYNC_S
    celestrak_propagate_s: float = DEFAULT_CELESTRAK_PROPAGATE_S
    celestrak_valid_s: float = DEFAULT_CELESTRAK_VALID_S
    celestrak_timeout_s: float = DEFAULT_CELESTRAK_TIMEOUT_S

    #: Persist fused records to SQLite (M4, PRD §19). Off by default — persistence
    #: is a *sibling* bus consumer that never gates serving live state (PRD §5).
    #: Track history is the first consumer; retention/alerts/replay build on the same
    #: store. ``db_path`` is the SQLite file; ``persist_queue_max`` bounds the in-memory
    #: write queue so a slow disk drops records instead of back-pressuring the bus.
    persist: bool = False
    db_path: str = DEFAULT_DB_PATH
    persist_queue_max: int = DEFAULT_PERSIST_QUEUE_MAX

    #: Per-source persist-cadence sampling (M4.2b, PRD §19.5). ``persist_sample``
    #: gates how often each ``(source, identity)`` is persisted to bound DB growth
    #: proactively; ``0`` for a source disables its time gate (persist every
    #: record). Active only when ``persist`` is on; never gates serving live state.
    persist_sample: bool = DEFAULT_PERSIST_SAMPLE
    persist_sample_local_adsb_s: float = DEFAULT_PERSIST_SAMPLE_LOCAL_ADSB_S
    persist_sample_network_adsb_s: float = DEFAULT_PERSIST_SAMPLE_NETWORK_ADSB_S
    persist_sample_ais_s: float = DEFAULT_PERSIST_SAMPLE_AIS_S
    persist_sample_aprs_s: float = DEFAULT_PERSIST_SAMPLE_APRS_S
    persist_sample_default_s: float = DEFAULT_PERSIST_SAMPLE_DEFAULT_S

    #: Retention manager (M4.2, PRD §19.4). Runs as a sibling of the persistence
    #: writer (only when ``persist`` is on) on its own DB connection, so a sweep —
    #: including a VACUUM — never gates serving live state (PRD §5). Enforces the
    #: ``retention_days`` window always, and the ``db_max_gb`` / ``min_free_disk_gb``
    #: limits via the storage-pressure ladder; ``*_water`` marks set when it engages.
    retention_days: int = DEFAULT_RETENTION_DAYS
    db_max_gb: float = DEFAULT_DB_MAX_GB
    min_free_disk_gb: float = DEFAULT_MIN_FREE_DISK_GB
    retention_interval_s: float = DEFAULT_RETENTION_INTERVAL_S
    db_high_water: float = DEFAULT_DB_HIGH_WATER
    db_critical_water: float = DEFAULT_DB_CRITICAL_WATER

    #: Track-history read API (M4.3, PRD §21.3/§11.15). ``GET /api/v2/tracks/{id}/
    #: history`` reads the persistence store on a fresh read-only connection per
    #: request, so a slow/locked store can never gate serving live state (PRD §5).
    #: ``history_max_points`` caps one response (the ``truncated`` flag signals a hit).
    history_max_points: int = DEFAULT_HISTORY_MAX_POINTS

    #: Replay API bounds (M4.8, PRD §19.6/§21.6). ``replay_max_records`` clamps the
    #: reconstructed buffer one ``POST /api/v2/replay/sessions`` returns and
    #: ``replay_max_window_h`` clamps its ``[start, end)`` span — both read from a fresh
    #: read-only connection per request, so replay never gates serving live state
    #: (PRD §5). Active only when ``persist`` is on (replay 503s otherwise).
    replay_max_records: int = DEFAULT_REPLAY_MAX_RECORDS
    replay_max_window_h: float = DEFAULT_REPLAY_MAX_WINDOW_H

    #: Per-channel notification severity thresholds (M4.7, PRD §20.5). The
    #: :class:`~aether.alerts.notify.NotificationDispatcher` delivers an alert on a
    #: channel only when its severity meets the channel threshold; below it the
    #: channel resolves to ``suppressed``. ``info`` ⇒ deliver everything. The browser
    #: threshold is honored now; email/discord thresholds gate their drivers in M4.7b.
    notify_browser_min_severity: str = DEFAULT_NOTIFY_BROWSER_MIN_SEVERITY
    notify_email_min_severity: str = DEFAULT_NOTIFY_EMAIL_MIN_SEVERITY
    notify_discord_min_severity: str = DEFAULT_NOTIFY_DISCORD_MIN_SEVERITY

    #: Server-side notification drivers (M4.7b, PRD §20.4). Email is wired only when
    #: ``smtp_host`` + ``email_from`` + ``email_to`` are all set; discord only when
    #: ``discord_webhook_url`` is set (see :func:`aether.alerts.notify.
    #: drivers_from_settings`). ``smtp_password`` and ``discord_webhook_url`` are
    #: secrets — ``repr=False`` keeps them out of any accidental ``repr(cfg)``/log.
    smtp_host: str = ""
    smtp_port: int = DEFAULT_SMTP_PORT
    smtp_tls: str = DEFAULT_SMTP_TLS
    smtp_username: str = ""
    smtp_password: str = field(default="", repr=False)
    email_from: str = ""
    email_to: str = ""
    discord_webhook_url: str = field(default="", repr=False)

    @classmethod
    def from_env(cls) -> "Settings":
        # Resolve the canonical station first; the per-adapter AOI centers default
        # to it (one home position), while still honoring an explicit per-adapter
        # override for the rare multi-AOI deployment.
        station_lat = float(os.environ.get("AETHER_STATION_LAT", DEFAULT_STATION_LAT))
        station_lon = float(os.environ.get("AETHER_STATION_LON", DEFAULT_STATION_LON))
        station_radius_nm = float(
            os.environ.get("AETHER_STATION_RADIUS_NM", DEFAULT_STATION_RADIUS_NM)
        )
        return cls(
            mqtt_host=os.environ.get("AETHER_MQTT_HOST", DEFAULT_MQTT_HOST),
            mqtt_port=int(os.environ.get("AETHER_MQTT_PORT", DEFAULT_MQTT_PORT)),
            demo_source=_env_bool("AETHER_DEMO_SOURCE", True),
            station_lat=station_lat,
            station_lon=station_lon,
            station_radius_nm=station_radius_nm,
            local_adsb=_env_bool("AETHER_LOCAL_ADSB", False),
            local_adsb_source=os.environ.get("AETHER_LOCAL_ADSB_SOURCE", DEFAULT_LOCAL_ADSB_SOURCE),
            local_adsb_poll_s=float(
                os.environ.get("AETHER_LOCAL_ADSB_POLL_S", DEFAULT_LOCAL_ADSB_POLL_S)
            ),
            local_adsb_throttle_s=float(
                os.environ.get("AETHER_LOCAL_ADSB_THROTTLE_S", DEFAULT_LOCAL_ADSB_THROTTLE_S)
            ),
            local_adsb_timeout_s=float(
                os.environ.get("AETHER_LOCAL_ADSB_TIMEOUT_S", DEFAULT_LOCAL_ADSB_TIMEOUT_S)
            ),
            local_aprs=_env_bool("AETHER_LOCAL_APRS", False),
            local_aprs_host=os.environ.get("AETHER_LOCAL_APRS_HOST", DEFAULT_LOCAL_APRS_HOST),
            local_aprs_port=int(os.environ.get("AETHER_LOCAL_APRS_PORT", DEFAULT_LOCAL_APRS_PORT)),
            local_aprs_throttle_s=float(
                os.environ.get("AETHER_LOCAL_APRS_THROTTLE_S", DEFAULT_LOCAL_APRS_THROTTLE_S)
            ),
            local_aprs_timeout_s=float(
                os.environ.get("AETHER_LOCAL_APRS_TIMEOUT_S", DEFAULT_LOCAL_APRS_TIMEOUT_S)
            ),
            network_adsb=_env_bool("AETHER_NETWORK_ADSB", False),
            network_adsb_provider=os.environ.get(
                "AETHER_NETWORK_ADSB_PROVIDER", DEFAULT_NETWORK_ADSB_PROVIDER
            ),
            network_adsb_center_lat=float(os.environ.get("AETHER_NETWORK_ADSB_LAT", station_lat)),
            network_adsb_center_lon=float(os.environ.get("AETHER_NETWORK_ADSB_LON", station_lon)),
            network_adsb_radius_nm=float(
                os.environ.get("AETHER_NETWORK_ADSB_RADIUS_NM", DEFAULT_NETWORK_ADSB_RADIUS_NM)
            ),
            network_adsb_poll_s=float(
                os.environ.get("AETHER_NETWORK_ADSB_POLL_S", DEFAULT_NETWORK_ADSB_POLL_S)
            ),
            network_adsb_rate_limit_s=float(
                os.environ.get(
                    "AETHER_NETWORK_ADSB_RATE_LIMIT_S", DEFAULT_NETWORK_ADSB_RATE_LIMIT_S
                )
            ),
            network_adsb_timeout_s=float(
                os.environ.get("AETHER_NETWORK_ADSB_TIMEOUT_S", DEFAULT_NETWORK_ADSB_TIMEOUT_S)
            ),
            mil_icao_blocks=os.environ.get("AETHER_MIL_ICAO_BLOCKS", DEFAULT_MIL_ICAO_BLOCKS),
            aprs_is=_env_bool("AETHER_APRS_IS", False),
            aprs_is_host=os.environ.get("AETHER_APRS_IS_HOST", DEFAULT_APRS_IS_HOST),
            aprs_is_port=int(os.environ.get("AETHER_APRS_IS_PORT", DEFAULT_APRS_IS_PORT)),
            aprs_is_callsign=os.environ.get("AETHER_APRS_IS_CALLSIGN", DEFAULT_APRS_IS_CALLSIGN),
            aprs_is_passcode=os.environ.get("AETHER_APRS_IS_PASSCODE", DEFAULT_APRS_IS_PASSCODE),
            aprs_is_center_lat=float(os.environ.get("AETHER_APRS_IS_LAT", station_lat)),
            aprs_is_center_lon=float(os.environ.get("AETHER_APRS_IS_LON", station_lon)),
            aprs_is_radius_nm=float(
                os.environ.get("AETHER_APRS_IS_RADIUS_NM", DEFAULT_APRS_IS_RADIUS_NM)
            ),
            aprs_is_throttle_s=float(
                os.environ.get("AETHER_APRS_IS_THROTTLE_S", DEFAULT_APRS_IS_THROTTLE_S)
            ),
            aprs_is_timeout_s=float(
                os.environ.get("AETHER_APRS_IS_TIMEOUT_S", DEFAULT_APRS_IS_TIMEOUT_S)
            ),
            aprs_is_stall_s=float(
                os.environ.get("AETHER_APRS_IS_STALL_S", DEFAULT_APRS_IS_STALL_S)
            ),
            ais=_env_bool("AETHER_AIS", False),
            ais_host=os.environ.get("AETHER_AIS_HOST", DEFAULT_AIS_HOST),
            ais_port=int(os.environ.get("AETHER_AIS_PORT", DEFAULT_AIS_PORT)),
            ais_path=os.environ.get("AETHER_AIS_PATH", DEFAULT_AIS_PATH),
            ais_tls=_env_bool("AETHER_AIS_TLS", DEFAULT_AIS_TLS),
            ais_api_key=os.environ.get("AETHER_AIS_API_KEY", DEFAULT_AIS_API_KEY),
            ais_center_lat=float(os.environ.get("AETHER_AIS_LAT", station_lat)),
            ais_center_lon=float(os.environ.get("AETHER_AIS_LON", station_lon)),
            ais_radius_nm=float(os.environ.get("AETHER_AIS_RADIUS_NM", DEFAULT_AIS_RADIUS_NM)),
            ais_throttle_s=float(os.environ.get("AETHER_AIS_THROTTLE_S", DEFAULT_AIS_THROTTLE_S)),
            ais_timeout_s=float(os.environ.get("AETHER_AIS_TIMEOUT_S", DEFAULT_AIS_TIMEOUT_S)),
            usgs=_env_bool("AETHER_USGS", False),
            usgs_feed_url=os.environ.get("AETHER_USGS_FEED_URL", DEFAULT_USGS_FEED_URL),
            usgs_center_lat=float(os.environ.get("AETHER_USGS_LAT", station_lat)),
            usgs_center_lon=float(os.environ.get("AETHER_USGS_LON", station_lon)),
            usgs_radius_nm=float(os.environ.get("AETHER_USGS_RADIUS_NM", DEFAULT_USGS_RADIUS_NM)),
            usgs_min_magnitude=float(
                os.environ.get("AETHER_USGS_MIN_MAGNITUDE", DEFAULT_USGS_MIN_MAGNITUDE)
            ),
            usgs_poll_s=float(os.environ.get("AETHER_USGS_POLL_S", DEFAULT_USGS_POLL_S)),
            usgs_timeout_s=float(os.environ.get("AETHER_USGS_TIMEOUT_S", DEFAULT_USGS_TIMEOUT_S)),
            sondehub=_env_bool("AETHER_SONDEHUB", False),
            sondehub_api_base=os.environ.get("AETHER_SONDEHUB_API_BASE", DEFAULT_SONDEHUB_API_BASE),
            sondehub_center_lat=float(os.environ.get("AETHER_SONDEHUB_LAT", station_lat)),
            sondehub_center_lon=float(os.environ.get("AETHER_SONDEHUB_LON", station_lon)),
            sondehub_radius_nm=float(
                os.environ.get("AETHER_SONDEHUB_RADIUS_NM", DEFAULT_SONDEHUB_RADIUS_NM)
            ),
            sondehub_recency_s=float(
                os.environ.get("AETHER_SONDEHUB_RECENCY_S", DEFAULT_SONDEHUB_RECENCY_S)
            ),
            sondehub_poll_s=float(
                os.environ.get("AETHER_SONDEHUB_POLL_S", DEFAULT_SONDEHUB_POLL_S)
            ),
            sondehub_timeout_s=float(
                os.environ.get("AETHER_SONDEHUB_TIMEOUT_S", DEFAULT_SONDEHUB_TIMEOUT_S)
            ),
            firms=_env_bool("AETHER_FIRMS", False),
            firms_map_key=os.environ.get("AETHER_FIRMS_MAP_KEY", ""),
            firms_api_base=os.environ.get("AETHER_FIRMS_API_BASE", DEFAULT_FIRMS_API_BASE),
            firms_source=os.environ.get("AETHER_FIRMS_SOURCE", DEFAULT_FIRMS_SOURCE),
            firms_day_range=int(os.environ.get("AETHER_FIRMS_DAY_RANGE", DEFAULT_FIRMS_DAY_RANGE)),
            firms_center_lat=float(os.environ.get("AETHER_FIRMS_LAT", station_lat)),
            firms_center_lon=float(os.environ.get("AETHER_FIRMS_LON", station_lon)),
            firms_radius_nm=float(
                os.environ.get("AETHER_FIRMS_RADIUS_NM", DEFAULT_FIRMS_RADIUS_NM)
            ),
            firms_min_confidence=os.environ.get(
                "AETHER_FIRMS_MIN_CONFIDENCE", DEFAULT_FIRMS_MIN_CONFIDENCE
            ),
            firms_poll_s=float(os.environ.get("AETHER_FIRMS_POLL_S", DEFAULT_FIRMS_POLL_S)),
            firms_timeout_s=float(
                os.environ.get("AETHER_FIRMS_TIMEOUT_S", DEFAULT_FIRMS_TIMEOUT_S)
            ),
            glm=_env_bool("AETHER_GLM", False),
            glm_satellite=os.environ.get("AETHER_GLM_SATELLITE", DEFAULT_GLM_SATELLITE),
            glm_s3_base=os.environ.get("AETHER_GLM_S3_BASE", DEFAULT_GLM_S3_BASE),
            glm_center_lat=float(os.environ.get("AETHER_GLM_LAT", station_lat)),
            glm_center_lon=float(os.environ.get("AETHER_GLM_LON", station_lon)),
            glm_radius_nm=float(os.environ.get("AETHER_GLM_RADIUS_NM", DEFAULT_GLM_RADIUS_NM)),
            glm_poll_s=float(os.environ.get("AETHER_GLM_POLL_S", DEFAULT_GLM_POLL_S)),
            glm_max_files_per_poll=int(
                os.environ.get("AETHER_GLM_MAX_FILES_PER_POLL", DEFAULT_GLM_MAX_FILES_PER_POLL)
            ),
            glm_flash_ttl_s=float(
                os.environ.get("AETHER_GLM_FLASH_TTL_S", DEFAULT_GLM_FLASH_TTL_S)
            ),
            glm_timeout_s=float(os.environ.get("AETHER_GLM_TIMEOUT_S", DEFAULT_GLM_TIMEOUT_S)),
            glm_good_quality_only=_env_bool(
                "AETHER_GLM_GOOD_QUALITY_ONLY", DEFAULT_GLM_GOOD_QUALITY_ONLY
            ),
            faa_tfr=_env_bool("AETHER_FAA_TFR", False),
            faa_tfr_base_url=os.environ.get("AETHER_FAA_TFR_BASE_URL", DEFAULT_FAA_TFR_BASE_URL),
            faa_tfr_center_lat=float(os.environ.get("AETHER_FAA_TFR_LAT", station_lat)),
            faa_tfr_center_lon=float(os.environ.get("AETHER_FAA_TFR_LON", station_lon)),
            faa_tfr_radius_nm=float(
                os.environ.get("AETHER_FAA_TFR_RADIUS_NM", DEFAULT_FAA_TFR_RADIUS_NM)
            ),
            faa_tfr_poll_s=float(os.environ.get("AETHER_FAA_TFR_POLL_S", DEFAULT_FAA_TFR_POLL_S)),
            faa_tfr_timeout_s=float(
                os.environ.get("AETHER_FAA_TFR_TIMEOUT_S", DEFAULT_FAA_TFR_TIMEOUT_S)
            ),
            faa_tfr_max_details_per_poll=int(
                os.environ.get(
                    "AETHER_FAA_TFR_MAX_DETAILS_PER_POLL", DEFAULT_FAA_TFR_MAX_DETAILS_PER_POLL
                )
            ),
            faa_tfr_states=tuple(
                s.strip().upper()
                for s in os.environ.get("AETHER_FAA_TFR_STATES", "").split(",")
                if s.strip()
            ),
            faa_notam=_env_bool("AETHER_FAA_NOTAM", False),
            faa_notam_base_url=os.environ.get(
                "AETHER_FAA_NOTAM_BASE_URL", DEFAULT_FAA_NOTAM_BASE_URL
            ),
            faa_notam_client_id=os.environ.get("AETHER_FAA_NOTAM_CLIENT_ID", ""),
            faa_notam_client_secret=os.environ.get("AETHER_FAA_NOTAM_CLIENT_SECRET", ""),
            faa_notam_center_lat=float(os.environ.get("AETHER_FAA_NOTAM_LAT", station_lat)),
            faa_notam_center_lon=float(os.environ.get("AETHER_FAA_NOTAM_LON", station_lon)),
            faa_notam_radius_nm=float(
                os.environ.get("AETHER_FAA_NOTAM_RADIUS_NM", DEFAULT_FAA_NOTAM_RADIUS_NM)
            ),
            faa_notam_page_size=int(
                os.environ.get("AETHER_FAA_NOTAM_PAGE_SIZE", DEFAULT_FAA_NOTAM_PAGE_SIZE)
            ),
            faa_notam_poll_s=float(
                os.environ.get("AETHER_FAA_NOTAM_POLL_S", DEFAULT_FAA_NOTAM_POLL_S)
            ),
            faa_notam_timeout_s=float(
                os.environ.get("AETHER_FAA_NOTAM_TIMEOUT_S", DEFAULT_FAA_NOTAM_TIMEOUT_S)
            ),
            faa_notam_max_pages_per_poll=int(
                os.environ.get(
                    "AETHER_FAA_NOTAM_MAX_PAGES_PER_POLL", DEFAULT_FAA_NOTAM_MAX_PAGES_PER_POLL
                )
            ),
            celestrak=_env_bool("AETHER_CELESTRAK", False),
            celestrak_base_url=os.environ.get(
                "AETHER_CELESTRAK_BASE_URL", DEFAULT_CELESTRAK_BASE_URL
            ),
            celestrak_groups=tuple(
                g.strip()
                for g in os.environ.get(
                    "AETHER_CELESTRAK_GROUPS", ",".join(DEFAULT_CELESTRAK_GROUPS)
                ).split(",")
                if g.strip()
            )
            or DEFAULT_CELESTRAK_GROUPS,
            celestrak_observer_lat=float(os.environ.get("AETHER_CELESTRAK_LAT", station_lat)),
            celestrak_observer_lon=float(os.environ.get("AETHER_CELESTRAK_LON", station_lon)),
            celestrak_observer_alt_m=float(
                os.environ.get("AETHER_CELESTRAK_ALT_M", DEFAULT_CELESTRAK_OBSERVER_ALT_M)
            ),
            celestrak_min_elevation_deg=float(
                os.environ.get(
                    "AETHER_CELESTRAK_MIN_ELEVATION_DEG", DEFAULT_CELESTRAK_MIN_ELEVATION_DEG
                )
            ),
            celestrak_sync_s=float(
                os.environ.get("AETHER_CELESTRAK_SYNC_S", DEFAULT_CELESTRAK_SYNC_S)
            ),
            celestrak_propagate_s=float(
                os.environ.get("AETHER_CELESTRAK_PROPAGATE_S", DEFAULT_CELESTRAK_PROPAGATE_S)
            ),
            celestrak_valid_s=float(
                os.environ.get("AETHER_CELESTRAK_VALID_S", DEFAULT_CELESTRAK_VALID_S)
            ),
            celestrak_timeout_s=float(
                os.environ.get("AETHER_CELESTRAK_TIMEOUT_S", DEFAULT_CELESTRAK_TIMEOUT_S)
            ),
            persist=_env_bool("AETHER_PERSIST", False),
            db_path=os.environ.get("AETHER_DB_PATH", DEFAULT_DB_PATH),
            persist_queue_max=int(
                os.environ.get("AETHER_PERSIST_QUEUE_MAX", DEFAULT_PERSIST_QUEUE_MAX)
            ),
            persist_sample=_env_bool("AETHER_PERSIST_SAMPLE", DEFAULT_PERSIST_SAMPLE),
            persist_sample_local_adsb_s=float(
                os.environ.get(
                    "AETHER_PERSIST_SAMPLE_LOCAL_ADSB_S", DEFAULT_PERSIST_SAMPLE_LOCAL_ADSB_S
                )
            ),
            persist_sample_network_adsb_s=float(
                os.environ.get(
                    "AETHER_PERSIST_SAMPLE_NETWORK_ADSB_S", DEFAULT_PERSIST_SAMPLE_NETWORK_ADSB_S
                )
            ),
            persist_sample_ais_s=float(
                os.environ.get("AETHER_PERSIST_SAMPLE_AIS_S", DEFAULT_PERSIST_SAMPLE_AIS_S)
            ),
            persist_sample_aprs_s=float(
                os.environ.get("AETHER_PERSIST_SAMPLE_APRS_S", DEFAULT_PERSIST_SAMPLE_APRS_S)
            ),
            persist_sample_default_s=float(
                os.environ.get("AETHER_PERSIST_SAMPLE_DEFAULT_S", DEFAULT_PERSIST_SAMPLE_DEFAULT_S)
            ),
            retention_days=int(os.environ.get("AETHER_RETENTION_DAYS", DEFAULT_RETENTION_DAYS)),
            db_max_gb=float(os.environ.get("AETHER_DB_MAX_GB", DEFAULT_DB_MAX_GB)),
            min_free_disk_gb=float(
                os.environ.get("AETHER_MIN_FREE_DISK_GB", DEFAULT_MIN_FREE_DISK_GB)
            ),
            retention_interval_s=float(
                os.environ.get("AETHER_RETENTION_INTERVAL_S", DEFAULT_RETENTION_INTERVAL_S)
            ),
            db_high_water=float(os.environ.get("AETHER_DB_HIGH_WATER", DEFAULT_DB_HIGH_WATER)),
            db_critical_water=float(
                os.environ.get("AETHER_DB_CRITICAL_WATER", DEFAULT_DB_CRITICAL_WATER)
            ),
            history_max_points=int(
                os.environ.get("AETHER_HISTORY_MAX_POINTS", DEFAULT_HISTORY_MAX_POINTS)
            ),
            replay_max_records=int(
                os.environ.get("AETHER_REPLAY_MAX_RECORDS", DEFAULT_REPLAY_MAX_RECORDS)
            ),
            replay_max_window_h=float(
                os.environ.get("AETHER_REPLAY_MAX_WINDOW_H", DEFAULT_REPLAY_MAX_WINDOW_H)
            ),
            notify_browser_min_severity=os.environ.get(
                "AETHER_NOTIFY_BROWSER_MIN_SEVERITY", DEFAULT_NOTIFY_BROWSER_MIN_SEVERITY
            ),
            notify_email_min_severity=os.environ.get(
                "AETHER_NOTIFY_EMAIL_MIN_SEVERITY", DEFAULT_NOTIFY_EMAIL_MIN_SEVERITY
            ),
            notify_discord_min_severity=os.environ.get(
                "AETHER_NOTIFY_DISCORD_MIN_SEVERITY", DEFAULT_NOTIFY_DISCORD_MIN_SEVERITY
            ),
            smtp_host=os.environ.get("AETHER_SMTP_HOST", ""),
            smtp_port=int(os.environ.get("AETHER_SMTP_PORT", DEFAULT_SMTP_PORT)),
            smtp_tls=os.environ.get("AETHER_SMTP_TLS", DEFAULT_SMTP_TLS),
            smtp_username=os.environ.get("AETHER_SMTP_USERNAME", ""),
            smtp_password=os.environ.get("AETHER_SMTP_PASSWORD", ""),
            email_from=os.environ.get("AETHER_EMAIL_FROM", ""),
            email_to=os.environ.get("AETHER_EMAIL_TO", ""),
            discord_webhook_url=os.environ.get("AETHER_DISCORD_WEBHOOK_URL", ""),
        )

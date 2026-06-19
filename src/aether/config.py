"""Process configuration from the environment (PRD §22 env table).

Only the keys M1 actually needs live here — the MQTT broker endpoint and the
no-hardware demo toggle. Later milestones extend this with home location, DB
paths, and per-source credentials; each is read at the edge that uses it, never
sprinkled through the backend. Everything has a safe loopback default so the
no-hardware demo runs with no environment set.
"""

import os
from dataclasses import dataclass

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

#: Hard cap on observations returned by one ``/api/v2/tracks/{id}/history`` request
#: (M4.3, PRD §21.3/§11.15). The request's ``limit`` query param defaults to and is
#: clamped to this, bounding response size + read cost on the Pi (PRD §37); the
#: response flags ``truncated`` when the cap is hit so a capped trail is never
#: mistaken for a complete one (no silent caps). The read is served on a fresh
#: read-only connection per request and never gates serving live state (PRD §5).
DEFAULT_HISTORY_MAX_POINTS = 10000


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
        )

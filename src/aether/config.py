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

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            mqtt_host=os.environ.get("AETHER_MQTT_HOST", DEFAULT_MQTT_HOST),
            mqtt_port=int(os.environ.get("AETHER_MQTT_PORT", DEFAULT_MQTT_PORT)),
            demo_source=_env_bool("AETHER_DEMO_SOURCE", True),
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
            network_adsb_center_lat=float(
                os.environ.get("AETHER_NETWORK_ADSB_LAT", DEFAULT_NETWORK_ADSB_LAT)
            ),
            network_adsb_center_lon=float(
                os.environ.get("AETHER_NETWORK_ADSB_LON", DEFAULT_NETWORK_ADSB_LON)
            ),
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
        )

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
        )

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

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            mqtt_host=os.environ.get("AETHER_MQTT_HOST", DEFAULT_MQTT_HOST),
            mqtt_port=int(os.environ.get("AETHER_MQTT_PORT", DEFAULT_MQTT_PORT)),
            demo_source=_env_bool("AETHER_DEMO_SOURCE", True),
        )

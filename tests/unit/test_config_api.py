"""GET /api/config shape: the station origin (M3.6a) + the orbital descriptor (M6.6a).

Hermetic like :mod:`tests.unit.test_frontend_serve`: the :class:`TestClient` runs WITHOUT
its context manager, so the app lifespan (MQTT subscriber) never starts and only the route
handler — driven purely by the injected :class:`Settings` — is exercised.
"""

from typing import Any

from fastapi.testclient import TestClient

from aether.backend.main import create_app
from aether.config import Settings


def _config(cfg: Settings) -> dict[str, Any]:
    # no `with` → lifespan (broker subscriber) never runs; the route still serves.
    client = TestClient(create_app(settings=cfg))
    resp = client.get("/api/config")
    assert resp.status_code == 200
    body: dict[str, Any] = resp.json()
    return body


def test_orbital_block_present_and_enabled_when_celestrak_on() -> None:
    cfg = Settings(
        demo_source=False,
        persist=False,
        celestrak=True,
        celestrak_groups=("stations", "amateur"),
        celestrak_min_elevation_deg=15.0,
    )
    orbital = _config(cfg)["orbital"]
    assert orbital == {
        "enabled": True,
        "groups": ["stations", "amateur"],  # tuple → list, in order
        "min_elevation_deg": 15.0,
    }


def test_orbital_block_always_present_when_celestrak_off() -> None:
    # The block is ALWAYS present (enabled:false) so the UI can decide whether to render
    # the orbital controls; groups/floor still reflect the defaults.
    cfg = Settings(demo_source=False, persist=False, celestrak=False)
    orbital = _config(cfg)["orbital"]
    assert orbital["enabled"] is False
    assert orbital["groups"] == list(cfg.celestrak_groups)
    assert orbital["min_elevation_deg"] == cfg.celestrak_min_elevation_deg


def test_station_key_unchanged_alongside_orbital() -> None:
    # The orbital addition does not disturb the existing station origin contract.
    null_station = _config(Settings(demo_source=False, persist=False))
    assert null_station["station"] is None  # 0,0 → null (not centred on null island)

    configured = _config(
        Settings(
            demo_source=False,
            persist=False,
            station_lat=41.1,
            station_lon=-94.6,
            station_radius_nm=20.0,
        )
    )
    assert configured["station"] == {"lat": 41.1, "lon": -94.6, "radius_nm": 20.0}
    assert "orbital" in configured  # still present regardless of station

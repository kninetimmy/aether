"""End-to-end: CelesTrak orbital objects -> MQTT -> live state -> ws (PRD §31.3, §32 M6).

The M6.5 path with no network (but the REAL SGP4 propagate path): the adapter runs the
in-process fake CelesTrak provider, whose canned OMM roster is solved relative to the AOI/
observer so one synthetic GEO sits reliably above the horizon and another sits below it. The
backend must carry the above-horizon object through the bus into live state and out over
/ws/v2 as a ``track_upsert`` ``orbital_object`` — predicted, with CelesTrak attribution and
the not-for-navigation caveat, az/el/range in attributes — while the below-horizon object
never appears (the elevation filter end to end).

Needs the optional ``[orbital]`` ``sgp4`` extra (the fake feeder drives the real propagate
path). Skips when no broker is reachable (see conftest); CI starts Mosquitto so it runs.
"""

import dataclasses
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from aether.config import Settings
from aether.persist.database import Database
from aether.persist.watchlist import upsert_watchlist_entry
from aether.schema.watchlist import WatchlistEntry, WatchlistEntryCreate

pytest.importorskip("sgp4")

from aether.backend.main import create_app  # noqa: E402  (after importorskip gate)

OBS_LAT, OBS_LON = 30.0, -97.0


def _app(settings: Settings) -> TestClient:
    # Demo off; only the CelesTrak adapter on, running the fake provider at a fast cadence.
    cfg = dataclasses.replace(
        settings,
        demo_source=False,
        celestrak=True,
        celestrak_base_url="fake",  # no-hardware feeder, drives the real SGP4 path
        celestrak_observer_lat=OBS_LAT,
        celestrak_observer_lon=OBS_LON,
        celestrak_min_elevation_deg=10.0,
        celestrak_propagate_s=0.05,
        celestrak_sync_s=1e9,  # one sync for the test
    )
    return TestClient(create_app(settings=cfg))


def test_above_horizon_object_reaches_the_ws_with_attribution(broker_settings: Settings) -> None:
    with _app(broker_settings) as client, client.websocket_connect("/ws/v2") as ws:
        ws.receive_json()  # snapshot
        orbital = None
        for _ in range(600):
            msg = ws.receive_json()
            rec = msg["record"] if msg["type"] == "track_upsert" else {}
            if rec.get("track_type") == "orbital_object":
                orbital = rec
                break
        assert orbital is not None, "orbital_object track never arrived over the ws"

        assert orbital["predicted"] is True
        assert orbital["locally_received"] is False
        assert orbital["valid_until"] is not None
        attrs = orbital["attributes"]
        assert attrs["attribution"].startswith("Orbital data: CelesTrak")
        assert "not for navigation" in attrs["caveat"].lower()
        assert attrs["elevation_deg"] >= 10.0
        assert "azimuth_deg" in attrs and "slant_range_m" in attrs
        assert "element_epoch_utc" in attrs and "element_age_s" in attrs

        # Live state holds the above-horizon object(s); the below-horizon GEO is filtered out.
        tracks = client.get("/api/state").json()["tracks"]
        orbitals = [t for t in tracks if t["track_type"] == "orbital_object"]
        assert orbitals, "no orbital_object in live state"
        names = {t["attributes"]["object_name"] for t in orbitals}
        assert "AETHER-GEO-FAR" not in names  # below the horizon — never emitted
        for t in orbitals:
            assert t["attributes"]["elevation_deg"] >= 10.0


def test_missing_sgp4_yields_one_offline_status_over_the_ws(
    broker_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Capability gate end to end: with sgp4 "unavailable" (build_satrec raising), the adapter
    # must publish exactly one offline source status through the bus to /ws/v2, then exit
    # cleanly — no plotted orbit, no spin (mirrors the GLM/FIRMS stance, PRD §2/§37).
    from aether.adapters import celestrak as mod
    from aether.orbital.sgp4_propagate import Sgp4Unavailable

    def _no_sgp4(fields: object) -> object:
        raise Sgp4Unavailable("sgp4 not installed")

    monkeypatch.setattr(mod, "build_satrec", _no_sgp4)

    with _app(broker_settings) as client, client.websocket_connect("/ws/v2") as ws:
        ws.receive_json()  # snapshot
        offline = None
        for _ in range(600):
            msg = ws.receive_json()
            rec = msg.get("record", {})
            if (
                msg["type"] == "source_status"
                and rec.get("source") == "celestrak"
                and rec.get("status") == "offline"
            ):
                offline = rec
                break
            assert rec.get("track_type") != "orbital_object", "plotted an orbit despite no sgp4"
        assert offline is not None, "no offline status reached the ws when sgp4 was unavailable"
        assert offline["error_code"] == "Sgp4Unavailable"


def test_watchlisted_object_propagates_on_fast_tier_end_to_end(
    broker_settings: Settings, tmp_path: Path
) -> None:
    # End-to-end two-tier (ORBIT-FR-011, M6.6b Part B): a persisted orbital watchlist entry must
    # drive the REAL list_watchlist read → cfg.db_path → partition → fast tier. We seed a migrated
    # store with the GEO-OVERHEAD (99001) on the watchlist, run the fake feeder with persist on,
    # and confirm a celestrak source_status carries fast_tracked >= 1 AND the 99001 track arrives
    # with attribution/caveat — proving the real persistence→adapter wiring, not just the loop.
    db = tmp_path / "aether.db"
    store = Database(str(db))
    store.open()  # migration v4 creates the watchlist table
    store.close()
    upsert_watchlist_entry(
        str(db),
        WatchlistEntry.create(
            WatchlistEntryCreate(label="GEO"),
            key="orbital:celestrak:99001",
            now=datetime.now(UTC),
        ),
    )

    cfg = dataclasses.replace(
        broker_settings,
        demo_source=False,
        celestrak=True,
        celestrak_base_url="fake",  # no-hardware feeder, drives the real SGP4 path
        celestrak_observer_lat=OBS_LAT,
        celestrak_observer_lon=OBS_LON,
        celestrak_min_elevation_deg=10.0,
        celestrak_propagate_s=0.05,
        celestrak_propagate_fast_s=0.05,
        celestrak_watchlist_refresh_s=0.05,
        celestrak_sync_s=1e9,  # one sync for the test
        persist=True,
        db_path=str(db),
    )

    with TestClient(create_app(settings=cfg)) as client, client.websocket_connect("/ws/v2") as ws:
        ws.receive_json()  # snapshot
        fast_status = None
        orbital = None
        for _ in range(800):
            msg = ws.receive_json()
            rec = msg.get("record", {})
            if (
                msg["type"] == "source_status"
                and rec.get("source") == "celestrak"
                and rec.get("attributes", {}).get("fast_tracked", 0) >= 1
            ):
                fast_status = rec
            if msg["type"] == "track_upsert" and rec.get("track_type") == "orbital_object":
                if rec.get("attributes", {}).get("norad_id") == 99001:
                    orbital = rec
            if fast_status is not None and orbital is not None:
                break

        assert fast_status is not None, "no celestrak status reported fast_tracked >= 1"
        assert fast_status["attributes"]["watchlisted"] >= 1
        assert orbital is not None, "watchlisted orbital_object (99001) never arrived over the ws"
        assert orbital["predicted"] is True
        attrs = orbital["attributes"]
        assert attrs["attribution"].startswith("Orbital data: CelesTrak")
        assert "not for navigation" in attrs["caveat"].lower()

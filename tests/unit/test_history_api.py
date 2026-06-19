"""Endpoint tests for the track read API (M4.3, PRD §21.3/§11.15).

Hermetic: the :class:`TestClient` is used WITHOUT its context manager so the app
lifespan (and thus the MQTT subscriber) never starts — these exercise only the REST
read path against a pre-populated temp store, so they run in the unit suite with no
broker. The end-to-end bus→persist→read path is covered in the integration suite.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from aether.backend.main import create_app
from aether.config import Settings
from aether.persist.database import Database, ObservationRow

T0 = datetime(2026, 6, 19, 12, 0, 0, tzinfo=UTC)
CORR = "aircraft:icao:abc"


def _obs(
    *, observed_at: datetime, correlation_key: str | None = CORR, record_id: str = "r"
) -> ObservationRow:
    iso = observed_at.isoformat()
    return ObservationRow(
        record_id=record_id,
        correlation_key=correlation_key,
        kind="track",
        track_type="aircraft",
        source="local_adsb",
        lon=-95.0,
        lat=40.0,
        alt_m=3000.0,
        observed_at=iso,
        received_at=iso,
        persisted_at=iso,
        payload="{}",
    )


def _seed(tmp_path: Path, rows: list[ObservationRow]) -> str:
    path = str(tmp_path / "history.db")
    db = Database(path)
    db.open()
    db.insert_observations(rows)
    db.close()
    return path


def _client(*, db_path: str | None, persist: bool, history_max_points: int = 10000) -> TestClient:
    settings = Settings(
        demo_source=False,
        persist=persist,
        db_path=db_path if db_path is not None else "unused.db",
        history_max_points=history_max_points,
    )
    return TestClient(create_app(settings=settings))  # no `with` → lifespan not run


def test_history_returns_points_oldest_first(tmp_path: Path) -> None:
    path = _seed(tmp_path, [_obs(observed_at=T0 + timedelta(seconds=s)) for s in (20, 0, 10)])
    client = _client(db_path=path, persist=True)
    body = client.get(f"/api/v2/tracks/{CORR}/history").json()
    assert body["track_id"] == CORR
    assert body["count"] == 3
    assert body["truncated"] is False
    observed = [p["observed_at"] for p in body["points"]]
    assert observed == sorted(observed)
    # The lightweight projection carries the trail fields, not the full payload.
    assert set(body["points"][0]) == {
        "observed_at",
        "received_at",
        "source",
        "track_type",
        "lon",
        "lat",
        "alt_m",
    }


def test_history_time_window_filters(tmp_path: Path) -> None:
    path = _seed(tmp_path, [_obs(observed_at=T0 + timedelta(seconds=s)) for s in range(5)])
    client = _client(db_path=path, persist=True)
    start = (T0 + timedelta(seconds=1)).isoformat()
    end = (T0 + timedelta(seconds=4)).isoformat()
    body = client.get(f"/api/v2/tracks/{CORR}/history", params={"start": start, "end": end}).json()
    assert body["count"] == 3  # [start, end): T0+1,+2,+3


def test_history_window_accepts_z_suffix_and_offset(tmp_path: Path) -> None:
    # Bounds in any ISO form normalize to the store's UTC form before comparing.
    path = _seed(tmp_path, [_obs(observed_at=T0 + timedelta(seconds=s)) for s in range(5)])
    client = _client(db_path=path, persist=True)
    body = client.get(
        f"/api/v2/tracks/{CORR}/history",
        params={"start": "2026-06-19T12:00:02Z"},  # == T0+2
    ).json()
    assert body["count"] == 3  # T0+2,+3,+4


def test_history_limit_caps_and_flags_truncated(tmp_path: Path) -> None:
    path = _seed(tmp_path, [_obs(observed_at=T0 + timedelta(seconds=s)) for s in range(10)])
    client = _client(db_path=path, persist=True)
    body = client.get(f"/api/v2/tracks/{CORR}/history", params={"limit": 3}).json()
    assert body["count"] == 3
    assert body["limit"] == 3
    assert body["truncated"] is True
    # Keeps the three most recent, oldest-first.
    assert [p["observed_at"] for p in body["points"]] == [
        (T0 + timedelta(seconds=s)).isoformat() for s in (7, 8, 9)
    ]


def test_history_request_limit_clamped_to_config_cap(tmp_path: Path) -> None:
    path = _seed(tmp_path, [_obs(observed_at=T0)])
    client = _client(db_path=path, persist=True, history_max_points=2)
    body = client.get(f"/api/v2/tracks/{CORR}/history", params={"limit": 1000}).json()
    assert body["limit"] == 2  # clamped down to the configured cap


def test_history_unknown_track_is_empty(tmp_path: Path) -> None:
    path = _seed(tmp_path, [_obs(observed_at=T0)])
    client = _client(db_path=path, persist=True)
    body = client.get("/api/v2/tracks/no-such-id/history").json()
    assert body["count"] == 0
    assert body["points"] == []


def test_history_missing_store_degrades_to_empty(tmp_path: Path) -> None:
    # Persistence on but nothing written yet (no DB file) → empty, never a 500.
    client = _client(db_path=str(tmp_path / "never.db"), persist=True)
    resp = client.get(f"/api/v2/tracks/{CORR}/history")
    assert resp.status_code == 200
    assert resp.json()["count"] == 0


def test_history_503_when_persistence_disabled(tmp_path: Path) -> None:
    client = _client(db_path=None, persist=False)
    resp = client.get(f"/api/v2/tracks/{CORR}/history")
    assert resp.status_code == 503


def test_history_400_on_bad_timestamp(tmp_path: Path) -> None:
    path = _seed(tmp_path, [_obs(observed_at=T0)])
    client = _client(db_path=path, persist=True)
    resp = client.get(f"/api/v2/tracks/{CORR}/history", params={"start": "not-a-time"})
    assert resp.status_code == 400


def test_history_422_on_nonpositive_limit(tmp_path: Path) -> None:
    path = _seed(tmp_path, [_obs(observed_at=T0)])
    client = _client(db_path=path, persist=True)
    assert client.get(f"/api/v2/tracks/{CORR}/history", params={"limit": 0}).status_code == 422


def test_track_detail_404_when_not_live(tmp_path: Path) -> None:
    # No lifespan → live state is empty, so any detail lookup is a clean 404
    # (not a crash). The 200 path is covered end-to-end in the integration suite.
    client = _client(db_path=str(tmp_path / "x.db"), persist=True)
    assert client.get(f"/api/v2/tracks/{CORR}").status_code == 404

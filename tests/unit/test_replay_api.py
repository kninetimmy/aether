"""Endpoint tests for the replay session API (M4.8, PRD §19.6/§21.6).

Hermetic like the history/geofence API tests: the :class:`TestClient` runs WITHOUT
its context manager so the app lifespan (MQTT subscriber, persistence writer) never
starts — these exercise only the REST replay path against a pre-populated temp store.
The end-to-end bus→persist→replay path and the "no live alerts" invariant are in the
integration suite.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from aether.backend.main import create_app
from aether.config import Settings
from aether.persist.database import Database, ObservationRow
from aether.persist.writer import to_observation_row
from aether.replay.session import DEFAULT_MAX_SESSIONS
from aether.schema.geometry import Point
from aether.schema.provenance import Provenance
from aether.schema.records import TrackRecord

T0 = datetime(2026, 6, 19, 12, 0, 0, tzinfo=UTC)


def _track(*, record_id: str, observed_at: datetime, source: str = "local_adsb") -> TrackRecord:
    return TrackRecord(
        id=record_id,
        source=source,
        observed_at=observed_at,
        received_at=observed_at,
        published_at=observed_at,
        correlation_key=f"aircraft:icao:{record_id}",
        track_type="aircraft",
        geometry=Point(coordinates=[-95.0, 40.0, 3000.0]),
        altitude_m=3000.0,
        locally_received=source == "local_adsb",
        provenance=[
            Provenance(
                source=source, observed_at=observed_at, received_at=observed_at, local_rf=True
            )
        ],
    )


def _obs(*, record_id: str, observed_at: datetime, source: str = "local_adsb") -> ObservationRow:
    row = to_observation_row(
        _track(record_id=record_id, observed_at=observed_at, source=source), now=observed_at
    )
    assert row is not None
    return row


def _seed(tmp_path: Path, rows: list[ObservationRow]) -> str:
    path = str(tmp_path / "replay.db")
    db = Database(path)
    db.open()
    db.insert_observations(rows)
    db.close()
    return path


def _client(
    *,
    db_path: str | None,
    persist: bool,
    replay_max_records: int = 20000,
    replay_max_window_h: float = 168.0,
) -> TestClient:
    settings = Settings(
        demo_source=False,
        persist=persist,
        db_path=db_path if db_path is not None else "unused.db",
        replay_max_records=replay_max_records,
        replay_max_window_h=replay_max_window_h,
    )
    return TestClient(create_app(settings=settings))  # no `with` → lifespan not run


def _window() -> dict[str, str]:
    return {"start": T0.isoformat(), "end": (T0 + timedelta(hours=1)).isoformat()}


# -- 503 / 400 guards ---------------------------------------------------------


def test_503_when_persistence_disabled(tmp_path: Path) -> None:
    client = _client(db_path=None, persist=False)
    assert client.post("/api/v2/replay/sessions", json=_window()).status_code == 503
    assert client.get("/api/v2/replay/sessions/whatever").status_code == 503
    assert client.delete("/api/v2/replay/sessions/whatever").status_code == 503


def test_400_on_unparseable_timestamp(tmp_path: Path) -> None:
    path = _seed(tmp_path, [_obs(record_id="a", observed_at=T0)])
    client = _client(db_path=path, persist=True)
    resp = client.post(
        "/api/v2/replay/sessions", json={"start": "not-a-time", "end": T0.isoformat()}
    )
    assert resp.status_code == 400


def test_400_when_end_not_after_start(tmp_path: Path) -> None:
    path = _seed(tmp_path, [_obs(record_id="a", observed_at=T0)])
    client = _client(db_path=path, persist=True)
    same = T0.isoformat()
    assert (
        client.post("/api/v2/replay/sessions", json={"start": same, "end": same}).status_code == 400
    )
    earlier = (T0 - timedelta(hours=1)).isoformat()
    assert (
        client.post("/api/v2/replay/sessions", json={"start": same, "end": earlier}).status_code
        == 400
    )


def test_422_on_empty_timestamp(tmp_path: Path) -> None:
    # min_length=1 on the body fields → schema rejects an empty string at the edge.
    path = _seed(tmp_path, [_obs(record_id="a", observed_at=T0)])
    client = _client(db_path=path, persist=True)
    resp = client.post("/api/v2/replay/sessions", json={"start": "", "end": ""})
    assert resp.status_code == 422


def test_400_when_window_exceeds_cap(tmp_path: Path) -> None:
    path = _seed(tmp_path, [_obs(record_id="a", observed_at=T0)])
    client = _client(db_path=path, persist=True, replay_max_window_h=1.0)
    resp = client.post(
        "/api/v2/replay/sessions",
        json={"start": T0.isoformat(), "end": (T0 + timedelta(hours=2)).isoformat()},
    )
    assert resp.status_code == 400


# -- reconstruction + bounds --------------------------------------------------


def test_post_returns_reconstructed_records_ascending(tmp_path: Path) -> None:
    rows = [_obs(record_id=f"t{s}", observed_at=T0 + timedelta(seconds=s)) for s in (30, 0, 20, 10)]
    path = _seed(tmp_path, rows)
    client = _client(db_path=path, persist=True)
    body = client.post("/api/v2/replay/sessions", json=_window()).json()
    assert body["count"] == 4
    assert body["truncated"] is False
    observed = [r["observed_at"] for r in body["records"]]
    assert observed == sorted(observed)  # ascending by observed_at
    # Records are full reconstructed wire records (not the lightweight history points).
    assert body["records"][0]["kind"] == "track"
    assert body["records"][0]["geometry"]["type"] == "Point"


def test_half_open_window_excludes_end(tmp_path: Path) -> None:
    rows = [_obs(record_id=f"t{s}", observed_at=T0 + timedelta(seconds=s)) for s in range(5)]
    path = _seed(tmp_path, rows)
    client = _client(db_path=path, persist=True)
    start = (T0 + timedelta(seconds=1)).isoformat()
    end = (T0 + timedelta(seconds=4)).isoformat()
    body = client.post("/api/v2/replay/sessions", json={"start": start, "end": end}).json()
    assert body["count"] == 3  # T0+1,+2,+3 (T0+4 == end excluded)


def test_max_records_clamp_sets_truncated(tmp_path: Path) -> None:
    rows = [_obs(record_id=f"t{s}", observed_at=T0 + timedelta(seconds=s)) for s in range(10)]
    path = _seed(tmp_path, rows)
    client = _client(db_path=path, persist=True, replay_max_records=3)
    body = client.post("/api/v2/replay/sessions", json=_window()).json()
    assert body["count"] == 3
    assert body["truncated"] is True
    # The EARLIEST max_records are returned (ascending), per the bounded-window
    # contract. The reconstructed wire form serializes UTC as a Z suffix, so compare
    # parsed instants rather than raw strings.
    observed = [datetime.fromisoformat(r["observed_at"]) for r in body["records"]]
    assert observed == [T0 + timedelta(seconds=s) for s in (0, 1, 2)]


def test_request_max_records_lowers_cap_but_is_clamped(tmp_path: Path) -> None:
    rows = [_obs(record_id=f"t{s}", observed_at=T0 + timedelta(seconds=s)) for s in range(10)]
    path = _seed(tmp_path, rows)
    client = _client(db_path=path, persist=True, replay_max_records=5)
    # Request asks for 2 (below the cap) → honored.
    body = client.post("/api/v2/replay/sessions", json={**_window(), "max_records": 2}).json()
    assert body["count"] == 2
    assert body["truncated"] is True
    # Request asks for 1000 (above the cap) → clamped to 5.
    body = client.post("/api/v2/replay/sessions", json={**_window(), "max_records": 1000}).json()
    assert body["count"] == 5
    assert body["truncated"] is True


def test_exact_count_is_not_truncated(tmp_path: Path) -> None:
    rows = [_obs(record_id=f"t{s}", observed_at=T0 + timedelta(seconds=s)) for s in range(3)]
    path = _seed(tmp_path, rows)
    client = _client(db_path=path, persist=True, replay_max_records=3)
    body = client.post("/api/v2/replay/sessions", json=_window()).json()
    assert body["count"] == 3
    assert body["truncated"] is False  # window holds exactly the cap, not more


def test_source_filter(tmp_path: Path) -> None:
    rows = [
        _obs(record_id="a", observed_at=T0 + timedelta(seconds=1), source="local_adsb"),
        _obs(record_id="b", observed_at=T0 + timedelta(seconds=2), source="network_adsb"),
        _obs(record_id="c", observed_at=T0 + timedelta(seconds=3), source="local_adsb"),
    ]
    path = _seed(tmp_path, rows)
    client = _client(db_path=path, persist=True)
    body = client.post(
        "/api/v2/replay/sessions", json={**_window(), "sources": ["local_adsb"]}
    ).json()
    assert body["count"] == 2
    assert {r["source"] for r in body["records"]} == {"local_adsb"}
    assert body["sources"] == ["local_adsb"]


def test_empty_window_is_clean(tmp_path: Path) -> None:
    path = _seed(tmp_path, [_obs(record_id="a", observed_at=T0)])
    client = _client(db_path=path, persist=True)
    # A window before any data → empty buffer, not an error.
    future = {
        "start": (T0 + timedelta(days=1)).isoformat(),
        "end": (T0 + timedelta(days=1, hours=1)).isoformat(),
    }
    body = client.post("/api/v2/replay/sessions", json=future).json()
    assert body["count"] == 0
    assert body["records"] == []


def test_missing_store_degrades_to_empty(tmp_path: Path) -> None:
    # Persistence on but nothing written yet (no DB file) → empty window, never a 500.
    client = _client(db_path=str(tmp_path / "never.db"), persist=True)
    resp = client.post("/api/v2/replay/sessions", json=_window())
    assert resp.status_code == 200
    assert resp.json()["count"] == 0


# -- session metadata lifecycle ----------------------------------------------


def test_get_session_returns_metadata_not_records(tmp_path: Path) -> None:
    path = _seed(tmp_path, [_obs(record_id="a", observed_at=T0)])
    client = _client(db_path=path, persist=True)
    sid = client.post("/api/v2/replay/sessions", json=_window()).json()["session_id"]
    meta = client.get(f"/api/v2/replay/sessions/{sid}")
    assert meta.status_code == 200
    body = meta.json()
    assert body["session_id"] == sid
    assert "records" not in body  # metadata only
    assert set(body) == {
        "session_id",
        "start",
        "end",
        "sources",
        "count",
        "truncated",
        "created_at",
    }


def test_get_unknown_session_404(tmp_path: Path) -> None:
    path = _seed(tmp_path, [_obs(record_id="a", observed_at=T0)])
    client = _client(db_path=path, persist=True)
    assert client.get("/api/v2/replay/sessions/nope").status_code == 404


def test_delete_session_204_then_404(tmp_path: Path) -> None:
    path = _seed(tmp_path, [_obs(record_id="a", observed_at=T0)])
    client = _client(db_path=path, persist=True)
    sid = client.post("/api/v2/replay/sessions", json=_window()).json()["session_id"]
    assert client.delete(f"/api/v2/replay/sessions/{sid}").status_code == 204
    assert client.get(f"/api/v2/replay/sessions/{sid}").status_code == 404  # gone
    assert client.delete(f"/api/v2/replay/sessions/{sid}").status_code == 404  # already gone


def test_registry_evicts_oldest_beyond_cap(tmp_path: Path) -> None:
    path = _seed(tmp_path, [_obs(record_id="a", observed_at=T0)])
    client = _client(db_path=path, persist=True)
    # Create one past the default cap; the very first session must have been evicted.
    sids = [
        client.post("/api/v2/replay/sessions", json=_window()).json()["session_id"]
        for _ in range(DEFAULT_MAX_SESSIONS + 1)
    ]
    assert client.get(f"/api/v2/replay/sessions/{sids[0]}").status_code == 404  # oldest evicted
    assert client.get(f"/api/v2/replay/sessions/{sids[-1]}").status_code == 200  # newest kept

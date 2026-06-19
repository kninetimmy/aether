"""Unit tests for the track-history read query (M4.3, PRD §21.3/§11.15).

Exercise :func:`read_track_history` directly against a populated store: identity
matching (fused via ``correlation_key``; unfused via ``record_id``), the half-open
time window, oldest-first ordering, the keep-newest cap, and the missing-store
behavior the API relies on to degrade to an empty history (PRD §5/§37).
"""

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from aether.persist.database import Database, ObservationRow, read_track_history

T0 = datetime(2026, 6, 19, 12, 0, 0, tzinfo=UTC)


def _row(
    *,
    record_id: str,
    correlation_key: str | None,
    observed_at: datetime,
    source: str = "local_adsb",
    lon: float = -95.0,
    lat: float = 40.0,
    alt_m: float | None = 3000.0,
) -> ObservationRow:
    iso = observed_at.isoformat()
    return ObservationRow(
        record_id=record_id,
        correlation_key=correlation_key,
        kind="track",
        track_type="aircraft",
        source=source,
        lon=lon,
        lat=lat,
        alt_m=alt_m,
        observed_at=iso,
        received_at=iso,
        persisted_at=iso,
        payload="{}",
    )


def _store(tmp_path: Path, rows: list[ObservationRow]) -> str:
    """Create a migrated store at a temp path, insert ``rows``, return its path."""
    path = str(tmp_path / "history.db")
    db = Database(path)
    db.open()
    db.insert_observations(rows)
    db.close()
    return path


def test_returns_observations_oldest_first(tmp_path: Path) -> None:
    # Insert out of order; the read must come back ascending by observed_at.
    rows = [
        _row(
            record_id="r",
            correlation_key="aircraft:icao:abc",
            observed_at=T0 + timedelta(seconds=s),
        )
        for s in (30, 0, 10, 20)
    ]
    path = _store(tmp_path, rows)
    out = read_track_history(path, "aircraft:icao:abc", limit=100)
    observed = [r.observed_at for r in out]
    assert observed == sorted(observed)
    assert len(out) == 4


def test_filters_by_fused_identity(tmp_path: Path) -> None:
    rows = [
        _row(record_id="a", correlation_key="aircraft:icao:abc", observed_at=T0),
        _row(record_id="b", correlation_key="aircraft:icao:xyz", observed_at=T0),
        _row(
            record_id="c",
            correlation_key="aircraft:icao:abc",
            observed_at=T0 + timedelta(seconds=5),
        ),
    ]
    path = _store(tmp_path, rows)
    out = read_track_history(path, "aircraft:icao:abc", limit=100)
    assert {r.correlation_key for r in out} == {"aircraft:icao:abc"}
    assert len(out) == 2


def test_unfused_track_matched_by_record_id(tmp_path: Path) -> None:
    # A None-correlation track is keyed by its record_id in live state, so the same
    # id must retrieve its history (the COALESCE identity the downsampler groups on).
    rows = [
        _row(record_id="loner-1", correlation_key=None, observed_at=T0),
        _row(record_id="other", correlation_key=None, observed_at=T0),
    ]
    path = _store(tmp_path, rows)
    out = read_track_history(path, "loner-1", limit=100)
    assert len(out) == 1
    assert out[0].record_id == "loner-1"


def test_does_not_match_record_id_when_correlation_key_present(tmp_path: Path) -> None:
    # A fused row must not be retrieved by its per-source record_id — only by the
    # fused correlation key the UI uses (no accidental cross-identity bleed).
    path = _store(
        tmp_path,
        [_row(record_id="local_adsb:abc", correlation_key="aircraft:icao:abc", observed_at=T0)],
    )
    assert read_track_history(path, "local_adsb:abc", limit=100) == []
    assert len(read_track_history(path, "aircraft:icao:abc", limit=100)) == 1


def test_half_open_time_window(tmp_path: Path) -> None:
    rows = [
        _row(record_id="r", correlation_key="k", observed_at=T0 + timedelta(seconds=s))
        for s in range(5)  # T0+0 .. T0+4
    ]
    path = _store(tmp_path, rows)
    start = (T0 + timedelta(seconds=1)).isoformat()
    end = (T0 + timedelta(seconds=4)).isoformat()
    out = read_track_history(path, "k", start_iso=start, end_iso=end, limit=100)
    secs = [r.observed_at for r in out]
    # [start, end): includes T0+1,+2,+3; excludes T0+0 (before) and T0+4 (== end).
    assert secs == [(T0 + timedelta(seconds=s)).isoformat() for s in (1, 2, 3)]


def test_limit_keeps_newest_and_returns_ascending(tmp_path: Path) -> None:
    rows = [
        _row(record_id="r", correlation_key="k", observed_at=T0 + timedelta(seconds=s))
        for s in range(10)  # T0+0 .. T0+9
    ]
    path = _store(tmp_path, rows)
    out = read_track_history(path, "k", limit=3)
    # The cap keeps the three most recent, returned oldest-first.
    assert [r.observed_at for r in out] == [
        (T0 + timedelta(seconds=s)).isoformat() for s in (7, 8, 9)
    ]


def test_unknown_identity_is_empty(tmp_path: Path) -> None:
    path = _store(tmp_path, [_row(record_id="r", correlation_key="k", observed_at=T0)])
    assert read_track_history(path, "no-such-track", limit=100) == []


def test_missing_store_raises_operational_error(tmp_path: Path) -> None:
    # The API relies on this to degrade to an empty history when persistence is on
    # but nothing has been written yet (no DB file). A read-only open cannot create it.
    missing = str(tmp_path / "never-created.db")
    with pytest.raises(sqlite3.OperationalError):
        read_track_history(missing, "k", limit=100)

"""Unit tests for the replay window read query (M4.8, PRD §19.6/§21.6).

Exercise :func:`read_observations_window` directly: the half-open time window across
*all* identities (not scoped to one track, unlike ``read_track_history``), the
optional source filter, ascending order, the cap, and the missing-store behavior the
API relies on to degrade to an empty window (PRD §5/§37).
"""

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from aether.persist.database import Database, ObservationRow, read_observations_window

T0 = datetime(2026, 6, 19, 12, 0, 0, tzinfo=UTC)


def _row(*, record_id: str, observed_at: datetime, source: str = "local_adsb") -> ObservationRow:
    iso = observed_at.isoformat()
    return ObservationRow(
        record_id=record_id,
        correlation_key=f"aircraft:icao:{record_id}",
        kind="track",
        track_type="aircraft",
        source=source,
        lon=-95.0,
        lat=40.0,
        alt_m=3000.0,
        observed_at=iso,
        received_at=iso,
        persisted_at=iso,
        payload="{}",
    )


def _store(tmp_path: Path, rows: list[ObservationRow]) -> str:
    path = str(tmp_path / "replay.db")
    db = Database(path)
    db.open()
    db.insert_observations(rows)
    db.close()
    return path


def test_returns_all_identities_in_window_ascending(tmp_path: Path) -> None:
    rows = [_row(record_id=f"t{s}", observed_at=T0 + timedelta(seconds=s)) for s in (30, 0, 20, 10)]
    path = _store(tmp_path, rows)
    out = read_observations_window(
        path,
        start_iso=T0.isoformat(),
        end_iso=(T0 + timedelta(minutes=1)).isoformat(),
        limit=100,
    )
    observed = [r.observed_at for r in out]
    assert observed == sorted(observed)
    assert len(out) == 4  # every identity in the window, not one track


def test_half_open_window(tmp_path: Path) -> None:
    rows = [_row(record_id=f"t{s}", observed_at=T0 + timedelta(seconds=s)) for s in range(5)]
    path = _store(tmp_path, rows)
    start = (T0 + timedelta(seconds=1)).isoformat()
    end = (T0 + timedelta(seconds=4)).isoformat()
    out = read_observations_window(path, start_iso=start, end_iso=end, limit=100)
    secs = [r.observed_at for r in out]
    assert secs == [(T0 + timedelta(seconds=s)).isoformat() for s in (1, 2, 3)]


def test_source_filter(tmp_path: Path) -> None:
    rows = [
        _row(record_id="a", observed_at=T0 + timedelta(seconds=1), source="local_adsb"),
        _row(record_id="b", observed_at=T0 + timedelta(seconds=2), source="network_adsb"),
        _row(record_id="c", observed_at=T0 + timedelta(seconds=3), source="ais"),
    ]
    path = _store(tmp_path, rows)
    out = read_observations_window(
        path,
        start_iso=T0.isoformat(),
        end_iso=(T0 + timedelta(minutes=1)).isoformat(),
        sources=["local_adsb", "ais"],
        limit=100,
    )
    assert {r.source for r in out} == {"local_adsb", "ais"}
    assert len(out) == 2


def test_empty_sources_list_is_no_filter(tmp_path: Path) -> None:
    rows = [
        _row(record_id="a", observed_at=T0, source="local_adsb"),
        _row(record_id="b", observed_at=T0 + timedelta(seconds=1), source="network_adsb"),
    ]
    path = _store(tmp_path, rows)
    out = read_observations_window(
        path,
        start_iso=T0.isoformat(),
        end_iso=(T0 + timedelta(minutes=1)).isoformat(),
        sources=[],  # falsy → treated as "no filter"
        limit=100,
    )
    assert len(out) == 2


def test_limit_caps_keeping_earliest(tmp_path: Path) -> None:
    rows = [_row(record_id=f"t{s}", observed_at=T0 + timedelta(seconds=s)) for s in range(10)]
    path = _store(tmp_path, rows)
    out = read_observations_window(
        path,
        start_iso=T0.isoformat(),
        end_iso=(T0 + timedelta(minutes=1)).isoformat(),
        limit=3,
    )
    # ORDER BY observed_at ASC LIMIT 3 keeps the EARLIEST three.
    assert [r.observed_at for r in out] == [
        (T0 + timedelta(seconds=s)).isoformat() for s in (0, 1, 2)
    ]


def test_excludes_non_track_rows(tmp_path: Path) -> None:
    """The ``kind='track'`` guard keeps the replay buffer track-only by construction.

    The writer persists only tracks today, but the read enforces it independently so a
    future migration that stores other kinds in ``observations`` can't leak a non-track
    row into a replay buffer (which the player would reconstruct and the UI would treat
    as a track).
    """
    track = _row(record_id="t", observed_at=T0 + timedelta(seconds=1))
    non_track = ObservationRow(
        record_id="evt",
        correlation_key=None,
        kind="event",  # not a track — must be excluded
        track_type=None,
        source="local_adsb",
        lon=None,
        lat=None,
        alt_m=None,
        observed_at=(T0 + timedelta(seconds=2)).isoformat(),
        received_at=(T0 + timedelta(seconds=2)).isoformat(),
        persisted_at=(T0 + timedelta(seconds=2)).isoformat(),
        payload="{}",
    )
    path = _store(tmp_path, [track, non_track])
    out = read_observations_window(
        path,
        start_iso=T0.isoformat(),
        end_iso=(T0 + timedelta(minutes=1)).isoformat(),
        limit=100,
    )
    assert [r.record_id for r in out] == ["t"]
    assert all(r.kind == "track" for r in out)


def test_missing_store_raises_operational_error(tmp_path: Path) -> None:
    # The API relies on this to degrade to an empty window when persistence is on but
    # nothing has been written yet. A read-only open cannot create the file.
    missing = str(tmp_path / "never-created.db")
    with pytest.raises(sqlite3.OperationalError):
        read_observations_window(
            missing,
            start_iso=T0.isoformat(),
            end_iso=(T0 + timedelta(minutes=1)).isoformat(),
            limit=100,
        )

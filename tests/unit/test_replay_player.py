"""Unit tests for pure replay record reconstruction (M4.8, PRD §19.6).

Exercise :func:`reconstruct_records` directly: a real persisted ``TrackRecord``
payload round-trips to its ``dump_record`` wire form, a malformed payload row is
skipped (never raised), and input order is preserved. No FastAPI, no store, no
hub/engine — the function is pure, which is what keeps the replay path decoupled
from the live alert/notification path (the M4 exit invariant).
"""

from datetime import UTC, datetime

from aether.persist.database import ObservationRow
from aether.persist.writer import to_observation_row
from aether.replay.player import reconstruct_records
from aether.schema.geometry import Point
from aether.schema.provenance import Provenance
from aether.schema.records import TrackRecord
from aether.schema.validation import dump_record

T0 = datetime(2026, 6, 19, 12, 0, 0, tzinfo=UTC)


def _track(record_id: str = "local_adsb:abc") -> TrackRecord:
    return TrackRecord(
        id=record_id,
        source="local_adsb",
        observed_at=T0,
        received_at=T0,
        published_at=T0,
        correlation_key="aircraft:icao:abc",
        track_type="aircraft",
        geometry=Point(coordinates=[-95.0, 40.0, 3000.0]),
        altitude_m=3000.0,
        locally_received=True,
        provenance=[Provenance(source="local_adsb", observed_at=T0, received_at=T0, local_rf=True)],
    )


def _row_for(track: TrackRecord) -> ObservationRow:
    row = to_observation_row(track, now=T0)
    assert row is not None  # a TrackRecord always maps to a row
    return row


def _bad_row() -> ObservationRow:
    iso = T0.isoformat()
    return ObservationRow(
        record_id="corrupt",
        correlation_key=None,
        kind="track",
        track_type="aircraft",
        source="local_adsb",
        lon=None,
        lat=None,
        alt_m=None,
        observed_at=iso,
        received_at=iso,
        persisted_at=iso,
        payload="{not valid json",  # cannot be parsed
    )


def test_reconstructs_lossless_dump_form() -> None:
    track = _track()
    out = reconstruct_records([_row_for(track)])
    assert out == [dump_record(track)]  # byte-for-byte the live wire shape


def test_malformed_payload_row_is_skipped_not_raised() -> None:
    track = _track()
    # A bad row between two good ones: only the good ones survive, no exception.
    out = reconstruct_records([_row_for(track), _bad_row(), _row_for(_track("local_adsb:def"))])
    assert len(out) == 2
    assert [r["id"] for r in out] == ["local_adsb:abc", "local_adsb:def"]


def test_preserves_input_order() -> None:
    ids = ["a", "b", "c", "d"]
    rows = [_row_for(_track(f"local_adsb:{i}")) for i in ids]
    out = reconstruct_records(rows)
    assert [r["id"] for r in out] == [f"local_adsb:{i}" for i in ids]


def test_empty_input_is_empty() -> None:
    assert reconstruct_records([]) == []

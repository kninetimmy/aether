"""Subscriber payload handling (PRD §37 failure isolation) — no broker needed.

``apply_payload`` is the per-message core of the bus subscriber: parse one wire
payload and feed the record to the sink, dropping (never raising on) anything
malformed so one bad message can't kill the ingest loop.
"""

from datetime import UTC, datetime

from aether.bus.client import apply_payload
from aether.schema.geometry import Point
from aether.schema.records import Record, TrackRecord
from aether.schema.validation import dump_record_json

_T = datetime(2026, 1, 1, tzinfo=UTC)


def _track() -> TrackRecord:
    return TrackRecord(
        id="aircraft:abc",
        source="demo",
        observed_at=_T,
        received_at=_T,
        published_at=_T,
        track_type="aircraft",
        geometry=Point(coordinates=[-95.0, 40.0]),
        locally_received=True,
    )


def test_valid_payload_is_parsed_and_handed_off() -> None:
    received: list[Record] = []
    ok = apply_payload(dump_record_json(_track()), received.append)
    assert ok is True
    assert len(received) == 1
    assert received[0].id == "aircraft:abc"


def test_malformed_json_is_dropped_not_raised() -> None:
    received: list[Record] = []
    assert apply_payload(b"{not json", received.append) is False
    assert received == []


def test_unknown_kind_is_dropped() -> None:
    received: list[Record] = []
    assert apply_payload(b'{"kind": "bogus", "id": "x"}', received.append) is False
    assert received == []


def test_non_bytes_payload_ignored() -> None:
    received: list[Record] = []
    assert apply_payload(None, received.append) is False
    assert received == []


def test_handler_exception_is_isolated_not_raised() -> None:
    # A bug while applying one record to state must not propagate out and kill the
    # subscriber loop — the whole ingest path for every source depends on it (§37).
    def boom(_record: Record) -> None:
        raise RuntimeError("state apply blew up")

    assert apply_payload(dump_record_json(_track()), boom) is False

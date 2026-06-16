"""Topic routing and delivery policy (PRD §23) — no broker needed."""

from datetime import UTC, datetime

from aether.bus.topics import (
    RECORDS_PREFIX,
    STATUS_PREFIX,
    SUBSCRIBE_FILTERS,
    record_qos,
    record_retain,
    record_topic,
)
from aether.schema.geometry import Point
from aether.schema.records import EventRecord, SourceStatusRecord, TrackRecord

_T = datetime(2026, 1, 1, tzinfo=UTC)


def _track() -> TrackRecord:
    return TrackRecord(
        id="aircraft:abc",
        source="local_adsb",
        observed_at=_T,
        received_at=_T,
        published_at=_T,
        track_type="aircraft",
        geometry=Point(coordinates=[-95.0, 40.0]),
        locally_received=True,
    )


def _status() -> SourceStatusRecord:
    return SourceStatusRecord(
        id="source_status:local_adsb",
        source="local_adsb",
        observed_at=_T,
        received_at=_T,
        published_at=_T,
        status="connected",
    )


def _event() -> EventRecord:
    return EventRecord(
        id="event:1",
        source="local_adsb",
        observed_at=_T,
        received_at=_T,
        published_at=_T,
        event_type="emergency",
        summary="squawk 7700",
    )


def test_records_go_to_per_source_records_topic() -> None:
    assert record_topic(_track()) == f"{RECORDS_PREFIX}/local_adsb"
    assert record_topic(_event()) == f"{RECORDS_PREFIX}/local_adsb"


def test_status_goes_to_per_source_status_topic() -> None:
    assert record_topic(_status()) == f"{STATUS_PREFIX}/local_adsb"


def test_qos_zero_for_high_rate_tracks_one_otherwise() -> None:
    assert record_qos(_track()) == 0
    assert record_qos(_event()) == 1
    assert record_qos(_status()) == 1


def test_only_source_status_is_retained() -> None:
    assert record_retain(_status()) is True
    assert record_retain(_track()) is False
    assert record_retain(_event()) is False


def test_subscribe_filters_cover_records_and_status_trees() -> None:
    assert f"{RECORDS_PREFIX}/#" in SUBSCRIBE_FILTERS
    assert f"{STATUS_PREFIX}/#" in SUBSCRIBE_FILTERS

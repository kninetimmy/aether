"""Unit tests for schema v2: the discriminated record union and its guards."""

from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from aether.schema import (
    AlertRecord,
    EventRecord,
    GeoFeatureRecord,
    Provenance,
    RecordTooLargeError,
    SourceStatusRecord,
    TrackRecord,
    dump_record,
    dump_record_json,
    parse_record,
    parse_records,
)

T0 = datetime(2026, 6, 15, 12, 0, 0, tzinfo=UTC)


def _base_fields(**overrides: object) -> dict[str, object]:
    fields: dict[str, object] = {
        "id": "test:1",
        "source": "demo",
        "observed_at": T0,
        "received_at": T0,
        "published_at": T0,
    }
    fields.update(overrides)
    return fields


def _track() -> TrackRecord:
    return TrackRecord(
        **_base_fields(id="aircraft:abc123"),
        track_type="aircraft",
        label="N123AB",
        geometry={"type": "Point", "coordinates": [-95.0, 40.0, 3000.0]},
        altitude_m=3000.0,
        speed_mps=120.0,
        heading_deg=270.0,
        locally_received=True,
        provenance=[
            Provenance(source="local_adsb", observed_at=T0, received_at=T0, local_rf=True),
        ],
    )


def test_track_round_trips_through_json() -> None:
    track = _track()
    reparsed = parse_record(dump_record_json(track))
    assert isinstance(reparsed, TrackRecord)
    assert reparsed == track
    assert reparsed.kind == "track"
    assert reparsed.locally_received is True


def test_dump_record_serializes_datetimes_as_iso_utc() -> None:
    dumped = dump_record(_track())
    assert dumped["observed_at"] in ("2026-06-15T12:00:00Z", "2026-06-15T12:00:00+00:00")
    assert dumped["schema_version"] == 2


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (
            {
                **{
                    "kind": "feature",
                    "feature_type": "tfr",
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [
                            [[-95.0, 40.0], [-94.0, 40.0], [-94.0, 41.0], [-95.0, 40.0]]
                        ],
                    },
                },
                **_base_fields(id="tfr:1"),
            },
            GeoFeatureRecord,
        ),
        (
            {
                **{"kind": "event", "event_type": "squawk_7700", "summary": "emergency"},
                **_base_fields(),
            },
            EventRecord,
        ),
        (
            {
                **{
                    "kind": "alert",
                    "rule_id": "r1",
                    "state": "open",
                    "severity": "high",
                    "title": "t",
                    "summary": "s",
                    "triggered_at": T0,
                },
                **_base_fields(),
            },
            AlertRecord,
        ),
        (
            {**{"kind": "source_status", "status": "connected"}, **_base_fields()},
            SourceStatusRecord,
        ),
    ],
)
def test_parse_record_discriminates_on_kind(payload: dict[str, object], expected: type) -> None:
    assert isinstance(parse_record(payload), expected)


def test_parse_records_validates_a_list() -> None:
    records = parse_records([dump_record(_track()), dump_record(_track())])
    assert len(records) == 2
    assert all(isinstance(r, TrackRecord) for r in records)


def test_unknown_kind_is_rejected() -> None:
    with pytest.raises(ValidationError):
        parse_record({**{"kind": "nonsense"}, **_base_fields()})


def test_extra_top_level_field_is_forbidden() -> None:
    payload = {
        **{"kind": "source_status", "status": "connected", "rogue": 1},
        **_base_fields(),
    }
    with pytest.raises(ValidationError):
        parse_record(payload)


def test_naive_datetime_is_rejected() -> None:
    payload = {
        **{"kind": "source_status", "status": "connected"},
        **_base_fields(observed_at="2026-06-15T12:00:00"),
    }
    with pytest.raises(ValidationError):
        parse_record(payload)


def test_aware_datetime_is_normalized_to_utc() -> None:
    plus_five = datetime(2026, 6, 15, 17, 0, 0, tzinfo=timezone(timedelta(hours=5)))
    record = parse_record(
        {**{"kind": "source_status", "status": "connected"}, **_base_fields(observed_at=plus_five)}
    )
    assert record.observed_at == T0
    assert record.observed_at.utcoffset() == timedelta(0)


def test_oversized_payload_is_rejected() -> None:
    blob = dump_record_json(_track())
    with pytest.raises(RecordTooLargeError):
        parse_record(blob, max_bytes=8)


def test_geometry_out_of_range_is_rejected() -> None:
    with pytest.raises(ValidationError):
        TrackRecord(
            **_base_fields(),
            track_type="aircraft",
            geometry={"type": "Point", "coordinates": [-95.0, 200.0]},
            locally_received=False,
        )


def test_geometry_position_requires_two_coordinates() -> None:
    with pytest.raises(ValidationError):
        TrackRecord(
            **_base_fields(),
            track_type="aircraft",
            geometry={"type": "Point", "coordinates": [-95.0]},
            locally_received=False,
        )

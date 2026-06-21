"""Unit tests for the FAA TFR adapter (PRD §11.13, §18.10, M6.1).

Covers the pure parsing layer (decimal-degrees-with-hemisphere coordinates, local-zone
→ UTC time conversion, ``<XNOTAM-Update>`` → Polygon/MultiPolygon normalization, the
malformed-geometry → textual-event fallback) and the runtime around it — AOI filtering,
revision dedupe across polls, the detail-fetch budget, degraded-on-failure isolation,
and provider selection. No broker, no live call: the in-process fake feeder stands in.
"""

import asyncio
import dataclasses
from datetime import UTC, datetime

from aether.adapters.faa_tfr import (
    SOURCE,
    build_provider,
    notam_to_path,
    parse_coord,
    parse_detail,
    parse_local_dt,
    tfr_in_aoi,
    tfr_records,
)
from aether.adapters.faa_tfr_fake_feeder import FakeFaaTfrProvider
from aether.config import Settings
from aether.schema.geometry import MultiPolygon, Point, Polygon
from aether.schema.records import EventRecord, GeoFeatureRecord, SourceStatusRecord

NOW = datetime(2026, 6, 21, 12, 0, 0, tzinfo=UTC)
CENTER_LAT, CENTER_LON = 38.5, -122.5


# --- Pure coordinate / time parsing --------------------------------------------


def test_parse_coord_decimal_hemisphere() -> None:
    assert parse_coord("30.32166667N", is_lat=True) == 30.32166667
    assert parse_coord("081.435W", is_lat=False) == -81.435
    assert parse_coord("10.0S", is_lat=True) == -10.0
    assert parse_coord("005.0E", is_lat=False) == 5.0


def test_parse_coord_rejects_bad_input() -> None:
    assert parse_coord("200.0N", is_lat=True) is None  # latitude out of range
    assert parse_coord("30.0W", is_lat=True) is None  # E/W hemisphere on a latitude
    assert parse_coord("081.4N", is_lat=False) is None  # N/S hemisphere on a longitude
    assert parse_coord("garbage", is_lat=True) is None
    assert parse_coord(None, is_lat=True) is None


def test_parse_local_dt_converts_zone_to_utc() -> None:
    # 12:30 EDT (UTC-4) → 16:30 UTC.
    assert parse_local_dt("2026-06-22T12:30:00", "EDT") == datetime(2026, 6, 22, 16, 30, tzinfo=UTC)
    # UTC passes through.
    assert parse_local_dt("2026-06-22T12:30:00", "UTC") == datetime(2026, 6, 22, 12, 30, tzinfo=UTC)


def test_parse_local_dt_unknown_zone_is_none() -> None:
    # An unmapped zone yields None rather than a UTC instant we cannot justify.
    assert parse_local_dt("2026-06-22T12:30:00", "XYZ") is None
    assert parse_local_dt("", "UTC") is None
    assert parse_local_dt("not-a-date", "UTC") is None


def test_notam_to_path() -> None:
    assert notam_to_path("6/9513") == "6_9513"


# --- Detail XML → record -------------------------------------------------------


def _detail(provider: FakeFaaTfrProvider, notam_id: str) -> bytes:
    return asyncio.run(provider.fetch_detail(notam_id))


def _fake() -> FakeFaaTfrProvider:
    return FakeFaaTfrProvider(center_lat=CENTER_LAT, center_lon=CENTER_LON, now_fn=lambda: NOW)


def test_parse_detail_single_area_polygon() -> None:
    rec = parse_detail(
        _detail(_fake(), "6/0001"), notam_id="6/0001", list_type="SECURITY", received_at=NOW
    )
    assert isinstance(rec, GeoFeatureRecord)
    assert rec.id == "tfr:faa:6/0001"
    assert rec.correlation_key == "tfr:faa:6/0001"
    assert rec.feature_type == "tfr"
    assert rec.source == SOURCE
    assert isinstance(rec.geometry, Polygon)
    ring = rec.geometry.coordinates[0]
    assert ring[0] == ring[-1]  # closed ring (RFC 7946)
    assert len(ring) == 5  # 4 corners + repeated first
    # Time-bounded (AIRSPACE-FR-002), converted from the source zone.
    assert rec.valid_from is not None and rec.valid_until is not None
    assert rec.valid_until > rec.valid_from
    # Original NOTAM detail retained (AIRSPACE-FR-006) + honest caveat (AIRSPACE-FR-007).
    assert rec.attributes["notam_id"] == "6/0001"
    assert rec.attributes["list_type"] == "SECURITY"
    assert rec.attributes["regulatory_label"] == "Security (99.7)"
    assert "flight-planning" in rec.attributes["caveat"]
    assert rec.attributes["attribution"]
    assert rec.provenance[0].local_rf is False


def test_parse_detail_multi_area_multipolygon() -> None:
    rec = parse_detail(
        _detail(_fake(), "6/0002"), notam_id="6/0002", list_type="VIP", received_at=NOW
    )
    assert isinstance(rec, GeoFeatureRecord)
    assert isinstance(rec.geometry, MultiPolygon)
    assert len(rec.geometry.coordinates) == 2  # two TFR areas
    assert len(rec.attributes["areas"]) == 2
    assert rec.attributes["areas"][0]["altitude_upper"]["value"] == "2500"


def test_parse_detail_malformed_geometry_becomes_event() -> None:
    rec = parse_detail(
        _detail(_fake(), "6/0004"), notam_id="6/0004", list_type="SPORTS", received_at=NOW
    )
    # Unparseable geometry → textual event, never an invented shape (§18.10).
    assert isinstance(rec, EventRecord)
    assert rec.event_type == "tfr_geometry_unparseable"
    assert rec.subject_id == "tfr:faa:6/0004"
    assert rec.correlation_key == "tfr:faa:6/0004"
    assert rec.attributes["notam_id"] == "6/0004"


def test_parse_detail_non_active_returns_none() -> None:
    cancel = b'<XNOTAM-Update version="0.1"><Group><Cancel><Not/></Cancel></Group></XNOTAM-Update>'
    assert parse_detail(cancel, notam_id="6/9999", list_type=None, received_at=NOW) is None


# --- AOI test ------------------------------------------------------------------


def test_tfr_in_aoi_vertex_and_enclosure() -> None:
    radius_m = 500.0 * 1852.0
    near = Polygon(
        coordinates=[
            [
                [CENTER_LON, CENTER_LAT],
                [CENTER_LON + 0.1, CENTER_LAT],
                [CENTER_LON + 0.1, CENTER_LAT + 0.1],
                [CENTER_LON, CENTER_LAT],
            ]
        ]
    )
    assert tfr_in_aoi(near, center_lat=CENTER_LAT, center_lon=CENTER_LON, radius_m=radius_m)
    far = Polygon(
        coordinates=[
            [
                [CENTER_LON + 25, CENTER_LAT + 25],
                [CENTER_LON + 25.1, CENTER_LAT + 25],
                [CENTER_LON + 25.1, CENTER_LAT + 25.1],
                [CENTER_LON + 25, CENTER_LAT + 25],
            ]
        ]
    )
    assert not tfr_in_aoi(far, center_lat=CENTER_LAT, center_lon=CENTER_LON, radius_m=radius_m)
    # A large TFR whose ring *encloses* the station (no vertex near it) still counts.
    big = Polygon(
        coordinates=[
            [
                [CENTER_LON - 3, CENTER_LAT - 3],
                [CENTER_LON + 3, CENTER_LAT - 3],
                [CENTER_LON + 3, CENTER_LAT + 3],
                [CENTER_LON - 3, CENTER_LAT + 3],
                [CENTER_LON - 3, CENTER_LAT - 3],
            ]
        ]
    )
    assert tfr_in_aoi(big, center_lat=CENTER_LAT, center_lon=CENTER_LON, radius_m=1.0)


def test_tfr_in_aoi_point_geometry_is_false() -> None:
    # Defensive: a non-areal geometry yields no rings → not in AOI (never raises).
    assert not tfr_in_aoi(
        Point(coordinates=[CENTER_LON, CENTER_LAT]),
        center_lat=CENTER_LAT,
        center_lon=CENTER_LON,
        radius_m=1.0,
    )


# --- Runtime (stream / dedupe / isolation) -------------------------------------


async def _drive(agen, *, statuses_wanted: int) -> list:
    records: list = []
    seen = 0
    async for record in agen:
        records.append(record)
        if isinstance(record, SourceStatusRecord):
            seen += 1
            if seen >= statuses_wanted:
                break
    await agen.aclose()
    return records


def _features(records: list) -> list[GeoFeatureRecord]:
    return [r for r in records if isinstance(r, GeoFeatureRecord)]


def _stream(**kw):
    return tfr_records(
        _fake(),
        center_lat=CENTER_LAT,
        center_lon=CENTER_LON,
        radius_nm=500.0,
        poll_s=0.0,
        **kw,
    )


def test_first_record_is_starting_status() -> None:
    records = asyncio.run(_drive(_stream(), statuses_wanted=2))
    assert isinstance(records[0], SourceStatusRecord)
    assert records[0].status == "starting"
    assert records[0].source == SOURCE


def test_in_aoi_tfrs_emitted_far_dropped() -> None:
    records = asyncio.run(_drive(_stream(), statuses_wanted=2))
    feat_ids = {r.id for r in _features(records)}
    assert feat_ids == {"tfr:faa:6/0001", "tfr:faa:6/0002"}  # in-AOI polygon + multipolygon
    # The far TFR (6/0003) is filtered; the malformed one (6/0004) is a textual event.
    events = [r for r in records if isinstance(r, EventRecord)]
    assert [e.subject_id for e in events] == ["tfr:faa:6/0004"]


def test_connected_status_reports_counts() -> None:
    status = asyncio.run(_drive(_stream(), statuses_wanted=2))[-1]
    assert isinstance(status, SourceStatusRecord)
    assert status.status == "connected"
    assert status.attributes["listed"] == 4
    assert status.attributes["emitted_this_poll"] == 3  # 2 features + 1 event
    assert status.attributes["attribution"]


def test_revision_dedupe_across_polls() -> None:
    # Same provider, same creation_date → a second poll fetches and emits nothing new.
    records = asyncio.run(_drive(_stream(), statuses_wanted=3))  # two completed polls
    assert len(_features(records)) == 2  # both from the first poll only


def test_states_filter_limits_listing() -> None:
    # Only state "YY" (the far TFR 6/0003) survives the pre-filter; it is out of AOI, so
    # nothing is emitted — but the status shows it was the only one considered.
    records = asyncio.run(_drive(_stream(states=frozenset({"YY"})), statuses_wanted=2))
    assert _features(records) == []
    assert records[-1].attributes["considered"] == 1


def test_detail_budget_defers_remaining_fetches() -> None:
    records = asyncio.run(_drive(_stream(max_details_per_poll=1), statuses_wanted=2))
    status = records[-1]
    assert status.attributes["fetched_this_poll"] == 1
    assert status.attributes["pending"] == 3  # 4 listed − 1 fetched


def test_list_fetch_failure_degrades_without_crashing() -> None:
    class _Failing:
        name = "boom"

        async def fetch_list(self) -> list:
            raise RuntimeError("network down")

        async def fetch_detail(self, notam_id: str) -> bytes:
            raise AssertionError("should not fetch detail after a list failure")

    agen = tfr_records(
        _Failing(), center_lat=CENTER_LAT, center_lon=CENTER_LON, radius_nm=500.0, poll_s=0.0
    )
    records = asyncio.run(_drive(agen, statuses_wanted=2))
    assert _features(records) == []
    degraded = records[-1]
    assert isinstance(degraded, SourceStatusRecord)
    assert degraded.status == "degraded"
    assert degraded.error_code == "RuntimeError"


def test_build_provider_selects_fake_feeder() -> None:
    cfg = dataclasses.replace(
        Settings(),
        faa_tfr_base_url="fake",
        faa_tfr_center_lat=CENTER_LAT,
        faa_tfr_center_lon=CENTER_LON,
    )
    assert isinstance(build_provider(cfg), FakeFaaTfrProvider)

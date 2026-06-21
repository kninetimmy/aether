"""Unit tests for the NASA FIRMS active-fire adapter (PRD §11.11, M5.3).

Covers the pure CSV→record normalizer (VIIRS + MODIS column sets, confidence-class
normalization, deterministic detection id, honest no-severity labeling), the helpers
(confidence buckets, acq-time parsing, AOI bbox), and the runtime around it — AOI
filtering, confidence floor, detection-id dedupe across polls, degraded-on-failure
isolation, the missing-map-key offline gate, and provider selection. No broker, no live
call: a fake/stub provider stands in for the network.
"""

import asyncio
import dataclasses
from datetime import UTC, datetime

from aether.adapters.firms import (
    SOURCE,
    acq_to_dt,
    aoi_bbox,
    build_provider,
    confidence_class,
    firms_records,
    parse_csv,
    row_to_record,
)
from aether.adapters.firms_fake_feeder import FakeFirmsProvider
from aether.config import Settings
from aether.schema.records import GeoFeatureRecord, SourceStatusRecord

NOW = datetime(2026, 6, 18, 12, 34, 0, tzinfo=UTC)
CENTER_LAT, CENTER_LON = 10.0, 20.0

_VIIRS_HEADER = (
    "latitude,longitude,bright_ti4,scan,track,acq_date,acq_time,"
    "satellite,instrument,confidence,version,bright_ti5,frp,daynight"
)
_MODIS_HEADER = (
    "latitude,longitude,brightness,scan,track,acq_date,acq_time,"
    "satellite,instrument,confidence,version,bright_t31,frp,daynight"
)


def _viirs_row(*, lat: float = 10.0, lon: float = 20.0, conf: str = "h", frp: float = 45.0) -> str:
    return f"{lat},{lon},345.2,0.39,0.36,2026-06-18,1234,N,VIIRS,{conf},2.0NRT,300.1,{frp},D"


async def _drive(agen, *, statuses_wanted: int) -> list:
    """Collect records until ``statuses_wanted`` status records have been seen."""
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


# --- Helpers -------------------------------------------------------------------


def test_confidence_class_handles_viirs_letters_and_modis_numbers() -> None:
    assert confidence_class("h") == "high"
    assert confidence_class("nominal") == "nominal"
    assert confidence_class("l") == "low"
    assert confidence_class("85") == "high"  # MODIS numeric >= 80
    assert confidence_class("50") == "nominal"  # 30..79
    assert confidence_class("10") == "low"  # < 30
    assert confidence_class("") is None
    assert confidence_class(None) is None


def test_acq_to_dt_parses_hhmm_with_stripped_zeros() -> None:
    assert acq_to_dt("2026-06-18", "1234") == datetime(2026, 6, 18, 12, 34, tzinfo=UTC)
    assert acq_to_dt("2026-06-18", "133") == datetime(2026, 6, 18, 1, 33, tzinfo=UTC)
    assert acq_to_dt("bad", "1234") is None


def test_aoi_bbox_circumscribes_the_disk_and_clamps() -> None:
    w, s, e, n = aoi_bbox(0.0, 0.0, 60.0)  # 60 NM == 1 deg latitude
    assert s < 0.0 < n and w < 0.0 < e
    assert round(n, 3) == 1.0  # one degree north
    # Near the pole the box clamps to valid ranges rather than exploding.
    _, _, _, n2 = aoi_bbox(89.0, 0.0, 500.0)
    assert n2 == 90.0


# --- Pure normalizer -----------------------------------------------------------


def test_row_to_record_maps_viirs_fields() -> None:
    [row] = parse_csv(_VIIRS_HEADER + "\n" + _viirs_row())
    rec = row_to_record(row, received_at=NOW)
    assert rec is not None
    assert rec.feature_type == "fire_detection"
    assert rec.source == SOURCE
    assert rec.geometry.coordinates == [20.0, 10.0]  # [lon, lat]
    assert rec.attributes["confidence"] == "h"
    assert rec.attributes["confidence_class"] == "high"
    assert rec.attributes["frp_mw"] == 45.0
    assert rec.attributes["brightness_k"] == 345.2
    assert rec.attributes["daynight"] == "D"
    assert rec.attributes["instrument"] == "VIIRS"
    assert rec.attributes["attribution"]
    assert "thermal-anomaly" in rec.attributes["caveat"]
    # A detection is not a graded hazard — no severity (FIRMS-FR-005).
    assert rec.severity is None
    assert rec.observed_at == datetime(2026, 6, 18, 12, 34, tzinfo=UTC)
    assert rec.provenance[0].local_rf is False
    assert rec.provenance[0].confidence == "high"
    assert rec.id == rec.correlation_key
    assert "fire_detection" in rec.tags


def test_row_to_record_maps_modis_numeric_confidence() -> None:
    modis = "10.0,20.0,310.5,1.0,1.0,2026-06-18,1234,Terra,MODIS,72,6.1NRT,290.0,15.5,D"
    [row] = parse_csv(_MODIS_HEADER + "\n" + modis)
    rec = row_to_record(row, received_at=NOW)
    assert rec is not None
    assert rec.attributes["confidence"] == "72"
    assert rec.attributes["confidence_class"] == "nominal"  # 30..79
    assert rec.attributes["brightness_k"] == 310.5  # MODIS "brightness" column
    assert rec.provenance[0].confidence == "medium"  # nominal -> medium


def test_row_to_record_rejects_missing_coords() -> None:
    [row] = parse_csv(_VIIRS_HEADER + "\n" + ",,345.2,0.4,0.4,2026-06-18,1234,N,VIIRS,h,2,300,12,D")
    assert row_to_record(row, received_at=NOW) is None


def test_parse_csv_rejects_non_csv_body() -> None:
    # A bad map key returns a plain-text message, not CSV — must raise (fail-visibly).
    try:
        parse_csv("Invalid MAP_KEY.")
    except ValueError as exc:
        assert "Invalid MAP_KEY" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("parse_csv should reject a non-CSV body")


# --- Runtime (filtering / dedupe / isolation) ----------------------------------


def _provider() -> FakeFirmsProvider:
    return FakeFirmsProvider(center_lat=CENTER_LAT, center_lon=CENTER_LON, now_fn=lambda: NOW)


def _run_one_poll(*, min_confidence: str = "") -> list:
    agen = firms_records(
        _provider(),
        center_lat=CENTER_LAT,
        center_lon=CENTER_LON,
        radius_nm=500.0,
        min_confidence=min_confidence,
        poll_s=0.0,
    )
    return asyncio.run(_drive(agen, statuses_wanted=2))


def test_first_record_is_starting_status() -> None:
    records = _run_one_poll()
    assert isinstance(records[0], SourceStatusRecord)
    assert records[0].status == "starting"
    assert records[0].source == SOURCE


def test_aoi_filter_drops_far_detections() -> None:
    recs = _features(_run_one_poll())
    # The center + short-hop + low-conf-at-center detections are inside the 500 NM AOI;
    # the ~15° away detection is not.
    assert len(recs) == 3
    for r in recs:
        lon, lat = r.geometry.coordinates
        assert abs(lat - CENTER_LAT) < 5.0 and abs(lon - CENTER_LON) < 5.0


def test_min_confidence_floor_drops_low_detections() -> None:
    classes = {
        r.attributes["confidence_class"] for r in _features(_run_one_poll(min_confidence="nominal"))
    }
    assert "low" not in classes
    assert classes == {"high", "nominal"}


def test_connected_status_reports_attribution_and_counts() -> None:
    status = _run_one_poll()[-1]
    assert isinstance(status, SourceStatusRecord)
    assert status.status == "connected"
    assert status.attributes["attribution"] == "NASA FIRMS (LANCE/EOSDIS)"
    assert status.attributes["emitted_this_poll"] == 3
    assert status.attributes["in_aoi"] == 3


def test_detections_are_deduped_across_polls() -> None:
    agen = firms_records(
        _provider(),
        center_lat=CENTER_LAT,
        center_lon=CENTER_LON,
        radius_nm=500.0,
        poll_s=0.0,
    )
    records = asyncio.run(_drive(agen, statuses_wanted=3))  # two completed polls
    # All three in-AOI detections come from the first poll; the second re-emits none.
    assert len(_features(records)) == 3


def test_fetch_failure_degrades_without_crashing() -> None:
    class _Failing:
        name = "boom"

        async def fetch(self) -> str:
            raise RuntimeError("network down")

    agen = firms_records(
        _Failing(),
        center_lat=CENTER_LAT,
        center_lon=CENTER_LON,
        radius_nm=500.0,
        poll_s=0.0,
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
        firms_api_base="fake",
        firms_center_lat=CENTER_LAT,
        firms_center_lon=CENTER_LON,
    )
    assert isinstance(build_provider(cfg), FakeFirmsProvider)


def test_build_provider_requires_map_key_for_live_feed() -> None:
    cfg = dataclasses.replace(Settings(), firms_map_key="")  # live base, no key
    try:
        build_provider(cfg)
    except ValueError as exc:
        assert "map key" in str(exc).lower()
    else:  # pragma: no cover
        raise AssertionError("build_provider should require a map key for the live feed")

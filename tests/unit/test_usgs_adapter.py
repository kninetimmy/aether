"""Unit tests for the USGS earthquake adapter (PRD §11.12, M5.1).

Covers the pure GeoJSON→record normalizer (field mapping, 2-D geometry with depth in
attributes, earthquake-only type guard, malformed-feature rejection) and the runtime
around it — AOI filtering, magnitude floor, event-id dedupe across polls, degraded-on-
failure isolation, and provider selection. No broker, no live call: a fake/stub
provider stands in for the network.
"""

import asyncio
import dataclasses
from datetime import UTC, datetime

from aether.adapters.usgs import (
    SOURCE,
    build_provider,
    feature_to_record,
    usgs_records,
)
from aether.adapters.usgs_fake_feeder import FakeUsgsProvider
from aether.config import Settings
from aether.schema.records import GeoFeatureRecord, SourceStatusRecord

NOW = datetime(2026, 6, 18, 12, 0, 0, tzinfo=UTC)
CENTER_LAT, CENTER_LON = 10.0, 20.0


def _feature() -> dict:
    return {
        "type": "Feature",
        "id": "us7000abcd",
        "properties": {
            "mag": 4.5,
            "magType": "mb",
            "place": "10km N of Somewhere",
            "time": int(NOW.timestamp() * 1000),
            "updated": int(NOW.timestamp() * 1000),
            "status": "reviewed",
            "tsunami": 0,
            "sig": 312,
            "felt": 12,
            "alert": "green",
            "url": "https://earthquake.usgs.gov/earthquakes/eventpage/us7000abcd",
            "title": "M 4.5 - 10km N of Somewhere",
            "type": "earthquake",
        },
        "geometry": {"type": "Point", "coordinates": [20.0, 10.0, 10.0]},
    }


async def _drive(agen, *, statuses_wanted: int) -> list:
    """Collect records until ``statuses_wanted`` status records have been seen.

    Statuses delimit polls (``starting``, then one per poll), so 2 = the first
    completed poll, 3 = two polls. ``aclose`` cancels at the yield so a trailing
    poll/backoff sleep never runs.
    """
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


# --- Pure normalizer -----------------------------------------------------------


def test_feature_to_record_maps_displayable_fields() -> None:
    rec = feature_to_record(_feature(), received_at=NOW)
    assert rec is not None
    assert rec.id == "earthquake:usgs:us7000abcd"
    assert rec.correlation_key == "earthquake:usgs:us7000abcd"  # event id is the dedupe key
    assert rec.feature_type == "earthquake"
    assert rec.source == SOURCE
    # Geometry is 2-D; depth is an attribute, not a mislabeled altitude.
    assert rec.geometry.coordinates == [20.0, 10.0]
    assert rec.attributes["depth_km"] == 10.0
    assert rec.attributes["magnitude"] == 4.5
    assert rec.attributes["review_status"] == "reviewed"
    assert rec.attributes["significance"] == 312
    assert rec.attributes["felt"] == 12
    assert rec.attributes["tsunami"] is False
    assert rec.attributes["pager_alert"] == "green"
    assert rec.attributes["attribution"]  # USGS credit present
    assert rec.severity == "green"  # PAGER alert is the honest severity proxy
    assert rec.observed_at == NOW
    assert rec.provenance[0].local_rf is False
    assert "earthquake" in rec.tags


def test_feature_to_record_rejects_non_earthquake_type() -> None:
    feat = _feature()
    feat["properties"]["type"] = "quarry blast"
    assert feature_to_record(feat, received_at=NOW) is None


def test_feature_to_record_rejects_malformed() -> None:
    assert feature_to_record({"id": "x", "properties": {}}, received_at=NOW) is None  # no geometry
    no_coords = {"id": "x", "properties": {"type": "earthquake"}, "geometry": {"type": "Point"}}
    assert feature_to_record(no_coords, received_at=NOW) is None


# --- Runtime (filtering / dedupe / isolation) ----------------------------------


def _provider() -> FakeUsgsProvider:
    return FakeUsgsProvider(center_lat=CENTER_LAT, center_lon=CENTER_LON, now_fn=lambda: NOW)


def _run_one_poll(*, min_magnitude: float = 0.0) -> list:
    agen = usgs_records(
        _provider(),
        center_lat=CENTER_LAT,
        center_lon=CENTER_LON,
        radius_nm=500.0,
        min_magnitude=min_magnitude,
        poll_s=0.0,
    )
    return asyncio.run(_drive(agen, statuses_wanted=2))


def test_first_record_is_starting_status() -> None:
    records = _run_one_poll()
    assert isinstance(records[0], SourceStatusRecord)
    assert records[0].status == "starting"
    assert records[0].source == SOURCE


def test_aoi_filter_drops_far_quakes() -> None:
    ids = {r.attributes["event_id"] for r in _features(_run_one_poll())}
    # In-AOI earthquakes are present; the ~15° away quake and the quarry blast are not.
    assert "ak_fake_001" in ids
    assert "ak_fake_002" in ids
    assert "ak_fake_003" not in ids  # outside the 500 NM AOI
    assert "ak_fake_005" not in ids  # quarry blast — not an earthquake


def test_min_magnitude_filter() -> None:
    ids = {r.attributes["event_id"] for r in _features(_run_one_poll(min_magnitude=3.0))}
    assert ids == {"ak_fake_001", "ak_fake_002"}  # the M0.8 micro quake is below the floor


def test_connected_status_reports_attribution_and_counts() -> None:
    status = _run_one_poll()[-1]
    assert isinstance(status, SourceStatusRecord)
    assert status.status == "connected"
    assert status.attributes["attribution"] == "USGS Earthquake Hazards Program"
    assert status.attributes["emitted_this_poll"] == 3
    assert status.attributes["in_aoi"] == 3


def test_unchanged_quakes_are_deduped_across_polls() -> None:
    # Same provider, same `updated` timestamps → a second poll must emit nothing new.
    agen = usgs_records(
        _provider(),
        center_lat=CENTER_LAT,
        center_lon=CENTER_LON,
        radius_nm=500.0,
        poll_s=0.0,
    )
    records = asyncio.run(_drive(agen, statuses_wanted=3))  # two completed polls
    # Three features total (all from the first poll); the second poll re-emits none.
    assert len(_features(records)) == 3


def test_fetch_failure_degrades_without_crashing() -> None:
    class _Failing:
        name = "boom"

        async def fetch(self) -> dict:
            raise RuntimeError("network down")

    agen = usgs_records(
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
        Settings(), usgs_feed_url="fake", usgs_center_lat=CENTER_LAT, usgs_center_lon=CENTER_LON
    )
    provider = build_provider(cfg)
    assert isinstance(provider, FakeUsgsProvider)

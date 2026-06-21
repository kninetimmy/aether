"""Unit tests for the SondeHub radiosonde adapter (PRD §11.9, M5.2).

Covers the pure frame→record normalizer (field mapping, ascent/descent derivation,
positionless-frame rejection) and the runtime around it — AOI filtering, serial+frame
dedupe across polls, degraded-on-failure isolation, and provider selection. No broker,
no live call: a fake/stub provider stands in for the network.
"""

import asyncio
import dataclasses
from datetime import UTC, datetime

from aether.adapters.sondehub import (
    SOURCE,
    build_provider,
    frame_to_record,
    sondehub_records,
    telemetry_url,
)
from aether.adapters.sondehub_fake_feeder import FakeSondeHubProvider
from aether.config import Settings
from aether.schema.records import SourceStatusRecord, TrackRecord

NOW = datetime(2026, 6, 18, 12, 0, 0, tzinfo=UTC)
CENTER_LAT, CENTER_LON = 10.0, 20.0


def _frame(**overrides: object) -> dict:
    frame = {
        "uploader_callsign": "N0CALL",
        "time_received": "2026-06-18T12:00:00.000000Z",
        "datetime": "2026-06-18T12:00:00.000000Z",
        "manufacturer": "Vaisala",
        "type": "RS41",
        "subtype": "RS41-SG",
        "serial": "S1234567",
        "frame": 4242,
        "lat": 10.0,
        "lon": 20.0,
        "alt": 18250.0,
        "vel_h": 12.5,
        "vel_v": 5.2,
        "heading": 270.0,
        "temp": -42.5,
        "humidity": 18.0,
        "pressure": 56.0,
        "sats": 9,
        "batt": 2.9,
        "frequency": 404.0,
    }
    frame.update(overrides)
    return frame


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


def _tracks(records: list) -> list[TrackRecord]:
    return [r for r in records if isinstance(r, TrackRecord)]


# --- Pure normalizer -----------------------------------------------------------


def test_frame_to_record_maps_displayable_fields() -> None:
    rec = frame_to_record(_frame(), serial="S1234567", received_at=NOW)
    assert rec is not None
    assert rec.id == "sonde:S1234567"
    assert rec.correlation_key == "sonde:S1234567"  # serial is the identity/dedupe key
    assert rec.track_type == "radiosonde"
    assert rec.source == SOURCE
    assert rec.geometry.coordinates == [20.0, 10.0]  # [lon, lat]
    # First-class track fields carry the dynamics (already SI / m/s upstream).
    assert rec.altitude_m == 18250.0
    assert rec.speed_mps == 12.5
    assert rec.heading_deg == 270.0
    assert rec.vertical_rate_mps == 5.2
    assert rec.attributes["serial"] == "S1234567"
    assert rec.attributes["sonde_type"] == "RS41"
    assert rec.attributes["ascent_state"] == "ascending"  # vel_v > +eps
    assert rec.attributes["uploader_callsign"] == "N0CALL"
    assert rec.attributes["frame"] == 4242
    assert rec.attributes["attribution"]  # SondeHub credit present
    assert rec.observed_at == datetime(2026, 6, 18, 12, 0, 0, tzinfo=UTC)
    assert rec.locally_received is False  # network-only Internet feed
    assert rec.predicted is False  # this is an observation, not a predicted landing
    assert rec.provenance[0].local_rf is False
    assert "radiosonde" in rec.tags


def test_ascent_state_descending_and_float() -> None:
    desc = frame_to_record(_frame(vel_v=-8.4), serial="S1", received_at=NOW)
    flat = frame_to_record(_frame(vel_v=0.0), serial="S2", received_at=NOW)
    assert desc is not None and desc.attributes["ascent_state"] == "descending"
    assert flat is not None and flat.attributes["ascent_state"] == "float"


def test_frame_to_record_rejects_positionless() -> None:
    no_pos = _frame()
    del no_pos["lat"]
    assert frame_to_record(no_pos, serial="S1234567", received_at=NOW) is None
    assert frame_to_record(_frame(lat=999.0), serial="S1234567", received_at=NOW) is None


def test_frame_to_record_drops_garbage_heading() -> None:
    rec = frame_to_record(_frame(heading=540.0), serial="S1", received_at=NOW)
    assert rec is not None and rec.heading_deg is None  # out-of-range heading dropped


# --- URL builder ---------------------------------------------------------------


def test_telemetry_url_carries_aoi_and_recency() -> None:
    url = telemetry_url(
        "https://api.v2.sondehub.org/",
        center_lat=10.0,
        center_lon=20.0,
        radius_nm=500.0,
        recency_s=3600.0,
    )
    assert url.startswith("https://api.v2.sondehub.org/sondes?")
    assert "lat=10.000000" in url
    assert "lon=20.000000" in url
    assert "distance=926000" in url  # 500 NM in metres
    assert "last=3600" in url


# --- Runtime (filtering / dedupe / isolation) ----------------------------------


def _provider() -> FakeSondeHubProvider:
    return FakeSondeHubProvider(center_lat=CENTER_LAT, center_lon=CENTER_LON, now_fn=lambda: NOW)


def _run_one_poll() -> list:
    agen = sondehub_records(
        _provider(),
        center_lat=CENTER_LAT,
        center_lon=CENTER_LON,
        radius_nm=500.0,
        poll_s=0.0,
    )
    return asyncio.run(_drive(agen, statuses_wanted=2))


def test_first_record_is_starting_status() -> None:
    records = _run_one_poll()
    assert isinstance(records[0], SourceStatusRecord)
    assert records[0].status == "starting"
    assert records[0].source == SOURCE


def test_aoi_filter_drops_far_sondes_and_positionless() -> None:
    serials = {r.attributes["serial"] for r in _tracks(_run_one_poll())}
    # In-AOI sondes present; the ~15° away sonde and the positionless frame are not.
    assert "RS41_FAKE_001" in serials  # ascending, at center
    assert "M10_FAKE_002" in serials  # descending, short hop
    assert "RS41_FAKE_003" not in serials  # outside the 500 NM AOI
    assert "DFM_FAKE_004" not in serials  # no position fix


def test_connected_status_reports_attribution_and_counts() -> None:
    status = _run_one_poll()[-1]
    assert isinstance(status, SourceStatusRecord)
    assert status.status == "connected"
    assert status.attributes["attribution"] == "SondeHub (Project Horus) radiosonde network"
    assert status.attributes["emitted_this_poll"] == 2
    assert status.attributes["in_aoi"] == 2


def test_unchanged_sondes_are_deduped_across_polls() -> None:
    # Same provider, fixed now → identical frame numbers → a second poll emits nothing.
    agen = sondehub_records(
        _provider(),
        center_lat=CENTER_LAT,
        center_lon=CENTER_LON,
        radius_nm=500.0,
        poll_s=0.0,
    )
    records = asyncio.run(_drive(agen, statuses_wanted=3))  # two completed polls
    assert len(_tracks(records)) == 2  # both from the first poll; second re-emits none


def test_fetch_failure_degrades_without_crashing() -> None:
    class _Failing:
        name = "boom"

        async def fetch_telemetry(self) -> dict:
            raise RuntimeError("network down")

    agen = sondehub_records(
        _Failing(),
        center_lat=CENTER_LAT,
        center_lon=CENTER_LON,
        radius_nm=500.0,
        poll_s=0.0,
    )
    records = asyncio.run(_drive(agen, statuses_wanted=2))
    assert _tracks(records) == []
    degraded = records[-1]
    assert isinstance(degraded, SourceStatusRecord)
    assert degraded.status == "degraded"
    assert degraded.error_code == "RuntimeError"


def test_build_provider_selects_fake_feeder() -> None:
    cfg = dataclasses.replace(
        Settings(),
        sondehub_api_base="fake",
        sondehub_center_lat=CENTER_LAT,
        sondehub_center_lon=CENTER_LON,
    )
    provider = build_provider(cfg)
    assert isinstance(provider, FakeSondeHubProvider)

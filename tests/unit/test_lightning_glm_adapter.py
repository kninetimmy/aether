"""Unit tests for the NOAA GOES GLM lightning adapter (PRD §11.10, M5.6).

Covers the pure flash→record normalizer (id form, geometry, TTL expiry, honest
total-lightning labeling with no severity), the helpers (bucket host, filename start-time
fallback, ISO-Z parsing), and the runtime — AOI filtering, the optional good-quality filter,
file-key dedupe across polls, backlog capping, list/fetch failure isolation, the missing-parser
offline propagation, and provider selection. The NetCDF parser is exercised separately and
skipped when ``netCDF4`` (the optional ``[lightning]`` dep) is absent. No broker, no live call.
"""

import asyncio
import dataclasses
from datetime import UTC, datetime, timedelta

import pytest

from aether.adapters.lightning_glm import (
    SOURCE,
    GlmFile,
    GlmFlash,
    GlmParserUnavailable,
    GlmS3Provider,
    build_provider,
    flash_to_record,
    glm_records,
    sat_bucket,
    start_time_from_key,
)
from aether.adapters.lightning_glm_fake_feeder import FakeGlmProvider
from aether.config import Settings
from aether.schema.records import GeoFeatureRecord, SourceStatusRecord

NOW = datetime(2026, 6, 21, 20, 0, 30, tzinfo=UTC)
CENTER_LAT, CENTER_LON = 39.0, -98.0


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


def _flash(**kw) -> GlmFlash:
    base = {
        "flash_id": 7,
        "lat": CENTER_LAT,
        "lon": CENTER_LON,
        "observed_at": NOW,
        "energy_j": 4.5e-14,
        "area_m2": 180_000.0,
        "quality_flag": 0,
    }
    base.update(kw)
    return GlmFlash(**base)  # type: ignore[arg-type]


# --- Helpers -------------------------------------------------------------------


def test_sat_bucket_maps_goes_ids() -> None:
    host = "https://noaa-goes19.s3.amazonaws.com"
    assert sat_bucket("G19") == host
    assert sat_bucket("GOES-19") == host
    assert sat_bucket("19") == host
    assert sat_bucket("g18") == "https://noaa-goes18.s3.amazonaws.com"


def test_start_time_from_key_parses_s_token() -> None:
    key = "GLM-L2-LCFA/2026/172/20/OR_GLM-L2-LCFA_G19_s20261722000000_e20261722000200_c....nc"
    assert start_time_from_key(key) == datetime(2026, 6, 21, 20, 0, 0, tzinfo=UTC)
    assert start_time_from_key("no-s-token-here") is None


# --- Pure normalizer -----------------------------------------------------------


def test_flash_to_record_maps_fields_and_ttl() -> None:
    rec = flash_to_record(_flash(), satellite="G19", received_at=NOW, ttl_s=600.0)
    assert rec.feature_type == "lightning_flash"
    assert rec.source == SOURCE
    assert rec.geometry.coordinates == [CENTER_LON, CENTER_LAT]  # [lon, lat]
    assert rec.id == rec.correlation_key
    assert rec.id.startswith("lightning:glm:G19:7:")  # PRD §23 correlation form
    assert rec.observed_at == NOW
    # Transient flash ages off via the live-state expiry sweep (bounded memory).
    assert rec.valid_until == NOW + timedelta(seconds=600.0)
    # Honest labeling: total-lightning, no graded hazard severity (LIGHTNING-FR-004).
    assert rec.severity is None
    assert "not a confirmed cloud-to-ground" in rec.attributes["caveat"].lower()
    assert rec.attributes["attribution"].startswith("NOAA")
    assert rec.attributes["energy_fj"] == pytest.approx(45.0)  # 4.5e-14 J -> 45 fJ
    assert rec.attributes["area_km2"] == pytest.approx(0.18)  # 180_000 m^2
    assert rec.attributes["good_quality"] is True
    assert rec.provenance[0].local_rf is False
    assert rec.provenance[0].confidence == "high"  # quality_flag == 0
    assert "lightning" in rec.tags and "G19" in rec.tags


def test_flash_to_record_degraded_quality_is_medium_confidence() -> None:
    rec = flash_to_record(_flash(quality_flag=1), satellite="G19", received_at=NOW, ttl_s=600.0)
    assert rec.attributes["good_quality"] is False
    assert rec.provenance[0].confidence == "medium"


def test_flash_to_record_handles_missing_energy() -> None:
    rec = flash_to_record(
        _flash(energy_j=None, area_m2=None), satellite="G19", received_at=NOW, ttl_s=600.0
    )
    assert rec.attributes["energy_fj"] is None
    assert rec.attributes["area_km2"] is None
    assert rec.label == "Lightning flash"


# --- Runtime (filtering / dedupe / isolation) ----------------------------------


def _provider() -> FakeGlmProvider:
    return FakeGlmProvider(center_lat=CENTER_LAT, center_lon=CENTER_LON, now_fn=lambda: NOW)


def _run_one_poll(*, good_quality_only: bool = False) -> list:
    agen = glm_records(
        _provider(),
        center_lat=CENTER_LAT,
        center_lon=CENTER_LON,
        radius_nm=500.0,
        poll_s=0.0,
        good_quality_only=good_quality_only,
    )
    return asyncio.run(_drive(agen, statuses_wanted=2))


def test_first_record_is_starting_status() -> None:
    records = _run_one_poll()
    assert isinstance(records[0], SourceStatusRecord)
    assert records[0].status == "starting"
    assert records[0].source == SOURCE


def test_aoi_filter_drops_far_flash() -> None:
    recs = _features(_run_one_poll())
    # center (good) + short-hop (good) + degraded-at-center are in the 500 NM AOI; the
    # ~15° away flash is not.
    assert len(recs) == 3
    for r in recs:
        lon, lat = r.geometry.coordinates
        assert abs(lat - CENTER_LAT) < 5.0 and abs(lon - CENTER_LON) < 5.0


def test_good_quality_only_drops_degraded_flash() -> None:
    recs = _features(_run_one_poll(good_quality_only=True))
    assert len(recs) == 2  # the degraded (quality_flag=1) flash is dropped
    assert all(r.attributes["good_quality"] is True for r in recs)


def test_connected_status_reports_counts_and_satellite() -> None:
    status = _run_one_poll()[-1]
    assert isinstance(status, SourceStatusRecord)
    assert status.status == "connected"
    assert status.attributes["attribution"].startswith("NOAA")
    assert status.attributes["emitted_this_poll"] == 3
    assert status.attributes["in_aoi"] == 3
    assert status.attributes["files_fetched"] == 1
    assert status.attributes["satellite"] == "GFAKE"


def test_flashes_deduped_across_polls() -> None:
    # Fixed now_fn ⇒ the same 20 s window key each poll ⇒ the second poll re-emits nothing.
    agen = glm_records(
        _provider(),
        center_lat=CENTER_LAT,
        center_lon=CENTER_LON,
        radius_nm=500.0,
        poll_s=0.0,
    )
    records = asyncio.run(_drive(agen, statuses_wanted=3))  # two completed polls
    assert len(_features(records)) == 3


def test_list_failure_degrades_without_crashing() -> None:
    class _Failing:
        name = "boom"

        async def list_keys(self) -> list[str]:
            raise RuntimeError("s3 down")

        async def fetch(self, key: str) -> GlmFile:  # pragma: no cover - never reached
            raise AssertionError

    agen = glm_records(
        _Failing(), center_lat=CENTER_LAT, center_lon=CENTER_LON, radius_nm=500.0, poll_s=0.0
    )
    records = asyncio.run(_drive(agen, statuses_wanted=2))
    assert _features(records) == []
    degraded = records[-1]
    assert isinstance(degraded, SourceStatusRecord)
    assert degraded.status == "degraded"
    assert degraded.error_code == "RuntimeError"


def test_fetch_failure_is_isolated_and_degrades() -> None:
    class _BadFile:
        name = "glm-s3:G19"

        async def list_keys(self) -> list[str]:
            return ["GLM-L2-LCFA/2026/172/20/OR_GLM-L2-LCFA_G19_s20261722000000_e_c.nc"]

        async def fetch(self, key: str) -> GlmFile:
            raise RuntimeError("corrupt netcdf")

    agen = glm_records(
        _BadFile(), center_lat=CENTER_LAT, center_lon=CENTER_LON, radius_nm=500.0, poll_s=0.0
    )
    records = asyncio.run(_drive(agen, statuses_wanted=2))  # starting, then the poll's status
    assert _features(records) == []
    status = records[-1]
    assert isinstance(status, SourceStatusRecord)
    assert status.status == "degraded"  # a bad file with no emit degrades
    assert status.records_rejected == 1


def test_parser_unavailable_propagates() -> None:
    class _NoParser:
        name = "glm-s3:G19"

        async def list_keys(self) -> list[str]:
            return ["GLM-L2-LCFA/2026/172/20/OR_GLM-L2-LCFA_G19_s20261722000000_e_c.nc"]

        async def fetch(self, key: str) -> GlmFile:
            raise GlmParserUnavailable("netCDF4 missing")

    async def _run() -> None:
        agen = glm_records(
            _NoParser(), center_lat=CENTER_LAT, center_lon=CENTER_LON, radius_nm=500.0, poll_s=0.0
        )
        async for _ in agen:
            pass

    with pytest.raises(GlmParserUnavailable):
        asyncio.run(_run())


def test_backlog_is_capped_and_all_marked_seen() -> None:
    # 30 keys, cap 5 → newest 5 fetched; all 30 marked seen so none re-emit next poll.
    keys = [
        f"GLM-L2-LCFA/2026/172/20/OR_GLM-L2-LCFA_G19_s202617220{i:02d}000_e_c.nc" for i in range(30)
    ]

    class _Many:
        name = "glm-s3:G19"

        async def list_keys(self) -> list[str]:
            return keys

        async def fetch(self, key: str) -> GlmFile:
            # one in-AOI flash per file, observed time disambiguated by the key token
            base = start_time_from_key(key) or NOW
            return GlmFile(
                key=key,
                satellite="G19",
                time_coverage_start=base,
                flashes=[_flash(flash_id=1, observed_at=base)],
            )

    agen = glm_records(
        _Many(),
        center_lat=CENTER_LAT,
        center_lon=CENTER_LON,
        radius_nm=500.0,
        poll_s=0.0,
        max_files_per_poll=5,
    )
    records = asyncio.run(_drive(agen, statuses_wanted=3))  # two polls
    assert len(_features(records)) == 5  # only the newest 5 of poll 1; poll 2 adds nothing
    connected = [
        r for r in records if isinstance(r, SourceStatusRecord) and r.status == "connected"
    ]
    assert connected[0].attributes["backlog_skipped"] == 25
    assert connected[0].attributes["files_fetched"] == 5


# --- Provider selection --------------------------------------------------------


def test_build_provider_selects_fake_feeder() -> None:
    cfg = dataclasses.replace(
        Settings(), glm_satellite="fake", glm_center_lat=CENTER_LAT, glm_center_lon=CENTER_LON
    )
    assert isinstance(build_provider(cfg), FakeGlmProvider)


def test_build_provider_defaults_to_live_s3() -> None:
    prov = build_provider(Settings())  # live default needs no key — only the optional parser
    assert isinstance(prov, GlmS3Provider)
    assert prov.name == "glm-s3:G19"


# --- NetCDF parser (skipped without the optional [lightning] dep) ---------------


def test_parse_glm_netcdf_reads_flashes(tmp_path) -> None:
    netCDF4 = pytest.importorskip("netCDF4")
    import numpy as np

    from aether.adapters.lightning_glm import parse_glm_netcdf

    path = tmp_path / "glm.nc"
    ds = netCDF4.Dataset(str(path), mode="w", format="NETCDF4")
    ds.platform_ID = "G19"
    ds.time_coverage_start = "2026-06-21T20:00:00.0Z"
    ds.createDimension("number_of_flashes", 2)
    lat = ds.createVariable("flash_lat", "f4", ("number_of_flashes",))
    lon = ds.createVariable("flash_lon", "f4", ("number_of_flashes",))
    fid = ds.createVariable("flash_id", "i2", ("number_of_flashes",))
    energy = ds.createVariable("flash_energy", "f8", ("number_of_flashes",))
    off = ds.createVariable("flash_time_offset_of_first_event", "f8", ("number_of_flashes",))
    qf = ds.createVariable("flash_quality_flag", "i2", ("number_of_flashes",))
    lat[:] = np.array([39.0, 6.0], dtype="f4")
    lon[:] = np.array([-98.0, -60.0], dtype="f4")
    fid[:] = np.array([11, 12], dtype="i2")
    energy[:] = np.array([4.5e-14, 1.2e-13], dtype="f8")
    off[:] = np.array([1.5, -0.5], dtype="f8")
    qf[:] = np.array([0, 1], dtype="i2")
    ds.close()

    raw = path.read_bytes()
    glm_file = parse_glm_netcdf(raw, "OR_GLM-L2-LCFA_G19_s20261722000000_e_c.nc")
    assert glm_file.satellite == "G19"
    assert len(glm_file.flashes) == 2
    f0 = glm_file.flashes[0]
    assert f0.flash_id == 11
    assert f0.lat == pytest.approx(39.0, abs=1e-4)
    assert f0.lon == pytest.approx(-98.0, abs=1e-4)
    assert f0.observed_at == datetime(2026, 6, 21, 20, 0, 1, 500_000, tzinfo=UTC)  # +1.5s
    assert f0.quality_flag == 0

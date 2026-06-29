"""Unit tests for the CelesTrak orbital adapter (PRD §11.14, §18.12, M6.5).

Covers the pure OMM→Satrec build (valid + malformed rows), epoch parsing, the propagate→
record normalizer (predicted labeling, attributes-only az/el/range/epoch/age, no schema
bump), the runtime — elevation filtering, last-good cache, sync/fetch failure isolation, the
301/403/404 no-retry guard, and provider selection — and the missing-``sgp4`` capability gate.
The real SGP4 path is exercised via the fake feeder (so this needs the ``[orbital]`` extra);
the capability gate is tested by stubbing ``build_satrec`` to raise. No broker, no live call.
"""

import asyncio
import dataclasses
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from aether.adapters.celestrak import (
    SOURCE,
    CelestrakHttpProvider,
    CelestrakNoRetry,
    _read_watchlisted_norads,
    build_provider,
    build_satrecs,
    celestrak_records,
    element_to_record,
    parse_epoch,
)
from aether.adapters.celestrak_fake_feeder import FakeCelestrakProvider
from aether.config import Settings
from aether.orbital.sgp4_propagate import Sgp4Unavailable
from aether.persist.database import Database
from aether.persist.watchlist import upsert_watchlist_entry
from aether.schema.records import SCHEMA_VERSION, SourceStatusRecord, TrackRecord
from aether.schema.watchlist import WatchlistEntry, WatchlistEntryCreate

# The feeder-driven tests below run the REAL SGP4 propagate path, so this whole module needs
# the optional ``[orbital]`` extra. Skip cleanly when ``sgp4`` is absent (mirrors the GLM/
# ``netCDF4`` pattern) instead of hanging in ``_drive`` waiting on statuses that never arrive
# because every object fails to propagate. CI installs ``[orbital]`` so this runs there. The
# aether imports above are safe without sgp4 — the adapter imports it lazily, behind the gate.
pytest.importorskip("sgp4")

# Observer for the canned roster: the overhead GEO is solved to sit here.
OBS_LAT, OBS_LON = 30.0, -97.0
NOW = datetime(2026, 6, 21, 18, 0, 0, tzinfo=UTC)


def _feeder() -> FakeCelestrakProvider:
    return FakeCelestrakProvider(observer_lat=OBS_LAT, observer_lon=OBS_LON, now_fn=lambda: NOW)


async def _drive(agen: Any, *, statuses_wanted: int) -> list[Any]:
    """Collect records until ``statuses_wanted`` status records have been seen."""
    records: list[Any] = []
    seen = 0
    async for record in agen:
        records.append(record)
        if isinstance(record, SourceStatusRecord):
            seen += 1
            if seen >= statuses_wanted:
                break
    await agen.aclose()
    return records


def _tracks(records: list[Any]) -> list[TrackRecord]:
    return [r for r in records if isinstance(r, TrackRecord)]


# --- Epoch parsing -------------------------------------------------------------


def test_parse_epoch_reads_naive_as_utc() -> None:
    dt = parse_epoch("2026-06-21T03:14:15.926784")
    assert dt == datetime(2026, 6, 21, 3, 14, 15, 926784, tzinfo=UTC)


def test_parse_epoch_handles_z_suffix_and_bad_input() -> None:
    assert parse_epoch("2026-06-21T00:00:00Z") == datetime(2026, 6, 21, tzinfo=UTC)
    assert parse_epoch("not-a-date") is None


# --- Pure OMM build ------------------------------------------------------------


async def _one_group_rows() -> list[dict[str, Any]]:
    return await _feeder().fetch_group("stations")


def test_build_satrecs_builds_valid_objects() -> None:
    rows = asyncio.run(_one_group_rows())
    elements, skipped = build_satrecs(rows, group="stations")
    assert skipped == 0
    norads = {e.norad_id for e in elements}
    assert 25544 in norads  # ISS is in the canned roster
    iss = next(e for e in elements if e.norad_id == 25544)
    assert iss.object_name == "ISS (ZARYA)"
    assert iss.group == "stations"
    assert iss.epoch.tzinfo is UTC


def test_build_satrecs_skips_malformed_rows() -> None:
    rows = [
        {"EPOCH": "garbage", "NORAD_CAT_ID": 1},  # bad epoch
        {"NORAD_CAT_ID": 2},  # missing everything
    ]
    elements, skipped = build_satrecs(rows, group="x")
    assert elements == []
    assert skipped == 2


# --- Propagate → record normalizer ---------------------------------------------


def test_element_to_record_predicted_and_attributes_only() -> None:
    rows = asyncio.run(_one_group_rows())
    elements, _ = build_satrecs(rows, group="stations")
    overhead = next(e for e in elements if e.object_name == "AETHER-GEO-OVERHEAD")
    rec = element_to_record(
        overhead,
        observer_lat=OBS_LAT,
        observer_lon=OBS_LON,
        observer_alt_m=0.0,
        at=NOW,
        valid_s=30.0,
    )
    assert rec is not None
    assert rec.track_type == "orbital_object"
    assert rec.source == SOURCE
    assert rec.id == "orbital:celestrak:99001"
    assert rec.id == rec.correlation_key
    assert rec.predicted is True  # honest labeling: propagated, not observed
    assert rec.locally_received is False
    assert rec.valid_until is not None  # short freshness so it ages off
    assert rec.geometry is not None
    # az/el/range/epoch/age live in attributes (schema is extra="forbid"; no new top fields).
    attrs = rec.attributes
    for key in (
        "norad_id",
        "object_id",
        "object_name",
        "group",
        "element_epoch_utc",
        "element_age_s",
        "azimuth_deg",
        "elevation_deg",
        "slant_range_m",
        "attribution",
        "caveat",
    ):
        assert key in attrs
    assert attrs["norad_id"] == 99001
    assert attrs["attribution"].startswith("Orbital data: CelesTrak")
    assert "not for navigation" in attrs["caveat"].lower()
    # The overhead GEO is reliably high above the horizon for this observer.
    assert attrs["elevation_deg"] > 10.0
    assert attrs["slant_range_m"] > 35_000_000.0  # geostationary slant range


def test_schema_version_is_unchanged() -> None:
    # This slice adds NO new top-level fields and does NOT bump the schema (maintainer-approved).
    assert SCHEMA_VERSION == 2


def test_track_record_rejects_stray_top_level_field() -> None:
    # The whole attributes-only approach rests on extra="forbid": a stray top-level field
    # must be rejected, guarding against schema creep that would re-open the bump question.
    from datetime import timedelta

    import pydantic

    from aether.schema.geometry import Point

    with pytest.raises(pydantic.ValidationError):
        TrackRecord(
            id="orbital:celestrak:1",
            source=SOURCE,
            observed_at=NOW,
            received_at=NOW,
            published_at=NOW,
            correlation_key="orbital:celestrak:1",
            track_type="orbital_object",
            geometry=Point(coordinates=[0.0, 0.0]),
            valid_until=NOW + timedelta(seconds=30),
            azimuth_deg=123.0,  # type: ignore[call-arg]  # stray top-level field — must be rejected
        )


# --- Runtime: elevation filter + last-good + isolation -------------------------


def _records_agen(provider: Any, **kw: Any) -> Any:
    base = dict(
        groups=("stations",),
        observer_lat=OBS_LAT,
        observer_lon=OBS_LON,
        observer_alt_m=0.0,
        min_elevation_deg=10.0,
        sync_s=1e9,  # one sync for the test
        propagate_s=0.0,
        valid_s=30.0,
        now_fn=lambda: NOW,
    )
    base.update(kw)
    return celestrak_records(provider, **base)


def test_first_record_is_starting_status() -> None:
    records = asyncio.run(_drive(_records_agen(_feeder()), statuses_wanted=1))
    assert isinstance(records[0], SourceStatusRecord)
    assert records[0].status == "starting"
    assert records[0].source == SOURCE


def test_elevation_filter_keeps_overhead_drops_far() -> None:
    records = asyncio.run(_drive(_records_agen(_feeder()), statuses_wanted=2))
    tracks = _tracks(records)
    names = {t.attributes["object_name"] for t in tracks}
    assert "AETHER-GEO-OVERHEAD" in names  # above the horizon → emitted
    assert "AETHER-GEO-FAR" not in names  # below the horizon → filtered (ORBIT-FR-007)
    for t in tracks:
        assert t.attributes["elevation_deg"] >= 10.0


def test_connected_status_reports_counts() -> None:
    records = asyncio.run(_drive(_records_agen(_feeder()), statuses_wanted=2))
    status = records[-1]
    assert isinstance(status, SourceStatusRecord)
    assert status.status == "connected"
    assert status.attributes["tracked_objects"] == 3  # ISS + 2 GEOs in the roster
    assert status.attributes["above_horizon"] >= 1
    assert status.attributes["min_elevation_deg"] == 10.0
    assert status.attributes["attribution"].startswith("Orbital data: CelesTrak")


def test_sync_failure_with_no_cache_degrades() -> None:
    class _Failing:
        name = "boom"

        async def fetch_group(self, group: str) -> list[dict[str, Any]]:
            raise RuntimeError("celestrak down")

    records = asyncio.run(_drive(_records_agen(_Failing()), statuses_wanted=2))
    assert _tracks(records) == []
    degraded = records[-1]
    assert isinstance(degraded, SourceStatusRecord)
    assert degraded.status == "degraded"
    assert degraded.error_code == "SyncFailed"


def test_no_retry_status_is_treated_as_failed_fetch() -> None:
    # A 301/403/404 surfaces as CelestrakNoRetry; with no last-good cache the source degrades
    # rather than tight-looping (the §38 rate-limit guard).
    class _Gone:
        name = "gone"

        async def fetch_group(self, group: str) -> list[dict[str, Any]]:
            raise CelestrakNoRetry("HTTP 404; not retrying")

    records = asyncio.run(_drive(_records_agen(_Gone()), statuses_wanted=2))
    degraded = records[-1]
    assert isinstance(degraded, SourceStatusRecord)
    assert degraded.status == "degraded"


def test_blocking_get_no_retry_on_terminal_status(monkeypatch: pytest.MonkeyPatch) -> None:
    import urllib.error

    from aether.adapters import celestrak as mod

    def _raise(req: Any, timeout: Any) -> Any:
        raise urllib.error.HTTPError("u", 403, "Forbidden", {}, None)  # type: ignore[arg-type]

    monkeypatch.setattr(mod.urllib.request, "urlopen", _raise)
    with pytest.raises(CelestrakNoRetry):
        mod._blocking_get("https://celestrak.org/x", 5.0)


def test_blocking_get_reraises_retryable_status(monkeypatch: pytest.MonkeyPatch) -> None:
    import urllib.error

    from aether.adapters import celestrak as mod

    def _raise(req: Any, timeout: Any) -> Any:
        raise urllib.error.HTTPError("u", 500, "Server Error", {}, None)  # type: ignore[arg-type]

    monkeypatch.setattr(mod.urllib.request, "urlopen", _raise)
    with pytest.raises(urllib.error.HTTPError):  # 500 is retryable — not swallowed
        mod._blocking_get("https://celestrak.org/x", 5.0)


def test_blocking_get_refuses_http_urls() -> None:
    # The live path is https-only; a plain-http URL is refused (never silently downgraded).
    from aether.adapters import celestrak as mod

    with pytest.raises(ValueError, match="non-https"):
        mod._blocking_get("http://celestrak.org/x", 5.0)


# --- Capability gate: missing sgp4 → Sgp4Unavailable propagates ----------------


def test_missing_sgp4_propagates_for_capability_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    from aether.adapters import celestrak as mod

    def _no_sgp4(fields: dict[str, Any]) -> Any:
        raise Sgp4Unavailable("sgp4 missing")

    monkeypatch.setattr(mod, "build_satrec", _no_sgp4)

    async def _run() -> None:
        async for _ in _records_agen(_feeder()):
            pass

    with pytest.raises(Sgp4Unavailable):
        asyncio.run(_run())


# --- Provider selection --------------------------------------------------------


def test_build_provider_selects_fake_feeder() -> None:
    cfg = dataclasses.replace(
        Settings(),
        celestrak_base_url="fake",
        celestrak_observer_lat=OBS_LAT,
        celestrak_observer_lon=OBS_LON,
    )
    prov = build_provider(cfg)
    assert isinstance(prov, FakeCelestrakProvider)


def test_build_provider_defaults_to_live_http() -> None:
    prov = build_provider(Settings())  # live default needs no key — only the optional parser
    assert isinstance(prov, CelestrakHttpProvider)
    assert prov.name == "celestrak"


def test_http_provider_sends_format_json_explicitly() -> None:
    captured: dict[str, str] = {}

    async def _fetch(url: str) -> bytes:
        captured["url"] = url
        return b"[]"

    prov = CelestrakHttpProvider("https://celestrak.org", fetch=_fetch)
    asyncio.run(prov.fetch_group("stations"))
    # Parse the query string so FORMAT=json is a *distinct* parameter, not a substring match
    # (the explicit send is load-bearing — the service default became CSV 2026-05-09).
    import urllib.parse

    qs = urllib.parse.parse_qs(urllib.parse.urlsplit(captured["url"]).query)
    assert qs["FORMAT"] == ["json"]
    assert qs["GROUP"] == ["stations"]


def test_http_provider_url_encodes_group() -> None:
    # A group with query-special characters must be percent-encoded, not injected raw.
    captured: dict[str, str] = {}

    async def _fetch(url: str) -> bytes:
        captured["url"] = url
        return b"[]"

    import urllib.parse

    prov = CelestrakHttpProvider("https://celestrak.org", fetch=_fetch)
    asyncio.run(prov.fetch_group("evil&FORMAT=csv"))
    parsed = urllib.parse.parse_qs(urllib.parse.urlsplit(captured["url"]).query)
    assert parsed["FORMAT"] == ["json"]  # injection did NOT override FORMAT
    assert parsed["GROUP"] == ["evil&FORMAT=csv"]  # decoded back to the literal group


# --- Two-tier watchlist-driven propagation (ORBIT-FR-011, M6.6b Part B) ---------


def _migrated_db(tmp_path: Path) -> str:
    """A store with the schema applied (migration v4 creates ``watchlist``)."""
    path = str(tmp_path / "watchlist.db")
    db = Database(path)
    db.open()  # runs all migrations including v4 (watchlist)
    db.close()
    return path


def _seed_watch(path: str, *keys: str) -> None:
    for key in keys:
        upsert_watchlist_entry(
            path, WatchlistEntry.create(WatchlistEntryCreate(), key=key, now=NOW)
        )


def test_read_watchlisted_norads_parses_orbital_keys(tmp_path: Path) -> None:
    path = _migrated_db(tmp_path)
    _seed_watch(
        path,
        "orbital:celestrak:25544",
        "orbital:celestrak:99001",
        "aircraft:icao:abc123",  # not orbital — filtered out
        "orbital:celestrak:NaN",  # malformed suffix — skipped, never raises
    )
    assert _read_watchlisted_norads(path) == {25544, 99001}
    # A missing store must degrade to empty, never raise (honest degradation, §37).
    assert _read_watchlisted_norads("/no/such.db") == set()


def test_watchlist_none_is_single_cadence() -> None:
    # No watchlist_source ⇒ the fast tier collapses and the stream is identical-to-today.
    records = asyncio.run(_drive(_records_agen(_feeder()), statuses_wanted=2))
    status = records[-1]
    assert isinstance(status, SourceStatusRecord)
    assert status.attributes["fast_tracked"] == 0
    assert status.attributes["slow_tracked"] == 3  # whole roster rides the slow tier
    assert status.attributes["tracked_objects"] == 3
    assert "propagate_fast_s" in status.attributes  # cadence transparency present even when off


def test_fast_tier_emits_watched_only_no_double_emit() -> None:
    # propagate_fast_s=1e9 parks the fast tier after its first tick; the slow tier permanently
    # excludes the watchlisted NORAD ⇒ 99001 appears EXACTLY once (ironclad disjoint proof).
    records = asyncio.run(
        _drive(
            _records_agen(
                _feeder(),
                watchlist_source=lambda: {99001},
                propagate_fast_s=1e9,
                propagate_s=0.0,
                watchlist_refresh_s=1e9,
            ),
            statuses_wanted=4,  # starting + 3 slow connecteds
        )
    )
    overhead = [t for t in _tracks(records) if t.attributes["object_name"] == "AETHER-GEO-OVERHEAD"]
    assert len(overhead) == 1  # fast tier, tick 1 only — never double-emitted by the slow tier
    assert overhead[0].attributes["norad_id"] == 99001


def test_connected_status_partition_counts() -> None:
    # Fast fires before slow within tick 1, so fast_above_horizon reads 1 in the first status.
    records = asyncio.run(
        _drive(
            _records_agen(
                _feeder(),
                watchlist_source=lambda: {99001},
                propagate_fast_s=0.0,
                propagate_s=0.0,
                watchlist_refresh_s=1e9,
            ),
            statuses_wanted=2,
        )
    )
    status = records[-1]
    assert isinstance(status, SourceStatusRecord)
    assert status.attributes["tracked_objects"] == 3
    assert status.attributes["watchlisted"] == 1
    assert status.attributes["fast_tracked"] == 1  # 99001 ∩ synced catalog
    assert status.attributes["slow_tracked"] == 2  # ISS + far GEO
    assert status.attributes["fast_above_horizon"] == 1  # overhead GEO is reliably above horizon


def test_watchlist_refresh_promotes_without_restart() -> None:
    # The reader returns empty first, then {99001}: a live re-read must move 99001 into the
    # fast tier WITHOUT restarting the adapter (watchlist_refresh_s=0.0 re-reads every tick).
    calls = [0]

    def reader() -> set[int]:
        calls[0] += 1
        return set() if calls[0] == 1 else {99001}

    records = asyncio.run(
        _drive(
            _records_agen(
                _feeder(),
                watchlist_source=reader,
                watchlist_refresh_s=0.0,
                propagate_s=0.0,
                propagate_fast_s=1e9,
            ),
            statuses_wanted=3,
        )
    )
    fast_counts = [
        r.attributes["fast_tracked"]
        for r in records
        if isinstance(r, SourceStatusRecord) and r.status == "connected"
    ]
    assert 0 in fast_counts  # before promotion
    assert 1 in fast_counts  # after the live re-read — no restart


def test_watchlist_read_error_is_isolated() -> None:
    # A raising reader must be caught, logged, and treated as empty — the adapter degrades to a
    # single tier for that cycle and never crashes (failure isolation, §37).
    def boom() -> set[int]:
        raise RuntimeError("watchlist exploded")

    records = asyncio.run(
        _drive(
            _records_agen(_feeder(), watchlist_source=boom, propagate_s=0.0),
            statuses_wanted=2,
        )
    )
    status = records[-1]
    assert isinstance(status, SourceStatusRecord)
    assert status.attributes["fast_tracked"] == 0  # treated as empty
    names = {t.attributes["object_name"] for t in _tracks(records)}
    assert "AETHER-GEO-OVERHEAD" in names  # slow tier still emitted it (degraded single tier)

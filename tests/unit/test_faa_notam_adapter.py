"""Unit tests for the FAA NOTAM adapter (PRD §11.13, §18.11, M6.4).

Covers the pure Feature→record normalizer (supplied geometry → GeoFeature; null/malformed
geometry → textual facility-panel event, never an invented shape; cancellation → dropped),
the geometry-member parser, the ISO-time helper, the live HTTP provider (radius cap,
credentials in headers not the URL, secret redaction, 401/403 → terminal auth error), and
the runtime around it — pagination, the per-poll page budget, revision dedupe across polls,
degraded-on-failure isolation, the unauthorized stop, and the missing-credentials gate.
No broker, no live call: a fake/stub provider stands in for the network.
"""

import asyncio
import dataclasses
import json
from datetime import UTC, datetime
from typing import Any

from aether.adapters.faa_notam import (
    SOURCE,
    FaaNotamHttpProvider,
    NotamAuthError,
    _parse_iso,
    build_provider,
    geometry_from_member,
    notam_records,
    parse_feature,
)
from aether.adapters.faa_notam_fake_feeder import FakeFaaNotamProvider
from aether.config import Settings
from aether.schema.geometry import MultiPolygon, Polygon
from aether.schema.records import EventRecord, GeoFeatureRecord, SourceStatusRecord

NOW = datetime(2026, 6, 21, 12, 0, 0, tzinfo=UTC)
CENTER_LAT, CENTER_LON = 10.0, 20.0

_RING = [[20.0, 10.0], [20.2, 10.0], [20.2, 10.2], [20.0, 10.2], [20.0, 10.0]]
_RING_B = [[20.5, 10.5], [20.6, 10.5], [20.6, 10.6], [20.5, 10.6], [20.5, 10.5]]
_GC_ONE = {
    "type": "GeometryCollection",
    "geometries": [{"type": "Polygon", "coordinates": [_RING]}],
}
_GC_TWO = {
    "type": "GeometryCollection",
    "geometries": [
        {"type": "Polygon", "coordinates": [_RING]},
        {"type": "Polygon", "coordinates": [_RING_B]},
    ],
}
# A vertex at latitude 200° is out of range — the ring is rejected, not coerced.
_GC_BAD = {
    "type": "GeometryCollection",
    "geometries": [
        {"type": "Polygon", "coordinates": [[[20.0, 200.0], [20.1, 10.0], [20.2, 10.1]]]}
    ],
}


def _feature(
    *,
    nid: str = "NOTAM_1",
    number: str = "01/001",
    ntype: str = "N",
    geometry: dict[str, Any] | None = None,
    text: str = "!ZZZ 01/001 ZZZ AIRSPACE ...",
    **notam_extra: Any,
) -> dict[str, Any]:
    notam = {
        "id": nid,
        "number": number,
        "type": ntype,
        "classification": "DOM",
        "icaoLocation": "KZZZ",
        "issued": "2026-06-21T10:00:00.000Z",
        "lastUpdated": "2026-06-21T11:00:00.000Z",
        "effectiveStart": "2026-06-21T11:30:00.000Z",
        "effectiveEnd": "2026-06-23T11:30:00.000Z",
        "text": text,
        **notam_extra,
    }
    return {
        "type": "Feature",
        "properties": {"coreNOTAMData": {"notam": notam}},
        "geometry": geometry,
    }


async def _drive(agen: Any, *, statuses_wanted: int) -> list:
    """Collect records until ``statuses_wanted`` statuses are seen (or the stream ends)."""
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


def _events(records: list) -> list[EventRecord]:
    return [r for r in records if isinstance(r, EventRecord)]


# --- ISO time helper -----------------------------------------------------------


def test_parse_iso_handles_z_suffix_perm_and_garbage() -> None:
    assert _parse_iso("2026-06-21T11:30:00.000Z") == datetime(2026, 6, 21, 11, 30, tzinfo=UTC)
    assert _parse_iso("2026-06-21T11:30:00") == datetime(2026, 6, 21, 11, 30, tzinfo=UTC)
    assert _parse_iso("PERM") is None  # permanent NOTAM → open-ended, not an error
    assert _parse_iso("") is None
    assert _parse_iso("not-a-date") is None
    assert _parse_iso(None) is None


# --- Geometry member parser ----------------------------------------------------


def test_geometry_from_member_collection_one_polygon() -> None:
    geom, dropped = geometry_from_member(_GC_ONE)
    assert isinstance(geom, Polygon)
    assert geom.coordinates == [_RING]
    assert dropped == 0


def test_geometry_from_member_collection_two_polygons_is_multipolygon() -> None:
    geom, dropped = geometry_from_member(_GC_TWO)
    assert isinstance(geom, MultiPolygon)
    assert geom.coordinates == [[_RING], [_RING_B]]
    assert dropped == 0


def test_geometry_from_member_accepts_bare_polygon_and_multipolygon() -> None:
    poly, _ = geometry_from_member({"type": "Polygon", "coordinates": [_RING]})
    assert isinstance(poly, Polygon)
    multi, _ = geometry_from_member({"type": "MultiPolygon", "coordinates": [[_RING], [_RING_B]]})
    assert isinstance(multi, MultiPolygon)
    assert multi.coordinates == [[_RING], [_RING_B]]


def test_geometry_from_member_null_and_unusable_return_none() -> None:
    assert geometry_from_member(None) == (None, 0)
    assert geometry_from_member({"type": "Point", "coordinates": [20.0, 10.0]}) == (None, 0)


def test_geometry_from_member_drops_malformed_member() -> None:
    geom, dropped = geometry_from_member(_GC_BAD)
    assert geom is None  # the only member was unusable
    assert dropped == 1


# --- Pure Feature normalizer ---------------------------------------------------


def test_parse_feature_supplied_geometry_becomes_geofeature() -> None:
    rec = parse_feature(_feature(geometry=_GC_ONE), received_at=NOW)
    assert isinstance(rec, GeoFeatureRecord)
    assert rec.source == SOURCE
    assert rec.feature_type == "notam_geometry"
    assert isinstance(rec.geometry, Polygon)
    assert rec.id == "notam:faa:NOTAM_1"
    assert rec.correlation_key == "notam:faa:NOTAM_1"
    assert rec.valid_from == datetime(2026, 6, 21, 11, 30, tzinfo=UTC)
    assert rec.valid_until == datetime(2026, 6, 23, 11, 30, tzinfo=UTC)
    assert rec.severity is None  # NOTAMs are not severity-ranked here
    assert "01/001" in (rec.label or "") and "KZZZ" in (rec.label or "")
    assert rec.attributes["text"] == "!ZZZ 01/001 ZZZ AIRSPACE ..."  # original text retained
    assert rec.attributes["attribution"] == "FAA NOTAMs (external-api.faa.gov)"
    assert "not a flight-planning product" in rec.attributes["caveat"].lower()
    assert rec.provenance[0].local_rf is False  # network-only feed
    assert rec.provenance[0].provider == "faa"


def test_parse_feature_null_geometry_becomes_textual_event() -> None:
    rec = parse_feature(
        _feature(nid="N3", number="01/003", geometry=None, text="RWY 12/30 CLSD"), received_at=NOW
    )
    assert isinstance(rec, EventRecord)
    assert rec.event_type == "notam_textual"
    assert rec.correlation_key == "notam:faa:N3"
    assert rec.subject_id == "notam:faa:N3"
    assert rec.severity == "low"
    assert "RWY 12/30 CLSD" in (rec.message or "")  # original text preserved (AIRSPACE-FR-006)
    assert "not a flight-planning product" in (rec.message or "").lower()
    assert rec.attributes["number"] == "01/003"


def test_parse_feature_malformed_geometry_becomes_unparseable_event() -> None:
    rec = parse_feature(_feature(nid="N4", geometry=_GC_BAD), received_at=NOW)
    assert isinstance(rec, EventRecord)
    assert rec.event_type == "notam_geometry_unparseable"
    assert rec.attributes["dropped_areas"] == 1
    assert "unparseable" in rec.summary.lower()


def test_parse_feature_cancellation_is_dropped() -> None:
    assert parse_feature(_feature(ntype="C", geometry=_GC_ONE), received_at=NOW) is None


def test_parse_feature_without_notam_body_is_dropped() -> None:
    assert parse_feature({"type": "Feature", "properties": {}}, received_at=NOW) is None
    assert parse_feature({"type": "Feature", "geometry": _GC_ONE}, received_at=NOW) is None


# --- Live HTTP provider --------------------------------------------------------


def test_http_provider_caps_radius_and_keeps_creds_in_headers() -> None:
    captured: dict[str, Any] = {}

    async def fake_fetch(url: str, headers: dict[str, str]) -> bytes:
        captured["url"] = url
        captured["headers"] = headers
        return json.dumps({"totalPages": 1, "items": []}).encode()

    prov = FaaNotamHttpProvider(
        "https://example.test",
        client_id="CID",
        client_secret="SECRET",
        center_lat=10.0,
        center_lon=20.0,
        radius_nm=500.0,  # wider than the FAA max — must be capped
        page_size=50,
        fetch=fake_fetch,
    )
    page = asyncio.run(prov.fetch_page(1))
    assert page["items"] == []
    assert prov.effective_radius_nm == 100.0
    assert "locationRadius=100" in captured["url"]
    # Credentials travel in headers, never in the URL/query (no leak via logs/redirects).
    assert "CID" not in captured["url"] and "SECRET" not in captured["url"]
    assert captured["headers"]["client_id"] == "CID"
    assert captured["headers"]["client_secret"] == "SECRET"


def test_http_provider_redacts_credentials_in_errors() -> None:
    async def boom(url: str, headers: dict[str, str]) -> bytes:
        raise RuntimeError("connect failed leaking SECRET here")

    prov = FaaNotamHttpProvider(
        "https://example.test",
        client_id="CID",
        client_secret="SECRET",
        center_lat=0.0,
        center_lon=0.0,
        radius_nm=50.0,
        page_size=50,
        fetch=boom,
    )
    try:
        asyncio.run(prov.fetch_page(1))
    except RuntimeError as exc:
        assert "SECRET" not in str(exc)
        assert "***" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("fetch_page should redact credentials from errors")


def test_http_provider_propagates_auth_error() -> None:
    async def auth(url: str, headers: dict[str, str]) -> bytes:
        raise NotamAuthError("NOTAM API returned HTTP 403")

    prov = FaaNotamHttpProvider(
        "https://example.test",
        client_id="CID",
        client_secret="SECRET",
        center_lat=0.0,
        center_lon=0.0,
        radius_nm=50.0,
        page_size=50,
        fetch=auth,
    )
    try:
        asyncio.run(prov.fetch_page(1))
    except NotamAuthError:
        pass
    else:  # pragma: no cover
        raise AssertionError("a 401/403 must surface as NotamAuthError, not be redacted")


# --- Runtime (pagination / dedupe / isolation) ---------------------------------


def _provider() -> FakeFaaNotamProvider:
    return FakeFaaNotamProvider(center_lat=CENTER_LAT, center_lon=CENTER_LON, now_fn=lambda: NOW)


def _run(*, statuses_wanted: int, max_pages: int = 5) -> list:
    agen = notam_records(_provider(), poll_s=0.0, max_pages_per_poll=max_pages)
    return asyncio.run(_drive(agen, statuses_wanted=statuses_wanted))


def test_first_record_is_starting_status() -> None:
    records = _run(statuses_wanted=1)
    assert isinstance(records[0], SourceStatusRecord)
    assert records[0].status == "starting"
    assert records[0].source == SOURCE


def test_one_poll_emits_two_features_and_two_events() -> None:
    records = _run(statuses_wanted=2)
    feats = _features(records)
    events = _events(records)
    # NOTAM_FAKE_1 (Polygon) + NOTAM_FAKE_2 (MultiPolygon); the cancelled #5 is dropped.
    assert {type(f.geometry) for f in feats} == {Polygon, MultiPolygon}
    assert len(feats) == 2
    # NOTAM_FAKE_3 (null geometry) + NOTAM_FAKE_4 (malformed) → textual events.
    assert {e.event_type for e in events} == {"notam_textual", "notam_geometry_unparseable"}
    assert len(events) == 2


def test_connected_status_reports_pagination_and_attribution() -> None:
    status = _run(statuses_wanted=2)[-1]
    assert isinstance(status, SourceStatusRecord)
    assert status.status == "connected"
    assert status.attributes["attribution"] == "FAA NOTAMs (external-api.faa.gov)"
    assert status.attributes["total_pages"] == 2
    assert status.attributes["fetched_pages"] == 2
    assert status.attributes["listed"] == 5  # both pages flattened
    assert status.attributes["emitted_this_poll"] == 4  # 2 features + 2 events
    assert status.attributes["query_radius_nm"] == 100.0


def test_max_pages_budget_limits_fetch() -> None:
    records = _run(statuses_wanted=2, max_pages=1)  # only page 1 (3 items)
    status = records[-1]
    assert status.attributes["fetched_pages"] == 1
    assert status.attributes["listed"] == 3
    # Page 1 holds NOTAM_FAKE_1/2 (features) + NOTAM_FAKE_3 (event).
    assert len(_features(records)) == 2
    assert len(_events(records)) == 1


def test_records_are_deduped_across_polls() -> None:
    records = _run(statuses_wanted=3)  # two completed polls
    # All records come from the first poll; the second re-emits none (same revision token).
    assert len(_features(records)) == 2
    assert len(_events(records)) == 2


def test_fetch_failure_degrades_without_crashing() -> None:
    class _Failing:
        name = "boom"

        async def fetch_page(self, page_num: int) -> dict[str, Any]:
            raise RuntimeError("network down")

    agen = notam_records(_Failing(), poll_s=0.0, max_pages_per_poll=5)
    records = asyncio.run(_drive(agen, statuses_wanted=2))
    assert _features(records) == [] and _events(records) == []
    degraded = records[-1]
    assert isinstance(degraded, SourceStatusRecord)
    assert degraded.status == "degraded"
    assert degraded.error_code == "RuntimeError"


def test_unauthorized_emits_offline_and_stops() -> None:
    class _Auth:
        name = "auth"

        async def fetch_page(self, page_num: int) -> dict[str, Any]:
            raise NotamAuthError("NOTAM API returned HTTP 403")

    agen = notam_records(_Auth(), poll_s=0.0, max_pages_per_poll=5)
    # The stream must END after the offline status — bad creds will not self-heal.
    records = asyncio.run(_collect_all(agen))
    statuses = [r for r in records if isinstance(r, SourceStatusRecord)]
    assert [s.status for s in statuses] == ["starting", "offline"]
    assert statuses[-1].error_code == "Unauthorized"


async def _collect_all(agen: Any, *, limit: int = 50) -> list:
    """Drain a (terminating) async generator fully, with a runaway guard."""
    records: list = []
    async for record in agen:
        records.append(record)
        if len(records) >= limit:  # pragma: no cover - guards against a non-terminating stream
            raise AssertionError("stream did not terminate")
    return records


# --- Capability gate / provider selection --------------------------------------


def test_build_provider_selects_fake_feeder_by_base() -> None:
    cfg = dataclasses.replace(Settings(), faa_notam_base_url="fake")
    assert isinstance(build_provider(cfg), FakeFaaNotamProvider)


def test_build_provider_selects_fake_feeder_by_credential() -> None:
    cfg = dataclasses.replace(Settings(), faa_notam_client_id="fake", faa_notam_client_secret="x")
    assert isinstance(build_provider(cfg), FakeFaaNotamProvider)


def test_build_provider_requires_credentials_for_live_api() -> None:
    cfg = dataclasses.replace(Settings(), faa_notam_client_id="", faa_notam_client_secret="")
    try:
        build_provider(cfg)
    except ValueError as exc:
        assert "credential" in str(exc).lower()
    else:  # pragma: no cover
        raise AssertionError("build_provider should require credentials for the live API")


def test_build_provider_builds_http_provider_with_credentials() -> None:
    cfg = dataclasses.replace(
        Settings(), faa_notam_client_id="CID", faa_notam_client_secret="SECRET"
    )
    prov = build_provider(cfg)
    assert isinstance(prov, FaaNotamHttpProvider)
    assert prov.effective_radius_nm == 100.0

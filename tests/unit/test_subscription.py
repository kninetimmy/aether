"""Unit tests for the per-connection display filter (PRD §16.3, §22.2).

FastAPI-free: exercises ``parse_subscribe`` validation, the ``ClientFilter.matches``
truth table, and the station-AOI clamp/intersection without a socket or the Hub.
"""

import dataclasses
from datetime import UTC, datetime

from aether.backend.subscription import (
    ClientFilter,
    default_filter,
    parse_subscribe,
    station_bbox,
)
from aether.config import Settings
from aether.schema.geometry import LineString, Point, Polygon
from aether.schema.records import GeoFeatureRecord, SourceStatusRecord, TrackRecord
from aether.state.live import StateChange

NOW = datetime(2026, 6, 15, tzinfo=UTC)


def _settings(**over: float) -> Settings:
    base = {"station_lat": 0.0, "station_lon": 0.0, "station_radius_nm": 500.0}
    base.update(over)
    return dataclasses.replace(Settings(), **base)  # type: ignore[arg-type]


def _sub(settings: Settings | None = None, **frame: object) -> ClientFilter | None:
    """parse_subscribe a frame (type defaulted), against (default) settings."""
    frame.setdefault("type", "subscribe")
    return parse_subscribe(frame, settings if settings is not None else _settings())


def _track(
    *,
    source: str = "demo",
    track_type: str = "aircraft",
    coords: list[float] | None = None,
) -> TrackRecord:
    geom = Point(coordinates=coords) if coords is not None else None
    return TrackRecord(
        id="t1",
        source=source,
        observed_at=NOW,
        received_at=NOW,
        published_at=NOW,
        track_type=track_type,  # type: ignore[arg-type]
        geometry=geom,
        locally_received=False,
    )


def _feature(geometry: object, *, source: str = "faa_tfr") -> GeoFeatureRecord:
    return GeoFeatureRecord(
        id="f1",
        source=source,
        observed_at=NOW,
        received_at=NOW,
        published_at=NOW,
        feature_type="tfr",
        geometry=geometry,  # type: ignore[arg-type]
    )


def _status() -> SourceStatusRecord:
    return SourceStatusRecord(
        id="status:demo",
        source="demo",
        observed_at=NOW,
        received_at=NOW,
        published_at=NOW,
        status="connected",
    )


def _change(kind: str, record: object, op: str = "upsert") -> StateChange:
    rid = getattr(record, "id", "x") if record is not None else "x"
    return StateChange(seq=1, op=op, kind=kind, id=rid, record=record)  # type: ignore[arg-type]


# --- parse_subscribe validation -------------------------------------------


def test_parse_good_bbox_unconfigured_station() -> None:
    f = parse_subscribe({"type": "subscribe", "bbox": [-80.0, 35.0, -74.0, 42.0]}, _settings())
    assert f is not None
    # No station AOI to clamp against → the requested box passes through verbatim.
    assert f.bbox == (-80.0, 35.0, -74.0, 42.0)


def test_parse_null_bbox_is_unbounded_when_station_unset() -> None:
    f = parse_subscribe({"type": "subscribe", "bbox": None}, _settings())
    assert f is not None and f.bbox is None


def test_parse_rejects_minlat_gt_maxlat() -> None:
    assert _sub(bbox=[-80.0, 42.0, -74.0, 35.0]) is None


def test_parse_allows_antimeridian_minlon_gt_maxlon() -> None:
    f = _sub(bbox=[170.0, -10.0, -170.0, 10.0])
    assert f is not None and f.bbox == (170.0, -10.0, -170.0, 10.0)


def test_parse_rejects_out_of_wgs84() -> None:
    assert _sub(bbox=[-80.0, 35.0, -74.0, 95.0]) is None
    assert _sub(bbox=[-200.0, 35.0, -74.0, 42.0]) is None


def test_parse_rejects_non_finite() -> None:
    assert _sub(bbox=[float("nan"), 35.0, -74.0, 42.0]) is None
    assert _sub(bbox=[float("inf"), 35.0, -74.0, 42.0]) is None


def test_parse_rejects_wrong_arity_and_non_subscribe() -> None:
    assert parse_subscribe({"type": "subscribe", "bbox": [1.0, 2.0, 3.0]}, _settings()) is None
    assert parse_subscribe({"type": "hello"}, _settings()) is None
    assert parse_subscribe("not a dict", _settings()) is None


def test_parse_rejects_bad_sources_keeps_prior() -> None:
    # A non-string element invalidates the whole frame (caller keeps prior filter).
    assert parse_subscribe({"type": "subscribe", "sources": [1, 2]}, _settings()) is None


def test_parse_sources_and_track_types_sets() -> None:
    f = parse_subscribe(
        {"type": "subscribe", "sources": ["local_adsb"], "track_types": ["aircraft", "vessel"]},
        _settings(),
    )
    assert f is not None
    assert f.sources == frozenset({"local_adsb"})
    assert f.track_types == frozenset({"aircraft", "vessel"})


def test_parse_empty_list_is_a_constraint_matching_nothing() -> None:
    f = parse_subscribe({"type": "subscribe", "sources": []}, _settings())
    assert f is not None and f.sources == frozenset()


def test_bbox_intersected_with_station_aoi() -> None:
    # Station at (-95, 40.5) r=60NM → ~1deg box. A wide request clamps to it.
    s = _settings(station_lat=40.5, station_lon=-95.0, station_radius_nm=60.0)
    station = station_bbox(s)
    assert station is not None
    f = parse_subscribe({"type": "subscribe", "bbox": [-180.0, -80.0, 180.0, 80.0]}, s)
    assert f is not None and f.bbox == station  # narrowed to the station cap


def test_include_flags_default_true_and_parse() -> None:
    f = parse_subscribe(
        {"type": "subscribe", "include_events": False, "include_alerts": False}, _settings()
    )
    assert f is not None and not f.include_events and not f.include_alerts
    g = parse_subscribe({"type": "subscribe"}, _settings())
    assert g is not None and g.include_events and g.include_alerts


# --- station_bbox / default_filter -----------------------------------------


def test_station_bbox_unbounded_at_null_island() -> None:
    assert station_bbox(_settings()) is None
    assert default_filter(_settings()).bbox is None


def test_station_bbox_box_when_configured() -> None:
    box = station_bbox(_settings(station_lat=40.0, station_lon=-95.0, station_radius_nm=60.0))
    assert box is not None
    min_lon, min_lat, max_lon, max_lat = box
    assert min_lat < 40.0 < max_lat
    assert min_lon < -95.0 < max_lon


# --- matches truth table ---------------------------------------------------


def test_source_status_always_passes() -> None:
    f = ClientFilter(bbox=(-1.0, -1.0, 1.0, 1.0), sources=frozenset({"other"}))
    assert f.matches(_change("source_status", _status())) is True


def test_alerts_gated_by_include_flag() -> None:
    on = ClientFilter(include_alerts=True)
    off = ClientFilter(include_alerts=False)
    assert on.matches_record("alert", object()) is True
    assert off.matches_record("alert", object()) is False


def test_events_gated_by_include_and_bbox() -> None:
    f = ClientFilter(bbox=(-1.0, -1.0, 1.0, 1.0), include_events=True)
    inside = _feature(Point(coordinates=[0.0, 0.0]))  # reuse a geometry holder
    outside = _feature(Point(coordinates=[50.0, 50.0]))
    assert f.matches_record("event", inside) is True
    assert f.matches_record("event", outside) is False
    # No geometry → passes (event without a location is not hidden).
    assert f.matches_record("event", _track()) is True
    assert ClientFilter(include_events=False).matches_record("event", inside) is False


def test_track_point_in_bbox() -> None:
    f = ClientFilter(bbox=(-95.0, 40.6, -94.5, 41.2))
    assert f.matches(_change("track", _track(coords=[-94.6, 41.1]))) is True
    assert f.matches(_change("track", _track(coords=[-95.4, 40.5]))) is False


def test_track_geometry_none_passes() -> None:
    f = ClientFilter(bbox=(-95.0, 40.6, -94.5, 41.2))
    assert f.matches(_change("track", _track(coords=None))) is True


def test_track_source_and_type_gating() -> None:
    f = ClientFilter(sources=frozenset({"local_adsb"}), track_types=frozenset({"aircraft"}))
    ok = _change("track", _track(source="local_adsb", track_type="aircraft"))
    wrong_source = _change("track", _track(source="network_adsb", track_type="aircraft"))
    wrong_type = _change("track", _track(source="local_adsb", track_type="vessel"))
    assert f.matches(ok) is True
    assert f.matches(wrong_source) is False
    assert f.matches(wrong_type) is False


def test_feature_polygon_intersects_box() -> None:
    f = ClientFilter(bbox=(-1.0, -1.0, 1.0, 1.0))
    # A polygon with a vertex straddling into the box passes.
    poly = Polygon(coordinates=[[[0.5, 0.5], [2.0, 0.5], [2.0, 2.0], [0.5, 2.0], [0.5, 0.5]]])
    assert f.matches(_change("feature", _feature(poly))) is True
    far = Polygon(coordinates=[[[10.0, 10.0], [11.0, 10.0], [11.0, 11.0], [10.0, 10.0]]])
    assert f.matches(_change("feature", _feature(far))) is False


def test_feature_linestring_intersects_box() -> None:
    f = ClientFilter(bbox=(-1.0, -1.0, 1.0, 1.0))
    line = LineString(coordinates=[[0.0, 0.0], [5.0, 5.0]])
    assert f.matches(_change("feature", _feature(line))) is True


def test_feature_straddling_edge_with_vertex_in_box() -> None:
    # An edge crossing the box boundary with an endpoint inside → passes.
    f = ClientFilter(bbox=(0.0, 0.0, 2.0, 2.0))
    line = LineString(coordinates=[[-5.0, 1.0], [1.0, 1.0]])
    assert f.matches(_change("feature", _feature(line))) is True


def test_antimeridian_bbox_point_membership() -> None:
    # Box wraps 170E..-170E; a point at 175 and at -175 are both inside, 0 is out.
    f = ClientFilter(bbox=(170.0, -10.0, -170.0, 10.0))
    assert f.matches(_change("track", _track(coords=[175.0, 0.0]))) is True
    assert f.matches(_change("track", _track(coords=[-175.0, 0.0]))) is True
    assert f.matches(_change("track", _track(coords=[0.0, 0.0]))) is False


def test_remove_passes_matches_decided_by_hub() -> None:
    # A remove has no record; matches() returns True (the Hub's sent_ids decides).
    f = ClientFilter(bbox=(-1.0, -1.0, 1.0, 1.0))
    assert f.matches(_change("track", None, op="remove")) is True


def test_unbounded_filter_is_a_noop() -> None:
    f = ClientFilter()
    assert f.matches(_change("track", _track(coords=[123.0, 45.0]))) is True
    assert f.matches(_change("feature", _feature(Point(coordinates=[1.0, 2.0])))) is True

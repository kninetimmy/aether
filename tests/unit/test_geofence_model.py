"""Geofence model, circle→polygon projection, and feature mapping (M4.4)."""

import math
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from aether.schema.geofence import (
    CircleShape,
    Geofence,
    GeofenceCreate,
    GeofenceUpdate,
    PolygonShape,
)
from aether.schema.geometry import circle_polygon

T0 = datetime(2026, 6, 19, 12, 0, 0, tzinfo=UTC)
T1 = datetime(2026, 6, 19, 13, 0, 0, tzinfo=UTC)
_EARTH_RADIUS_M = 6_371_008.8


def _haversine_m(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * _EARTH_RADIUS_M * math.asin(math.sqrt(a))


def _circle(center=(-95.0, 40.0), radius_m=5000.0) -> Geofence:
    return Geofence.create(
        GeofenceCreate(name="ring", shape=CircleShape(center=list(center), radius_m=radius_m)),
        id="gf-1",
        now=T0,
    )


def test_circle_polygon_vertices_lie_on_the_radius() -> None:
    poly = circle_polygon(-95.0, 40.0, 5000.0, vertices=64)
    ring = poly.coordinates[0]
    assert len(ring) == 65  # 64 vertices + closing repeat
    assert ring[0] == ring[-1]  # closed ring (RFC 7946)
    for lon, lat in ring[:-1]:
        assert abs(_haversine_m(-95.0, 40.0, lon, lat) - 5000.0) < 1.0


def test_circle_to_feature_record_keeps_authoritative_shape() -> None:
    feature = _circle(radius_m=3000.0).to_feature_record()
    assert feature.kind == "feature"
    assert feature.feature_type == "geofence"
    assert feature.id == "gf-1"
    assert feature.source == "geofence"
    assert feature.label == "ring"
    assert feature.valid_until is None  # never expired by the live-state sweep
    assert feature.geometry.type == "Polygon"
    gf_attr = feature.attributes["geofence"]
    assert gf_attr["shape"] == {"type": "circle", "center": [-95.0, 40.0], "radius_m": 3000.0}
    assert gf_attr["enabled"] is True


def test_polygon_to_feature_record_passes_geometry_through() -> None:
    ring = [[-96.0, 40.0], [-94.0, 40.0], [-94.0, 41.0], [-96.0, 41.0], [-96.0, 40.0]]
    gf = Geofence.create(
        GeofenceCreate(name="box", shape=PolygonShape(polygon={"coordinates": [ring]})),
        id="gf-2",
        now=T0,
    )
    feature = gf.to_feature_record()
    assert feature.geometry.type == "Polygon"
    assert feature.geometry.coordinates == [ring]
    assert feature.attributes["geofence"]["shape"] == {"type": "polygon"}


def test_radius_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        CircleShape(center=[-95.0, 40.0], radius_m=0.0)


def test_altitude_band_must_be_ordered() -> None:
    with pytest.raises(ValidationError):
        GeofenceCreate(
            name="bad",
            shape=CircleShape(center=[-95.0, 40.0], radius_m=1000.0),
            min_altitude_m=2000.0,
            max_altitude_m=1000.0,
        )


def test_with_update_changes_only_set_fields_and_bumps_updated_at() -> None:
    gf = _circle()
    patched = gf.with_update(GeofenceUpdate(name="renamed", enabled=False), now=T1)
    assert patched.name == "renamed"
    assert patched.enabled is False
    assert patched.created_at == T0  # preserved
    assert patched.updated_at == T1  # bumped
    assert patched.shape == gf.shape  # untouched
    assert patched.id == gf.id


def test_with_update_can_replace_the_shape() -> None:
    gf = _circle()
    ring = [[-96.0, 40.0], [-94.0, 40.0], [-94.0, 41.0], [-96.0, 40.0]]
    patched = gf.with_update(
        GeofenceUpdate(shape=PolygonShape(polygon={"coordinates": [ring]})), now=T1
    )
    assert isinstance(patched.shape, PolygonShape)
    assert patched.shape.polygon.coordinates == [ring]

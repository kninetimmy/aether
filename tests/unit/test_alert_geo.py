"""Pure geometry predicates for contextual alert operators (M4.6c).

Exercises :mod:`aether.alerts.geo` directly: total functions over floats and the
geometry/geofence types, no engine, no state. The haversine reference mirrors the
``test_geofence_model.py`` style so the alert-side distance math is pinned to the
same formula the circle projection inverts.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime

from aether.alerts import geo
from aether.schema.geofence import CircleShape, Geofence, GeofenceCreate, PolygonShape
from aether.schema.geometry import Polygon

T0 = datetime(2026, 6, 19, 12, 0, 0, tzinfo=UTC)
_EARTH_RADIUS_M = 6_371_008.8


def _ref_haversine_m(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * _EARTH_RADIUS_M * math.asin(math.sqrt(a))


def _box(*corners: tuple[float, float]) -> Polygon:
    return Polygon(coordinates=[[[c[0], c[1]] for c in corners]])


# --- haversine ----------------------------------------------------------------


def test_haversine_matches_reference_formula() -> None:
    d = geo.haversine_m(-95.0, 40.0, -95.0, 40.5)
    assert abs(d - _ref_haversine_m(-95.0, 40.0, -95.0, 40.5)) < 1.0


# --- circle containment -------------------------------------------------------


def test_point_in_circle_inside_on_radius_and_outside() -> None:
    center: list[float] = [-95.0, 40.0]
    # A point ~half the radius north is well inside.
    assert geo.point_in_circle(-95.0, 40.0, center, 5000.0)
    # Construct a point exactly on the radius (north) and confirm inclusive.
    north = 40.0 + math.degrees(5000.0 / _EARTH_RADIUS_M)
    assert geo.point_in_circle(-95.0, north, center, 5000.0 + 1.0)  # inside (+1 m slack)
    # Clearly outside.
    assert not geo.point_in_circle(-94.0, 40.0, center, 5000.0)


# --- polygon containment ------------------------------------------------------


def test_point_in_polygon_box_inside_outside() -> None:
    box = _box((-96.0, 40.0), (-94.0, 40.0), (-94.0, 41.0), (-96.0, 41.0), (-96.0, 40.0))
    assert geo.point_in_polygon(-95.0, 40.5, box)
    assert not geo.point_in_polygon(-93.0, 40.5, box)


def test_point_in_polygon_with_hole() -> None:
    exterior = [[-96.0, 40.0], [-94.0, 40.0], [-94.0, 42.0], [-96.0, 42.0], [-96.0, 40.0]]
    hole = [[-95.5, 40.5], [-94.5, 40.5], [-94.5, 41.5], [-95.5, 41.5], [-95.5, 40.5]]
    poly = Polygon(coordinates=[exterior, hole])
    assert geo.point_in_polygon(-95.7, 40.2, poly)  # inside exterior, outside hole
    assert not geo.point_in_polygon(-95.0, 41.0, poly)  # inside the hole → outside


def test_point_in_polygon_antimeridian_box() -> None:
    # A box straddling +-180 with vertices written near both signs.
    box = _box((179.0, 0.0), (-179.0, 0.0), (-179.0, 2.0), (179.0, 2.0), (179.0, 0.0))
    assert geo.point_in_polygon(180.0, 1.0, box)
    assert geo.point_in_polygon(-179.5, 1.0, box)
    assert not geo.point_in_polygon(170.0, 1.0, box)
    # A point far from the seam must read OUTSIDE: the ray-cast unwrap is anchored on
    # the ring (not the test point), so a seam-straddling fence stays a narrow band
    # rather than inflating to most of the globe (regression for the point-anchored bug).
    assert not geo.point_in_polygon(0.0, 1.0, box)
    assert not geo.point_in_polygon(-90.0, 1.0, box)


def test_point_on_polygon_edge_is_inside() -> None:
    box = _box((-96.0, 40.0), (-94.0, 40.0), (-94.0, 41.0), (-96.0, 41.0), (-96.0, 40.0))
    assert geo.point_in_polygon(-95.0, 40.0, box)  # on the bottom edge
    assert geo.point_in_polygon(-96.0, 40.5, box)  # on the left edge


def test_empty_polygon_is_outside() -> None:
    assert not geo.point_in_polygon(0.0, 0.0, Polygon(coordinates=[]))


# --- areal intersection (geofence_intersects) ---------------------------------


def _ring(*corners: tuple[float, float]) -> list[list[float]]:
    return [[c[0], c[1]] for c in corners]


def test_rings_intersect_overlapping_boxes() -> None:
    a = _ring((-96.0, 40.0), (-94.0, 40.0), (-94.0, 42.0), (-96.0, 42.0))
    b = _ring((-95.0, 41.0), (-93.0, 41.0), (-93.0, 43.0), (-95.0, 43.0))  # overlaps NE corner
    assert geo.rings_intersect(a, b)
    assert geo.rings_intersect(b, a)  # symmetric


def test_rings_intersect_disjoint_boxes() -> None:
    a = _ring((-96.0, 40.0), (-95.0, 40.0), (-95.0, 41.0), (-96.0, 41.0))
    b = _ring((-90.0, 40.0), (-89.0, 40.0), (-89.0, 41.0), (-90.0, 41.0))  # far east
    assert not geo.rings_intersect(a, b)


def test_rings_intersect_one_fully_inside_other() -> None:
    outer = _ring((-96.0, 40.0), (-94.0, 40.0), (-94.0, 42.0), (-96.0, 42.0))
    inner = _ring((-95.5, 40.5), (-94.5, 40.5), (-94.5, 41.5), (-95.5, 41.5))  # no edge crossing
    assert geo.rings_intersect(outer, inner)  # inner vertex inside outer
    assert geo.rings_intersect(inner, outer)  # outer "contains" inner — symmetric


def test_rings_intersect_degenerate_ring_is_false() -> None:
    a = _ring((-96.0, 40.0), (-94.0, 40.0), (-94.0, 42.0), (-96.0, 42.0))
    assert not geo.rings_intersect(a, [[-95.0, 41.0], [-94.0, 41.0]])  # < 3 vertices


def test_ring_intersects_circle_center_inside_ring() -> None:
    # A big polygon enclosing the circle's center → overlap even though no vertex is near.
    ring = _ring((-96.0, 39.0), (-94.0, 39.0), (-94.0, 41.0), (-96.0, 41.0))
    assert geo.ring_intersects_circle(ring, [-95.0, 40.0], 1000.0)


def test_ring_intersects_circle_edge_within_radius() -> None:
    # Circle center just outside the box, but within radius of the nearest edge.
    ring = _ring((-95.0, 40.0), (-94.0, 40.0), (-94.0, 41.0), (-95.0, 41.0))
    near = [-95.0 - math.degrees(3000.0 / _EARTH_RADIUS_M), 40.5]  # ~3 km west of left edge
    assert geo.ring_intersects_circle(ring, near, 5000.0)  # edge within 5 km
    assert not geo.ring_intersects_circle(ring, near, 1000.0)  # edge beyond 1 km


def test_geofence_intersects_rings_circle_and_polygon_shapes() -> None:
    tfr_ring = _ring((-95.2, 39.9), (-94.8, 39.9), (-94.8, 40.1), (-95.2, 40.1))  # around station

    circle = Geofence.create(
        GeofenceCreate(name="c", shape=CircleShape(center=[-95.0, 40.0], radius_m=5000.0)),
        id="gf-c",
        now=T0,
    )
    assert geo.geofence_intersects_rings(circle, [tfr_ring])

    poly = Geofence.create(
        GeofenceCreate(
            name="p",
            shape=PolygonShape(
                polygon={"coordinates": [_ring((-95.1, 40.0), (-94.0, 40.0), (-94.0, 41.0))]}
            ),
        ),
        id="gf-p",
        now=T0,
    )
    assert geo.geofence_intersects_rings(poly, [tfr_ring])  # overlaps the TFR's east side

    far = Geofence.create(
        GeofenceCreate(name="f", shape=CircleShape(center=[-80.0, 40.0], radius_m=5000.0)),
        id="gf-f",
        now=T0,
    )
    assert not geo.geofence_intersects_rings(far, [tfr_ring])


# --- altitude band ------------------------------------------------------------


def test_in_altitude_band_unbounded_is_true_even_when_alt_none() -> None:
    assert geo.in_altitude_band(None, None, None)
    assert geo.in_altitude_band(1000.0, None, None)


def test_in_altitude_band_single_sided() -> None:
    assert geo.in_altitude_band(2000.0, 1000.0, None)  # min-only, above min
    assert not geo.in_altitude_band(500.0, 1000.0, None)
    assert geo.in_altitude_band(500.0, None, 1000.0)  # max-only, below max
    assert not geo.in_altitude_band(2000.0, None, 1000.0)


def test_in_altitude_band_inclusive_bounds() -> None:
    assert geo.in_altitude_band(1000.0, 1000.0, 2000.0)
    assert geo.in_altitude_band(2000.0, 1000.0, 2000.0)


def test_in_altitude_band_missing_alt_with_bound_is_false() -> None:
    assert not geo.in_altitude_band(None, 1000.0, None)
    assert not geo.in_altitude_band(None, None, 2000.0)


# --- elevation ----------------------------------------------------------------


def test_elevation_angle_horizon_overhead_and_below() -> None:
    assert abs(geo.elevation_angle_deg(100000.0, 0.0)) < 0.1  # far + low → ~horizon
    assert abs(geo.elevation_angle_deg(0.0, 5000.0) - 90.0) < 1e-9  # overhead
    assert abs(geo.elevation_angle_deg(1000.0, 1000.0) - 45.0) < 1e-9  # ground == alt
    assert geo.elevation_angle_deg(1000.0, -500.0) < 0.0  # below observer


# --- geofence containment + reference point -----------------------------------


def _circle_gf() -> Geofence:
    return Geofence.create(
        GeofenceCreate(name="c", shape=CircleShape(center=[-95.0, 40.0], radius_m=5000.0)),
        id="gf-c",
        now=T0,
    )


def test_geofence_contains_circle_with_altitude_band() -> None:
    gf = Geofence.create(
        GeofenceCreate(
            name="c",
            shape=CircleShape(center=[-95.0, 40.0], radius_m=5000.0),
            min_altitude_m=1000.0,
            max_altitude_m=3000.0,
        ),
        id="gf-band",
        now=T0,
    )
    assert geo.geofence_contains(gf, -95.0, 40.0, 2000.0)  # inside + in band
    assert not geo.geofence_contains(gf, -95.0, 40.0, 5000.0)  # inside but above band
    assert not geo.geofence_contains(gf, -95.0, 40.0, None)  # band set + no alt → out


def test_geofence_reference_point_circle_is_center() -> None:
    assert geo.geofence_reference_point(_circle_gf()) == [-95.0, 40.0]


def test_geofence_reference_point_polygon_is_vertex_mean() -> None:
    ring = [[-96.0, 40.0], [-94.0, 40.0], [-94.0, 42.0], [-96.0, 42.0]]
    gf = Geofence.create(
        GeofenceCreate(name="p", shape=PolygonShape(polygon={"coordinates": [ring]})),
        id="gf-p",
        now=T0,
    )
    ref = geo.geofence_reference_point(gf)
    assert abs(ref[0] - (-95.0)) < 1e-9
    assert abs(ref[1] - 41.0) < 1e-9


def test_geofence_reference_point_polygon_antimeridian() -> None:
    # A fence straddling +-180: a plain arithmetic longitude mean collapses to ~0 (the
    # antipode); the circular mean keeps the reference near +-180 where the fence is.
    ring = [[179.0, 0.0], [-179.0, 0.0], [-179.0, 2.0], [179.0, 2.0]]
    gf = Geofence.create(
        GeofenceCreate(name="p", shape=PolygonShape(polygon={"coordinates": [ring]})),
        id="gf-am",
        now=T0,
    )
    ref = geo.geofence_reference_point(gf)
    assert abs(abs(ref[0]) - 180.0) < 1e-6  # near +-180, not ~0
    assert abs(ref[1] - 1.0) < 1e-9

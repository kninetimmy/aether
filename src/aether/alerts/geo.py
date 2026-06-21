"""Geometry predicates for contextual alert operators (PRD §12 #6/#7, §20.2).

The first behavioural consumers of geofence/station geometry. Pure, total
functions over plain floats and the geometry *types* (no record dumps, no I/O), so
the evaluator can call them on the hot path and mypy-strict sees no ``Any`` leak.
Spherical-Earth math on the WGS84 mean radius (``_EARTH_RADIUS_M`` from
:mod:`aether.schema.geometry`) — adequate at home-station scale (<=500 NM AOI), the
same approximation the circle-display projection already accepts. None of these
raises; an out-of-domain input returns the documented safe default so the evaluator
never guards a ``ValueError`` mid-AND. Circle containment uses the AUTHORITATIVE
center+radius, never the 64-gon display polygon.
"""

from __future__ import annotations

import math

from aether.schema.geofence import CircleShape, Geofence
from aether.schema.geometry import _EARTH_RADIUS_M, Polygon, Position


def haversine_m(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    """Great-circle distance between two ``[lon, lat]`` points, in metres.

    Standard haversine on the WGS84 mean sphere — the same formula the geofence
    model's circle projection inverts, so a ``point_in_circle`` test is consistent
    with the displayed ring at geofence scale.
    """
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2.0) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlmb / 2.0) ** 2
    return 2.0 * _EARTH_RADIUS_M * math.asin(math.sqrt(a))


def _unwrap_lon(lon: float, anchor: float) -> float:
    """Shift ``lon`` to within 180 deg of ``anchor`` (antimeridian-safe longitude frame).

    Anchoring lets an edge spanning +-180 (or a fence written with >180 longitudes) be
    tested against a contiguous longitude axis rather than wrapping the wrong way. Shared
    by the ray cast and the areal intersection predicates so both put two rings in one
    frame the same way.
    """
    delta = lon - anchor
    if delta > 180.0:
        return lon - 360.0
    if delta < -180.0:
        return lon + 360.0
    return lon


def point_in_ring(lon: float, lat: float, ring: list[Position]) -> bool:
    """Even-odd ray cast for ONE ring, antimeridian-aware, boundary = inside.

    Longitudes are unwrapped into the ring's OWN frame — anchored on its first vertex,
    add/subtract 360 — so a home-station fence straddling +-180 (or written with >180
    longitudes) stays a contiguous narrow band, then the test point is shifted into the
    same frame before the cast. Anchoring on the ring rather than the test point is
    deliberate: anchoring on the point would inflate a seam-straddling ring to nearly
    the whole globe and report far-away points inside. The ring need not be explicitly
    closed (last->first edge always tested). A point exactly on an edge counts as
    inside (deterministic; avoids flapping for a track parked on a border). Uses only
    the first two ordinates of each [lon,lat,(alt)] position.
    """
    n = len(ring)
    if n < 3:
        return False

    anchor = ring[0][0]

    def _unwrap(vlon: float) -> float:
        return _unwrap_lon(vlon, anchor)

    x = _unwrap(lon)  # the test point, into the ring's frame
    inside = False
    j = n - 1
    xj, yj = _unwrap(ring[j][0]), ring[j][1]
    for i in range(n):
        xi, yi = _unwrap(ring[i][0]), ring[i][1]
        # On-edge test: collinear with the [i, j] segment and within its bounds → inside.
        if _on_segment(x, lat, xi, yi, xj, yj):
            return True
        # Standard even-odd crossing of the horizontal ray to +inf in longitude.
        if (yi > lat) != (yj > lat):
            x_cross = xi + (lat - yi) / (yj - yi) * (xj - xi)
            if x < x_cross:
                inside = not inside
        xj, yj = xi, yi
    return inside


def _on_segment(px: float, py: float, ax: float, ay: float, bx: float, by: float) -> bool:
    """Whether ``(px, py)`` lies on the segment ``(ax, ay)``–``(bx, by)`` (planar).

    A small epsilon on the cross-product tolerates float rounding so a vertex/edge
    point reads as on-boundary; the bounding-box guard keeps it to the segment, not
    the infinite line. Operates in the already-unwrapped longitude frame.
    """
    cross = (bx - ax) * (py - ay) - (by - ay) * (px - ax)
    if abs(cross) > 1e-12:
        return False
    if min(ax, bx) - 1e-12 <= px <= max(ax, bx) + 1e-12:
        if min(ay, by) - 1e-12 <= py <= max(ay, by) + 1e-12:
            return True
    return False


def point_in_polygon(lon: float, lat: float, polygon: Polygon) -> bool:
    """RFC-7946 containment: inside exterior ring (coordinates[0]) AND outside every
    hole (coordinates[1:]). Empty coordinates -> False."""
    rings = polygon.coordinates
    if not rings:
        return False
    if not point_in_ring(lon, lat, rings[0]):
        return False
    for hole in rings[1:]:
        if point_in_ring(lon, lat, hole):
            return False
    return True


def point_in_circle(lon: float, lat: float, center: Position, radius_m: float) -> bool:
    """True haversine containment: haversine_m(point, center) <= radius_m (inclusive)."""
    return haversine_m(lon, lat, center[0], center[1]) <= radius_m


# --- areal intersection (a feature's polygon vs a geofence; geofence_intersects) ----
# The first AREAL operator: earlier geometry leaves reduce a record to a representative
# POINT, but a TFR is a polygon, so "does this area overlap the fence" needs ring math.
# Spherical-Earth planar approximations on the unwrapped longitude frame, adequate at
# the <=500 NM home-station AOI (the same regime as the containment predicates above).


def _orient(ax: float, ay: float, bx: float, by: float, cx: float, cy: float) -> float:
    """Signed twice-area of triangle (a, b, c): >0 if c is left of a->b, <0 right, 0 collinear."""
    return (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)


def _segments_cross(a1: Position, a2: Position, b1: Position, b2: Position) -> bool:
    """Whether segment ``a1-a2`` intersects ``b1-b2`` (planar, endpoints/touching count).

    Standard orientation test: a proper crossing flips both pairs of orientations; the
    four collinear cases (an endpoint lying on the other segment) are caught via the
    bounding-box :func:`_on_segment` check so a TFR edge merely grazing the fence still
    reads as intersecting. Operates in whatever longitude frame the caller unwraps into.
    """
    ax, ay, bx, by = a1[0], a1[1], a2[0], a2[1]
    cx, cy, dx, dy = b1[0], b1[1], b2[0], b2[1]
    d1 = _orient(cx, cy, dx, dy, ax, ay)
    d2 = _orient(cx, cy, dx, dy, bx, by)
    d3 = _orient(ax, ay, bx, by, cx, cy)
    d4 = _orient(ax, ay, bx, by, dx, dy)
    if ((d1 > 0.0) != (d2 > 0.0)) and ((d3 > 0.0) != (d4 > 0.0)):
        return True
    if d1 == 0.0 and _on_segment(ax, ay, cx, cy, dx, dy):
        return True
    if d2 == 0.0 and _on_segment(bx, by, cx, cy, dx, dy):
        return True
    if d3 == 0.0 and _on_segment(cx, cy, ax, ay, bx, by):
        return True
    if d4 == 0.0 and _on_segment(dx, dy, ax, ay, bx, by):
        return True
    return False


def rings_intersect(ring_a: list[Position], ring_b: list[Position]) -> bool:
    """Whether two closed rings overlap: any edges cross, OR one fully contains the other.

    Both rings are unwrapped into ``ring_a``'s anchor frame so a seam-straddling pair is
    compared on one contiguous axis. An edge crossing is a clear overlap; with no
    crossing the rings are either nested (one vertex of each tested for containment in
    the other via :func:`point_in_ring`) or disjoint. A ring with < 3 vertices is
    degenerate and never intersects. Holes are not considered — callers pass exterior
    rings (a coarse overlap test, matching the polygon reference-point convention)."""
    n, m = len(ring_a), len(ring_b)
    if n < 3 or m < 3:
        return False
    anchor = ring_a[0][0]
    a: list[Position] = [[_unwrap_lon(p[0], anchor), p[1]] for p in ring_a]
    b: list[Position] = [[_unwrap_lon(p[0], anchor), p[1]] for p in ring_b]
    for i in range(n):
        a1, a2 = a[i], a[(i + 1) % n]
        for j in range(m):
            if _segments_cross(a1, a2, b[j], b[(j + 1) % m]):
                return True
    # No edge crossing → nested or disjoint. point_in_ring re-anchors on its own ring,
    # so pass the ORIGINAL (not pre-unwrapped) vertices to keep its frame self-consistent.
    if point_in_ring(ring_a[0][0], ring_a[0][1], ring_b):
        return True
    return point_in_ring(ring_b[0][0], ring_b[0][1], ring_a)


def _segment_point_distance_m(clon: float, clat: float, a: Position, b: Position) -> float:
    """Shortest distance (metres) from ``(clon, clat)`` to segment ``a-b``.

    Projects to a local equirectangular frame (longitude scaled by cos(lat) at the
    point, so east-west distances aren't overstated), clamps the closest-point parameter
    to the segment, then measures the haversine to that closest point — keeping the
    returned distance in true metres while the parametrisation stays cheap and planar.
    """
    cx = clon
    ax = _unwrap_lon(a[0], clon)
    bx = _unwrap_lon(b[0], clon)
    ay, by = a[1], b[1]
    kx = math.cos(math.radians(clat))
    ux, uy = (bx - ax) * kx, by - ay
    wx, wy = (cx - ax) * kx, clat - ay
    seg2 = ux * ux + uy * uy
    t = 0.0 if seg2 == 0.0 else max(0.0, min(1.0, (wx * ux + wy * uy) / seg2))
    px, py = ax + t * (bx - ax), ay + t * (by - ay)
    return haversine_m(clon, clat, px, py)


def ring_intersects_circle(ring: list[Position], center: Position, radius_m: float) -> bool:
    """Whether a ring overlaps a circle: center inside the ring, OR an edge within radius.

    Covers every overlap case — circle inside the polygon (center contained), polygon
    inside or straddling the circle (some edge passes within ``radius_m`` of the center,
    which subsumes a vertex falling inside the circle). A degenerate ring never overlaps."""
    if len(ring) < 3:
        return False
    clon, clat = center[0], center[1]
    if point_in_ring(clon, clat, ring):
        return True
    n = len(ring)
    return any(
        _segment_point_distance_m(clon, clat, ring[i], ring[(i + 1) % n]) <= radius_m
        for i in range(n)
    )


def geofence_intersects_rings(gf: Geofence, rings: list[list[Position]]) -> bool:
    """Whether any of a feature's exterior rings overlaps the geofence's authoritative shape.

    Horizontal-only overlap (a TFR polygon vs the fence): the geofence altitude band and
    any polygon holes are NOT applied — a TFR carries its own vertical limits as text, not
    a single altitude, so areal overlap is the honest signal (the vertical refinement can
    come later). A ``CircleShape`` uses its authoritative center+radius; a ``PolygonShape``
    its exterior ring."""
    shape = gf.shape
    for ring in rings:
        if isinstance(shape, CircleShape):
            if ring_intersects_circle(ring, shape.center, shape.radius_m):
                return True
        else:
            exterior = shape.polygon.coordinates[0] if shape.polygon.coordinates else []
            if rings_intersect(ring, exterior):
                return True
    return False


def in_altitude_band(alt_m: float | None, min_m: float | None, max_m: float | None) -> bool:
    """Inclusive [min, max] membership; an unset bound is open on that side.

    Both bounds None -> True (no band constraint, even when alt_m is None). If EITHER
    bound is set and alt_m is None -> False (a missing altitude cannot be proven
    in-band; honest-conservative, NOT 'unknown' — see decision on altitude bands).
    """
    if min_m is None and max_m is None:
        return True
    if alt_m is None:
        return False
    if min_m is not None and alt_m < min_m:
        return False
    if max_m is not None and alt_m > max_m:
        return False
    return True


def geofence_contains(gf: Geofence, lon: float, lat: float, alt_m: float | None) -> bool:
    """True iff (lon,lat) is inside gf's authoritative shape AND alt_m is in its band.

    CircleShape -> point_in_circle(center, radius_m); PolygonShape -> point_in_polygon.
    Then AND in_altitude_band(alt_m, gf.min_altitude_m, gf.max_altitude_m).
    """
    shape = gf.shape
    if isinstance(shape, CircleShape):
        horizontal = point_in_circle(lon, lat, shape.center, shape.radius_m)
    else:
        horizontal = point_in_polygon(lon, lat, shape.polygon)
    return horizontal and in_altitude_band(alt_m, gf.min_altitude_m, gf.max_altitude_m)


def geofence_reference_point(gf: Geofence) -> Position:
    """The point distance_* measures to for a geofence: a circle's center, or a
    polygon's exterior-ring vertex mean (a stable, cheap 'center'; distance-to-polygon
    is documented as a coarse proximity). Used by the evaluator, which may cache it per
    synced geofence."""
    shape = gf.shape
    if isinstance(shape, CircleShape):
        return [shape.center[0], shape.center[1]]
    ring = shape.polygon.coordinates[0] if shape.polygon.coordinates else []
    if not ring:
        return [0.0, 0.0]
    n = float(len(ring))
    # Circular mean for longitude (latitude never wraps at home-station scale): a plain
    # arithmetic mean collapses to the antipode for a fence straddling +-180, so average
    # on the unit circle via atan2 of the summed sines/cosines instead.
    sin_sum = sum(math.sin(math.radians(v[0])) for v in ring)
    cos_sum = sum(math.cos(math.radians(v[0])) for v in ring)
    mean_lon = math.degrees(math.atan2(sin_sum, cos_sum))
    mean_lat = sum(v[1] for v in ring) / n
    return [mean_lon, mean_lat]


def elevation_angle_deg(
    ground_distance_m: float, altitude_m: float, *, observer_alt_m: float = 0.0
) -> float:
    """Observer->target elevation above local horizontal, degrees: atan2(h, ground).

    Flat-local tangent model, adequate at home-station range (PRD §16.2); Earth
    curvature intentionally ignored. h = altitude_m - observer_alt_m may be negative
    (target below observer -> negative angle). ground_distance_m == 0 with positive h
    -> +90. observer_alt_m defaults 0.0 (no station-altitude config exists yet)."""
    h = altitude_m - observer_alt_m
    return math.degrees(math.atan2(h, ground_distance_m))

"""GeoJSON geometry models (RFC 7946) used by schema v2 records.

Modeled directly in Pydantic rather than pulling in a GIS dependency. Coordinate
order is GeoJSON-native ``[longitude, latitude, (altitude)]`` in WGS 84 decimal
degrees (PRD §14.8). Tracks carry a single ``Point``; overlay features (TFRs,
fire detections, lightning clusters) carry the full ``GeoJSONGeometry`` union.
"""

import math
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, Field

#: Mean Earth radius (m), WGS84 sphere approximation — adequate for geofence-scale
#: circle rendering (the authoritative circle stays center+radius; this is display).
_EARTH_RADIUS_M = 6_371_008.8

#: Vertices used to approximate a geodesic circle as a display Polygon.
CIRCLE_POLYGON_VERTICES = 64


def _validate_position(value: list[float]) -> list[float]:
    """A GeoJSON position is ``[lon, lat]`` or ``[lon, lat, alt]`` within WGS 84 bounds."""
    if not 2 <= len(value) <= 3:
        raise ValueError("position must have 2 or 3 elements: [lon, lat, (alt)]")
    lon, lat = value[0], value[1]
    if not -180.0 <= lon <= 180.0:
        raise ValueError(f"longitude {lon} out of range [-180, 180]")
    if not -90.0 <= lat <= 90.0:
        raise ValueError(f"latitude {lat} out of range [-90, 90]")
    return value


#: A single GeoJSON position, validated for arity and WGS 84 bounds. Nested uses
#: (rings, multi-geometries) are validated element-by-element by Pydantic.
Position = Annotated[list[float], AfterValidator(_validate_position)]


class Point(BaseModel):
    type: Literal["Point"] = "Point"
    coordinates: Position


class LineString(BaseModel):
    type: Literal["LineString"] = "LineString"
    coordinates: list[Position]


class Polygon(BaseModel):
    type: Literal["Polygon"] = "Polygon"
    coordinates: list[list[Position]]  # array of linear rings; first is the exterior ring


class MultiPoint(BaseModel):
    type: Literal["MultiPoint"] = "MultiPoint"
    coordinates: list[Position]


class MultiLineString(BaseModel):
    type: Literal["MultiLineString"] = "MultiLineString"
    coordinates: list[list[Position]]


class MultiPolygon(BaseModel):
    type: Literal["MultiPolygon"] = "MultiPolygon"
    coordinates: list[list[list[Position]]]


#: Alias matching the PRD field name on point-only records (e.g. ``TrackRecord``).
GeoJSONPoint = Point

#: Any GeoJSON geometry, discriminated on its ``type`` tag (PRD §14.4–14.5).
GeoJSONGeometry = Annotated[
    Point | LineString | Polygon | MultiPoint | MultiLineString | MultiPolygon,
    Field(discriminator="type"),
]


def circle_polygon(
    center_lon: float,
    center_lat: float,
    radius_m: float,
    *,
    vertices: int = CIRCLE_POLYGON_VERTICES,
) -> Polygon:
    """Approximate a geodesic circle as a closed GeoJSON ``Polygon`` for display.

    GeoJSON has no circle primitive, so a circular geofence is *stored* as its
    authoritative center+radius and rendered via this approximation (RFC 7946:
    exterior ring, counter-clockwise, first vertex repeated to close). Each vertex
    is the great-circle destination ``radius_m`` from the center on an evenly-spaced
    bearing (standard spherical destination-point formula), so the ring is faithful
    at geofence scale. Longitudes are wrapped to ``[-180, 180]`` and latitudes
    clamped to ``[-90, 90]`` so a circle near the antimeridian/poles still yields
    in-range positions (each remains a valid :class:`Position`); the math is not
    meant for circles spanning a pole.
    """
    lat1 = math.radians(center_lat)
    lon1 = math.radians(center_lon)
    ang = radius_m / _EARTH_RADIUS_M  # angular radius (radians)
    sin_lat1, cos_lat1 = math.sin(lat1), math.cos(lat1)
    sin_ang, cos_ang = math.sin(ang), math.cos(ang)
    ring: list[Position] = []
    for i in range(vertices):
        bearing = 2.0 * math.pi * i / vertices
        sin_lat2 = sin_lat1 * cos_ang + cos_lat1 * sin_ang * math.cos(bearing)
        lat2 = math.asin(max(-1.0, min(1.0, sin_lat2)))
        lon2 = lon1 + math.atan2(
            math.sin(bearing) * sin_ang * cos_lat1,
            cos_ang - sin_lat1 * sin_lat2,
        )
        lon_deg = (math.degrees(lon2) + 180.0) % 360.0 - 180.0  # wrap to [-180, 180]
        lat_deg = max(-90.0, min(90.0, math.degrees(lat2)))
        ring.append([lon_deg, lat_deg])
    ring.append(ring[0])  # close the ring (RFC 7946)
    return Polygon(coordinates=[ring])

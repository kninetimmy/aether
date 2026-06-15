"""GeoJSON geometry models (RFC 7946) used by schema v2 records.

Modeled directly in Pydantic rather than pulling in a GIS dependency. Coordinate
order is GeoJSON-native ``[longitude, latitude, (altitude)]`` in WGS 84 decimal
degrees (PRD §14.8). Tracks carry a single ``Point``; overlay features (TFRs,
fire detections, lightning clusters) carry the full ``GeoJSONGeometry`` union.
"""

from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, Field


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

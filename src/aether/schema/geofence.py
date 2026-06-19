"""Operator-defined geofences (PRD §11.1 COP-FR-008, §21.5, §19.3).

A geofence is *operator configuration*, not an observed record: the operator
creates named areas (a circle about a point, or a polygon) that alert rules later
reference by id (PRD §20.1 ``geofence_id``) for enter/exit/contains conditions
(PRD §12 templates #6, #7, #15, #21, #22). It is persisted in the ``geofences``
table and CRUD-managed via ``/api/v2/geofences``.

For display it is *projected* into the live map as a :class:`GeoFeatureRecord`
with ``feature_type="geofence"`` (:func:`to_feature_record`) — the centralized
presentation registry already styles that type, so geofences render with no
backend per-source branching (the §5 decision: areas are features, not tracks).
A circle has no GeoJSON primitive, so it is stored authoritatively as
center+radius and rendered as a polygon approximation; the exact shape is kept in
the feature's ``attributes`` so later containment math uses the true circle, not
the approximation.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from aether.schema.common import UtcDatetime
from aether.schema.geometry import Polygon, Position, circle_polygon
from aether.schema.records import GeoFeatureRecord

#: ``source`` stamped on the projected geofence feature — geofences are first-party
#: operator config, distinct from any ingested feed, so they get their own source.
GEOFENCE_SOURCE = "geofence"


class CircleShape(BaseModel):
    """A circular geofence: a center point and a radius in metres."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["circle"] = "circle"
    center: Position  # [lon, lat], WGS84 (validated for bounds by Position)
    radius_m: float = Field(gt=0.0)


class PolygonShape(BaseModel):
    """A polygonal geofence: a GeoJSON ``Polygon`` (exterior ring + any holes)."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["polygon"] = "polygon"
    polygon: Polygon


#: A geofence's authoritative shape, discriminated on ``kind``.
GeofenceShape = Annotated[CircleShape | PolygonShape, Field(discriminator="kind")]


def _check_altitudes(min_altitude_m: float | None, max_altitude_m: float | None) -> None:
    if (
        min_altitude_m is not None
        and max_altitude_m is not None
        and max_altitude_m < min_altitude_m
    ):
        raise ValueError("max_altitude_m must be >= min_altitude_m")


class GeofenceCreate(BaseModel):
    """Request body for ``POST /api/v2/geofences`` — operator-supplied fields only.

    ``id``/timestamps are server-assigned; ``enabled`` defaults on. The optional
    altitude band feeds the "below altitude inside a geofence" template (PRD §12 #7)
    that the alert engine consumes later.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    shape: GeofenceShape
    enabled: bool = True
    min_altitude_m: float | None = None
    max_altitude_m: float | None = None
    description: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def _altitudes_ordered(self) -> GeofenceCreate:
        _check_altitudes(self.min_altitude_m, self.max_altitude_m)
        return self


class GeofenceUpdate(BaseModel):
    """Request body for ``PATCH /api/v2/geofences/{id}`` — every field optional.

    A field left unset (``None``/absent) keeps its stored value; the patch is applied
    field-by-field by :func:`apply_update`. ``description`` cannot be cleared via PATCH
    in this slice (``None`` means "unchanged"); that refinement can come with the UI.
    """

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=200)
    shape: GeofenceShape | None = None
    enabled: bool | None = None
    min_altitude_m: float | None = None
    max_altitude_m: float | None = None
    description: str | None = Field(default=None, max_length=2000)


class Geofence(BaseModel):
    """A stored geofence — operator config with a stable id and audit timestamps."""

    model_config = ConfigDict(extra="forbid")

    id: str
    name: str = Field(min_length=1, max_length=200)
    shape: GeofenceShape
    enabled: bool = True
    min_altitude_m: float | None = None
    max_altitude_m: float | None = None
    description: str | None = Field(default=None, max_length=2000)
    created_at: UtcDatetime
    updated_at: UtcDatetime

    @model_validator(mode="after")
    def _altitudes_ordered(self) -> Geofence:
        _check_altitudes(self.min_altitude_m, self.max_altitude_m)
        return self

    @classmethod
    def create(cls, body: GeofenceCreate, *, id: str, now: UtcDatetime) -> Geofence:
        """Build a stored geofence from a create request at time ``now``."""
        return cls(
            id=id,
            name=body.name,
            shape=body.shape,
            enabled=body.enabled,
            min_altitude_m=body.min_altitude_m,
            max_altitude_m=body.max_altitude_m,
            description=body.description,
            created_at=now,
            updated_at=now,
        )

    def with_update(self, patch: GeofenceUpdate, *, now: UtcDatetime) -> Geofence:
        """Return a copy with ``patch``'s set fields applied and ``updated_at=now``.

        ``created_at`` is preserved; only fields the patch actually sets (present and
        non-``None``) change. Changed values are taken as their model values — not
        ``model_dump(exclude_unset=True)``, which would strip a nested shape's
        defaulted ``kind`` discriminator — then the merged copy is re-validated so a
        patched altitude band stays ordered (and the shape union round-trips).
        """
        changes: dict[str, Any] = {
            name: getattr(patch, name)
            for name in patch.model_fields_set
            if getattr(patch, name) is not None
        }
        merged = self.model_copy(update={**changes, "updated_at": now})
        return Geofence.model_validate(merged.model_dump())

    def to_feature_record(self) -> GeoFeatureRecord:
        """Project this geofence to its live-map :class:`GeoFeatureRecord`.

        The geometry is the polygon the map draws (a circle becomes a polygon
        approximation); the authoritative shape and the alert-relevant config travel
        in ``attributes['geofence']`` so containment uses the true circle later.
        ``valid_until`` is ``None`` so the live-state expiry sweep never ages a
        geofence out — it persists until the operator deletes it.
        """
        if isinstance(self.shape, CircleShape):
            lon, lat = self.shape.center[0], self.shape.center[1]
            geometry = circle_polygon(lon, lat, self.shape.radius_m)
            shape_attr: dict[str, Any] = {
                "type": "circle",
                "center": list(self.shape.center),
                "radius_m": self.shape.radius_m,
            }
        else:
            geometry = self.shape.polygon
            shape_attr = {"type": "polygon"}
        return GeoFeatureRecord(
            kind="feature",
            feature_type="geofence",
            id=self.id,
            source=GEOFENCE_SOURCE,
            observed_at=self.created_at,
            received_at=self.updated_at,
            published_at=self.updated_at,
            geometry=geometry,
            valid_from=self.created_at,
            valid_until=None,
            label=self.name,
            tags=["geofence"],
            attributes={
                "geofence": {
                    "shape": shape_attr,
                    "enabled": self.enabled,
                    "min_altitude_m": self.min_altitude_m,
                    "max_altitude_m": self.max_altitude_m,
                    "description": self.description,
                }
            },
        )

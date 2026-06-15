"""Schema v2 normalized record union (PRD §14).

A discriminated union over ``kind`` — ``track`` / ``feature`` / ``event`` /
``alert`` / ``source_status``. Adapters normalize their source into one of these
at the edge so the backend stays generic (no per-source branching). Unknown
source-native fields go into ``attributes``; the top level is ``extra="forbid"``
so anything undeclared fails loudly instead of silently riding along.
"""

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from aether.schema.common import Confidence, UtcDatetime
from aether.schema.geometry import GeoJSONGeometry, GeoJSONPoint
from aether.schema.provenance import Provenance

#: Bumped whenever the record shape changes incompatibly (PRD §37 guardrail).
SCHEMA_VERSION = 2


class RecordBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[2] = 2
    kind: str
    id: str
    source: str
    observed_at: UtcDatetime  # source event time
    received_at: UtcDatetime  # adapter receipt time
    published_at: UtcDatetime  # normalized-record publication time
    correlation_key: str | None = None
    provenance: list[Provenance] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    attributes: dict[str, Any] = Field(default_factory=dict)


class Classification(BaseModel):
    """Military classification basis (PRD §11.5).

    First-cut model; the fusion/classification engine that populates it lands in
    M3. Only provider reports or ICAO address-block matches may set ``military``
    — no movement, callsign, route, or appearance heuristic (MIL-FR-004). Consumers
    must avoid certainty language when ``confidence`` is not high (MIL-FR-005).
    """

    model_config = ConfigDict(extra="forbid")

    military: bool | None = None
    basis: Literal["provider", "address_block", "both", "unknown"] = "unknown"
    confidence: Confidence = "unknown"
    note: str | None = None


class TrackRecord(RecordBase):
    kind: Literal["track"] = "track"
    track_type: Literal[
        "aircraft",
        "vessel",
        "aprs_station",
        "aprs_object",
        "radiosonde",
        "orbital_object",
        "other",
    ]
    label: str | None = None
    geometry: GeoJSONPoint | None = None
    altitude_m: float | None = None
    speed_mps: float | None = None
    heading_deg: float | None = None
    vertical_rate_mps: float | None = None
    locally_received: bool
    classification: Classification | None = None
    valid_until: UtcDatetime | None = None
    predicted: bool = False


class GeoFeatureRecord(RecordBase):
    kind: Literal["feature"] = "feature"
    feature_type: Literal[
        "lightning_flash",
        "lightning_cluster",
        "fire_detection",
        "earthquake",
        "tfr",
        "notam_geometry",
        "predicted_landing",
        "geofence",
        "other",
    ]
    geometry: GeoJSONGeometry
    valid_from: UtcDatetime | None = None
    valid_until: UtcDatetime | None = None
    severity: str | None = None
    label: str | None = None


class EventRecord(RecordBase):
    kind: Literal["event"] = "event"
    event_type: str
    subject_id: str | None = None
    summary: str
    message: str | None = None
    geometry: GeoJSONGeometry | None = None
    severity: str | None = None


class AlertRecord(RecordBase):
    kind: Literal["alert"] = "alert"
    rule_id: str
    subject_id: str | None = None
    state: Literal["open", "acknowledged", "resolved", "suppressed", "delivery_failed"]
    severity: Literal["info", "low", "medium", "high", "critical"]
    title: str
    summary: str
    triggered_at: UtcDatetime
    acknowledged_at: UtcDatetime | None = None
    resolved_at: UtcDatetime | None = None
    delivery_status: dict[str, str] = Field(default_factory=dict)


class SourceStatusRecord(RecordBase):
    kind: Literal["source_status"] = "source_status"
    status: Literal["starting", "connected", "degraded", "stale", "offline", "disabled"]
    last_success_at: UtcDatetime | None = None
    last_record_at: UtcDatetime | None = None
    lag_s: float | None = None
    records_received: int = 0
    records_rejected: int = 0
    error_code: str | None = None
    error_summary: str | None = None


#: The normalized record union, discriminated on ``kind`` (PRD §14).
Record = Annotated[
    TrackRecord | GeoFeatureRecord | EventRecord | AlertRecord | SourceStatusRecord,
    Field(discriminator="kind"),
]

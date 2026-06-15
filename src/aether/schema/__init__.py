"""Schema v2 — the normalized record union and its supporting types (PRD §14)."""

from aether.schema.common import Confidence, UtcDatetime
from aether.schema.geometry import (
    GeoJSONGeometry,
    GeoJSONPoint,
    LineString,
    MultiLineString,
    MultiPoint,
    MultiPolygon,
    Point,
    Polygon,
    Position,
)
from aether.schema.provenance import Provenance
from aether.schema.records import (
    SCHEMA_VERSION,
    AlertRecord,
    Classification,
    EventRecord,
    GeoFeatureRecord,
    Record,
    RecordBase,
    SourceStatusRecord,
    TrackRecord,
)
from aether.schema.validation import (
    MAX_RECORD_BYTES,
    RecordInput,
    RecordTooLargeError,
    dump_record,
    dump_record_json,
    parse_record,
    parse_records,
)

__all__ = [
    # common
    "Confidence",
    "UtcDatetime",
    # geometry
    "Position",
    "Point",
    "LineString",
    "Polygon",
    "MultiPoint",
    "MultiLineString",
    "MultiPolygon",
    "GeoJSONPoint",
    "GeoJSONGeometry",
    # provenance
    "Provenance",
    # records
    "SCHEMA_VERSION",
    "RecordBase",
    "Classification",
    "TrackRecord",
    "GeoFeatureRecord",
    "EventRecord",
    "AlertRecord",
    "SourceStatusRecord",
    "Record",
    # validation
    "MAX_RECORD_BYTES",
    "RecordInput",
    "RecordTooLargeError",
    "parse_record",
    "parse_records",
    "dump_record",
    "dump_record_json",
]

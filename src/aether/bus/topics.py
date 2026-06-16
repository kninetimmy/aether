"""MQTT topic routing and delivery policy (PRD §23).

Pure functions mapping a normalized record to where and how it goes on the bus —
no I/O, no aiomqtt — so the wire vocabulary is unit-testable on its own. The
backend and adapters share these so a record always lands on the same topic with
the same QoS/retention regardless of who publishes it.
"""

from aether.schema.records import Record, SourceStatusRecord

#: Versioned root for the whole scheme; bump alongside ``SCHEMA_VERSION``.
TOPIC_ROOT = "aether/v2"

#: Per-source record stream (track/feature/event/alert).
RECORDS_PREFIX = f"{TOPIC_ROOT}/records"
#: Per-source health stream (source_status); current status may be retained.
STATUS_PREFIX = f"{TOPIC_ROOT}/status"
#: System-level events not tied to a single source (operational; future use).
SYSTEM_EVENTS_TOPIC = f"{TOPIC_ROOT}/system/events"

#: Filters the backend subscribes to ingest the whole source tree (PRD §23).
SUBSCRIBE_FILTERS: tuple[str, ...] = (
    f"{RECORDS_PREFIX}/#",
    f"{STATUS_PREFIX}/#",
)


def record_topic(record: Record) -> str:
    """The topic a record publishes to: status stream for health, else records."""
    if isinstance(record, SourceStatusRecord):
        return f"{STATUS_PREFIX}/{record.source}"
    return f"{RECORDS_PREFIX}/{record.source}"


def record_qos(record: Record) -> int:
    """Delivery QoS per PRD §23: high-rate positions QoS 0, everything else QoS 1."""
    return 0 if record.kind == "track" else 1


def record_retain(record: Record) -> bool:
    """Only current source status is retained so a fresh client sees health (§23)."""
    return isinstance(record, SourceStatusRecord)

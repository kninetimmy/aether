"""Parsing, serialization, and size-guarding for schema v2 records.

The single entry point the bus and adapters use to turn bytes/dicts into
validated records and back. The oversized-payload guard (PRD §14.1) is applied
to raw wire input (bytes/str), since that's where untrusted, unbounded payloads
arrive; already-parsed dicts are treated as in-process and not re-measured.
"""

from typing import Any, cast

from pydantic import TypeAdapter

from aether.schema.records import Record

#: Reject wire payloads larger than this before parsing (PRD §14.1).
MAX_RECORD_BYTES = 64 * 1024

RecordInput = bytes | str | dict[str, Any]

_record_adapter: TypeAdapter[Record] = TypeAdapter(Record)
_records_adapter: TypeAdapter[list[Record]] = TypeAdapter(list[Record])


class RecordTooLargeError(ValueError):
    """Raised when a raw record payload exceeds ``MAX_RECORD_BYTES``."""


def _enforce_size(data: RecordInput, max_bytes: int) -> RecordInput:
    if isinstance(data, str):
        size = len(data.encode("utf-8"))
    elif isinstance(data, bytes):
        size = len(data)
    else:
        return data  # already-parsed dict: trusted in-process, not re-measured
    if size > max_bytes:
        raise RecordTooLargeError(f"record payload {size} bytes exceeds limit {max_bytes}")
    return data


def parse_record(data: RecordInput, *, max_bytes: int = MAX_RECORD_BYTES) -> Record:
    """Validate a single record from JSON bytes/str or a mapping.

    Routes to the correct concrete model via the ``kind`` discriminator and
    raises ``pydantic.ValidationError`` on anything malformed.
    """
    raw = _enforce_size(data, max_bytes)
    if isinstance(raw, (bytes, str)):
        return _record_adapter.validate_json(raw)
    return _record_adapter.validate_python(raw)


def parse_records(data: RecordInput, *, max_bytes: int = MAX_RECORD_BYTES) -> list[Record]:
    """Validate a JSON array (or list of mappings) of records."""
    raw = _enforce_size(data, max_bytes)
    if isinstance(raw, (bytes, str)):
        return _records_adapter.validate_json(raw)
    return _records_adapter.validate_python(raw)


def dump_record(record: Record) -> dict[str, Any]:
    """Serialize a record to a JSON-ready dict (datetimes as ISO 8601 UTC)."""
    return cast(dict[str, Any], _record_adapter.dump_python(record, mode="json"))


def dump_record_json(record: Record) -> bytes:
    """Serialize a record to compact JSON bytes, ready to publish on the bus."""
    return _record_adapter.dump_json(record)

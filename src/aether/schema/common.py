"""Shared primitives for schema v2 records.

Kept in its own module so the leaf models (``geometry``, ``provenance``) and the
record union (``records``) can all share the timestamp and confidence types
without import cycles. This is a small, deliberate extension of the file list in
PRD §30 — it carries no model of its own, only field types.
"""

from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import AfterValidator

#: Confidence label reused by provenance and military classification (PRD §14.2, §11.5).
Confidence = Literal["high", "medium", "low", "unknown"]


def _ensure_utc(value: datetime) -> datetime:
    """Reject naive datetimes and normalize aware ones to UTC.

    PRD §14.1: "All timestamps are timezone-aware UTC." We fail loudly on naive
    input rather than guessing a zone, then convert any aware value to UTC so the
    whole pipeline stores a single canonical form.
    """
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValueError("datetime must be timezone-aware (UTC)")
    return value.astimezone(UTC)


#: A timezone-aware datetime, normalized to UTC on validation.
UtcDatetime = Annotated[datetime, AfterValidator(_ensure_utc)]

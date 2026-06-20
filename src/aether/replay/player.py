"""Pure record reconstruction for replay (M4.8, PRD §19.6).

Turns persisted :class:`~aether.persist.database.ObservationRow` rows back into
wire-shaped schema-v2 record dicts. The persistence writer stores the *whole*
record JSON verbatim in ``observations.payload`` (see
:func:`aether.persist.writer.to_observation_row`), so reconstruction is lossless:
parse the payload, validate it against the schema, and dump it back to the wire
shape the live snapshot/websocket already emit — so a replayed record is byte-for-byte
what a client would have seen live, with no per-source branching.

Intentionally pure: no FastAPI, no I/O beyond accepting already-read rows, no clock,
no hub/engine reference. That keeps it deterministic and unit-testable, and keeps the
replay path decoupled from the live alert/notification path (the M4 exit invariant,
PRD §19.6/§32). A row whose payload fails to parse/validate is *skipped and logged*,
never raised — one bad stored row must not fail a whole replay window (PRD §37).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from typing import Any

from aether.persist.database import ObservationRow
from aether.schema.validation import dump_record, parse_record

log = logging.getLogger(__name__)


def reconstruct_records(rows: Sequence[ObservationRow]) -> list[dict[str, Any]]:
    """Reconstruct wire-shaped record dicts from persisted observation rows.

    Each row's verbatim ``payload`` is parsed and validated back into a schema-v2
    record, then dumped to the JSON-ready wire dict (datetimes as ISO-8601 UTC) — the
    same shape the live snapshot emits. Order is preserved from ``rows`` (the caller
    reads them ascending by ``observed_at``), so the returned list is the replay
    timeline in play order. A row that fails to parse or validate is skipped and
    logged rather than raised, so one malformed stored payload can't fail the window
    (PRD §37). This function does not touch the hub, the alert engine, or live state.
    """
    records: list[dict[str, Any]] = []
    for row in rows:
        try:
            record = parse_record(json.loads(row.payload))
        except Exception:
            # A corrupt/legacy/oversized stored payload: drop just this row. Logged at
            # debug so a noisy window doesn't flood the log; the count gap is visible
            # to the caller via len(records) < len(rows).
            log.debug(
                "skipping unreconstructable observation %s (corrupt payload)",
                row.record_id,
                exc_info=True,
            )
            continue
        records.append(dump_record(record))
    return records

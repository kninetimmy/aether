"""Websocket wire protocol mapping (PRD §22).

Pure translation between live-state objects and the JSON frames on ``/ws/v2`` —
deliberately free of FastAPI so it can be unit-tested on its own. The backend
owns the wire vocabulary; ``state`` stays agnostic of it.
"""

from typing import Any

from aether.schema.validation import dump_record
from aether.state.live import Snapshot, StateChange

# Wire "type" for an upsert of each kind (PRD §22.4). source_status has no
# "_upsert" suffix — its presence on the wire is the upsert.
_UPSERT_TYPE: dict[str, str] = {
    "track": "track_upsert",
    "feature": "feature_upsert",
    "alert": "alert_upsert",
    "source_status": "source_status",
}


def snapshot_message(snapshot: Snapshot) -> dict[str, Any]:
    """Build the full ``snapshot`` frame sent to a newly connected client (§22.3)."""
    return {
        "type": "snapshot",
        "seq": snapshot.seq,
        "tracks": [dump_record(r) for r in snapshot.tracks],
        "features": [dump_record(r) for r in snapshot.features],
        "events": [dump_record(r) for r in snapshot.events],
        "alerts": [dump_record(r) for r in snapshot.alerts],
        "source_status": [dump_record(r) for r in snapshot.source_status],
    }


def delta_message(change: StateChange) -> dict[str, Any]:
    """Build the delta frame for a single state change (§22.4)."""
    if change.op == "remove":
        return {"type": "remove", "seq": change.seq, "kind": change.kind, "id": change.id}
    assert change.record is not None  # upsert/event always carry the record
    if change.op == "event":
        return {"type": "event", "seq": change.seq, "record": dump_record(change.record)}
    return {
        "type": _UPSERT_TYPE[change.kind],
        "seq": change.seq,
        "record": dump_record(change.record),
    }

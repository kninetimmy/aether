"""In-memory live state: the backend's current fused world (PRD §19.1).

Holds current tracks and geo-features (keyed by id), a bounded ring of recent
events, open alerts, and the latest status per source, plus a monotonic sequence
number bumped on every mutation. ``apply`` returns a ``StateChange`` describing
what happened; the websocket layer turns those into wire deltas (PRD §22.4) and
serves ``snapshot()`` to newly connected clients.

No persistence here — SQLite arrives at M4 with its first consumers. A single
asyncio loop owns this object, so it carries no locks.
"""

from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, assert_never

from aether.schema.records import (
    AlertRecord,
    EventRecord,
    GeoFeatureRecord,
    Record,
    SourceStatusRecord,
    TrackRecord,
)
from aether.state.sequence import Sequence

#: Default cap on the recent-events ring buffer kept in live state.
RECENT_EVENTS_MAX = 256

StateKind = Literal["track", "feature", "event", "alert", "source_status"]


@dataclass(frozen=True)
class StateChange:
    """One mutation of live state, ready for the websocket to render as a delta."""

    seq: int
    op: Literal["upsert", "remove", "event"]
    kind: StateKind
    id: str
    record: Record | None  # set for upsert/event; None for remove


@dataclass
class Snapshot:
    """A point-in-time view of all live state at sequence ``seq`` (PRD §22.3)."""

    seq: int
    tracks: list[TrackRecord]
    features: list[GeoFeatureRecord]
    events: list[EventRecord]
    alerts: list[AlertRecord]
    source_status: list[SourceStatusRecord]


class LiveState:
    def __init__(self, *, recent_events_max: int = RECENT_EVENTS_MAX) -> None:
        self._seq = Sequence()
        self._tracks: dict[str, TrackRecord] = {}
        self._features: dict[str, GeoFeatureRecord] = {}
        self._alerts: dict[str, AlertRecord] = {}
        self._source_status: dict[str, SourceStatusRecord] = {}
        self._events: deque[EventRecord] = deque(maxlen=recent_events_max)

    @property
    def seq(self) -> int:
        return self._seq.current

    def apply(self, record: Record) -> StateChange:
        """Merge a normalized record into live state and return the change."""
        seq = self._seq.next()
        if isinstance(record, TrackRecord):
            self._tracks[record.id] = record
            return StateChange(seq, "upsert", "track", record.id, record)
        if isinstance(record, GeoFeatureRecord):
            self._features[record.id] = record
            return StateChange(seq, "upsert", "feature", record.id, record)
        if isinstance(record, AlertRecord):
            self._alerts[record.id] = record
            return StateChange(seq, "upsert", "alert", record.id, record)
        if isinstance(record, SourceStatusRecord):
            self._source_status[record.source] = record
            return StateChange(seq, "upsert", "source_status", record.id, record)
        if isinstance(record, EventRecord):
            self._events.append(record)
            return StateChange(seq, "event", "event", record.id, record)
        assert_never(record)

    def remove(self, kind: StateKind, id: str) -> StateChange:
        """Drop a track/feature/alert by id and return the removal change."""
        seq = self._seq.next()
        if kind == "track":
            self._tracks.pop(id, None)
        elif kind == "feature":
            self._features.pop(id, None)
        elif kind == "alert":
            self._alerts.pop(id, None)
        return StateChange(seq, "remove", kind, id, None)

    def expire(self, now: datetime) -> list[StateChange]:
        """Remove tracks/features whose ``valid_until`` has passed (PRD §8.4)."""
        changes: list[StateChange] = []
        for tid in [
            tid
            for tid, t in self._tracks.items()
            if t.valid_until is not None and t.valid_until <= now
        ]:
            changes.append(self.remove("track", tid))
        for fid in [
            fid
            for fid, f in self._features.items()
            if f.valid_until is not None and f.valid_until <= now
        ]:
            changes.append(self.remove("feature", fid))
        return changes

    def snapshot(self) -> Snapshot:
        """Return the full current state at the current sequence number."""
        return Snapshot(
            seq=self._seq.current,
            tracks=list(self._tracks.values()),
            features=list(self._features.values()),
            events=list(self._events),
            alerts=list(self._alerts.values()),
            source_status=list(self._source_status.values()),
        )

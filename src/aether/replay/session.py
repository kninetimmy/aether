"""Bounded in-memory registry of replay sessions (M4.8, PRD §19.6/§21.6).

A replay *session* is the metadata for one reconstructed window — the bounds, the
optional source filter, how many records it produced, and whether the ``max_records``
cap truncated it. The records themselves are returned once on creation and held by
the browser (replay is played client-side, PRD §19.6); the server keeps only this
small metadata so a client can re-read what a session *was* and tear it down.

The registry is deliberately bounded (PRD §37): it evicts the oldest session beyond
``max_sessions`` so an operator opening many windows can never grow server memory
without limit. It is pure data + an ordered dict — no clock, no I/O, no hub/engine
reference — so it stays unit-testable and keeps the replay path decoupled from the
live alert/notification path (the M4 exit invariant). ``created_at`` is supplied by
the caller (the API passes ``datetime.now(UTC).isoformat()``) so tests can pin it.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass

#: Default cap on concurrently-tracked replay sessions before the oldest is evicted.
DEFAULT_MAX_SESSIONS = 32


@dataclass(frozen=True)
class ReplaySession:
    """Metadata for one reconstructed replay window (no records — those go to the client)."""

    session_id: str
    start: str
    end: str
    sources: list[str] | None
    count: int
    truncated: bool
    created_at: str


class SessionRegistry:
    """Bounded, insertion-ordered store of :class:`ReplaySession` metadata.

    Newest-last ordering backs O(1) oldest-eviction: :meth:`add` past
    ``max_sessions`` drops the least-recently-added session. Lookups and deletes are
    by ``session_id``. No locking — the API drives it from the event-loop thread only,
    so all access is single-threaded.
    """

    def __init__(self, *, max_sessions: int = DEFAULT_MAX_SESSIONS) -> None:
        if max_sessions < 1:
            raise ValueError("max_sessions must be >= 1")
        self._max_sessions = max_sessions
        self._sessions: OrderedDict[str, ReplaySession] = OrderedDict()

    def add(self, session: ReplaySession) -> None:
        """Register ``session``, evicting the oldest if over the cap."""
        self._sessions[session.session_id] = session
        self._sessions.move_to_end(session.session_id)  # newest last
        while len(self._sessions) > self._max_sessions:
            self._sessions.popitem(last=False)  # drop the oldest

    def get(self, session_id: str) -> ReplaySession | None:
        """Return the session by id, or ``None`` if unknown/evicted."""
        return self._sessions.get(session_id)

    def delete(self, session_id: str) -> bool:
        """Remove the session by id; return ``True`` if it existed."""
        return self._sessions.pop(session_id, None) is not None

    def __len__(self) -> int:
        return len(self._sessions)

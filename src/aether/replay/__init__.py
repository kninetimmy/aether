"""Read-only record/replay over persisted history (M4.8, PRD §19.6).

The FINAL M4 exit slice. Replay is a *bounded, server-reconstructed buffer played
client-side*: the server reads persisted observations for a ``[start, end)`` window
on a fresh read-only SQLite connection, reconstructs full schema-v2 records from
each row's verbatim ``payload`` JSON, and returns them over plain REST; the browser
holds the buffer and runs the timeline locally.

The hard M4 exit invariant (PRD §19.6/§32) — **replay cannot fire live alerts or
notifications** — is guaranteed *structurally*, not by a runtime check: this package
is physically decoupled from the live path. It is REST + a read-only connection +
record reconstruction, and it must NEVER import or call the hub, ``hub.publish``, the
alert engine, or the notification dispatcher, and never mutate live state. The
import surface here is deliberately tiny (persistence reads + schema validation) so
that decoupling is self-evident on inspection.
"""

from aether.replay.player import reconstruct_records
from aether.replay.session import ReplaySession, SessionRegistry

__all__ = [
    "ReplaySession",
    "SessionRegistry",
    "reconstruct_records",
]

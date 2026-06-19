"""SQLite persistence store (M4, PRD §19).

Introduced at M4 *with its first consumer* — track history — and structured as a
sibling bus consumer that never gates serving live state (PRD §5). The public
surface is the lifespan runner :func:`aether.persist.runner.run_persistence`; the
:class:`~aether.persist.database.Database` handle and
:class:`~aether.persist.writer.PersistenceWriter` are the pieces it wires together.
"""

from aether.persist.database import Database, ObservationRow
from aether.persist.runner import build_sample_gate, run_persistence
from aether.persist.sampling import SampleGate
from aether.persist.writer import PersistenceWriter, to_observation_row

__all__ = [
    "Database",
    "ObservationRow",
    "PersistenceWriter",
    "SampleGate",
    "build_sample_gate",
    "run_persistence",
    "to_observation_row",
]

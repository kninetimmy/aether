"""Per-observation provenance (PRD §14.2).

Every record carries a list of these — one per source/receiver that contributed
to it. ``local_rf`` is the load-bearing flag that distinguishes "my antenna
heard this" from "an Internet feed reported it"; fusion appends provenance
entries rather than overwriting them so the operator can always trace a field
back to who observed it.
"""

from pydantic import BaseModel, ConfigDict, Field

from aether.schema.common import Confidence, UtcDatetime


class Provenance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str
    provider: str | None = None
    receiver_id: str | None = None
    observed_at: UtcDatetime
    received_at: UtcDatetime
    local_rf: bool = False
    derived: bool = False
    confidence: Confidence = "unknown"
    fields: list[str] = Field(default_factory=list)

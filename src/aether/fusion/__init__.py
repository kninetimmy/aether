"""In-process aircraft fusion (PRD §11.4, §15): one fused track per identity.

The backend owns authoritative fused state; same-identity local-RF and network
observations collapse onto one track keyed by ``correlation_key``, with fresh
local data privileged, network filling the gaps, and the track continuing from a
network observation when the local radio goes quiet (FUSION-FR-001..005). The
core is pure and deterministic — every decision takes an explicit ``now``
(FUSION-FR-007).
"""

from aether.fusion.engine import (
    FUSION_ATTR_KEY,
    Contributor,
    FusionConfig,
    FusionEngine,
    FusionGroup,
)
from aether.fusion.freshness import (
    DEFAULT_FRESHNESS,
    DEFAULT_FRESHNESS_FALLBACK,
    FreshnessClass,
    FreshnessWindow,
)

__all__ = [
    "DEFAULT_FRESHNESS",
    "DEFAULT_FRESHNESS_FALLBACK",
    "FUSION_ATTR_KEY",
    "Contributor",
    "FreshnessClass",
    "FreshnessWindow",
    "FusionConfig",
    "FusionEngine",
    "FusionGroup",
]

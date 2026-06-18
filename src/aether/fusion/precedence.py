"""Per-field precedence: which contributor wins each dynamic/metadata field.

This is the heart of "fresh local-RF wins, network fills the gaps, and when the
local radio goes quiet the network observation continues the track" (FUSION-FR-002/
003/004, PRD §15.2). It is deliberately a pure function of a list of contributor
*views* (a contributor plus its already-computed freshness at a given ``now``) —
no clock, no source-name ``if/elif``. Ranking is driven by ``local_rf`` (from
provenance) and freshness only, so the backend stays generic (PRD §37).

The precedence ladder (PRD §15.2), best-first:

    0  live  + local RF      fresh from my own antenna — wins outright
    1  live  + network       fresh Internet feed
    2  stale + local RF      my antenna, going quiet — still beats stale network
    3  stale + network       stale Internet feed
    4  expired (either)      cached prior value, last resort

Within a tier, the most recently observed wins; ties break on source name for a
total, deterministic order (FUSION-FR-007). Geometry is selected *whole* from one
contributor — positions are never averaged or interpolated across sources
(PRD §15.5).
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from aether.fusion.freshness import FreshnessClass
from aether.schema.records import TrackRecord

#: Dynamic state fields fused per-contributor (each may come from a different source).
DYNAMIC_FIELDS = (
    "geometry",
    "altitude_m",
    "speed_mps",
    "heading_deg",
    "vertical_rate_mps",
)

#: Slower-moving descriptive fields; the freshest non-expired contributor wins.
METADATA_FIELDS = ("label", "classification")


@dataclass(frozen=True)
class ContributorView:
    """A contributor evaluated at a fixed ``now``: its record + freshness."""

    source: str
    record: TrackRecord
    local_rf: bool
    observed_at: datetime
    freshness: FreshnessClass


@dataclass(frozen=True)
class FieldPick:
    """The winning value for one field, plus which source/freshness it came from."""

    value: Any
    source: str | None
    freshness: FreshnessClass | None


def _tier(view: ContributorView) -> int:
    """Precedence tier (lower = better); the PRD §15.2 ladder."""
    if view.freshness == "expired":
        return 4
    if view.local_rf:
        return 0 if view.freshness == "live" else 2
    return 1 if view.freshness == "live" else 3


def _sort_key(view: ContributorView) -> tuple[int, float, str]:
    """Total order: tier, then most-recent first, then source name for ties."""
    return (_tier(view), -view.observed_at.timestamp(), view.source)


def pick_dynamic_field(field: str, views: list[ContributorView]) -> FieldPick:
    """Pick the winning value of a dynamic field across contributors.

    A contributor only competes for a field it actually carries (non-``None``):
    this is how the network "fills a field the local observation lacks"
    (FUSION-FR-003) — local can win position while network wins ``speed_mps``
    that local never reported. Expired contributors still compete (tier 4) so a
    stale-but-known value survives over nothing.
    """
    candidates = [v for v in views if getattr(v.record, field) is not None]
    if not candidates:
        return FieldPick(None, None, None)
    winner = min(candidates, key=_sort_key)
    return FieldPick(getattr(winner.record, field), winner.source, winner.freshness)


def pick_metadata_field(field: str, views: list[ContributorView]) -> FieldPick:
    """Pick a metadata field (label/classification) from the freshest live/stale source.

    Expired contributors are excluded — a name or classification from an expired
    observation is not worth surfacing — and ``local_rf`` does not privilege
    metadata (a callsign is a callsign regardless of who heard it). Ties break on
    most-recent then source name for determinism.
    """
    candidates = [
        v for v in views if v.freshness != "expired" and getattr(v.record, field) is not None
    ]
    if not candidates:
        return FieldPick(None, None, None)
    winner = min(candidates, key=lambda v: (-v.observed_at.timestamp(), v.source))
    return FieldPick(getattr(winner.record, field), winner.source, winner.freshness)

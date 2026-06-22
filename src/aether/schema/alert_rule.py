"""Operator-defined alert rules (PRD §20.1, §11.16 ALERT-FR-001..003, §21.4).

An alert rule is *operator configuration*, not an observed record: the operator
defines when a fused-state transition, event, or source-status change should raise
an :class:`~aether.schema.records.AlertRecord`. Like a geofence it is low-volume
CRUD config, persisted in the ``alert_rules`` table and managed via
``/api/v2/alert-rules`` — distinct from the high-rate observation stream.

This module is the *model + validation only*. The evaluation engine that consumes
a rule against live state (matching :class:`AlertCondition`s, honoring
``transition``/``cooldown_s``/``geofence_id``/``schedule``/``quiet_hours``,
emitting and de-duplicating alerts) lands in the next M4 slice; nothing here
evaluates a rule. Keeping the model complete now means the engine slice adds no
config migration (the full rule shape is stored as JSON in ``payload``).

Field-path convention (the contract the engine will honor): :attr:`AlertCondition.
field` is a dotted path resolved against the *dumped record* — e.g.
``attributes.squawk`` reads ``dump["attributes"]["squawk"]`` and
``classification.military`` reads the nested classification object. Top-level
fields (``locally_received``, ``track_type``, ``status``) are bare names. The
engine resolves these paths; the model only stores them.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from aether.schema.common import UtcDatetime

#: Alert severity ladder — mirrors :class:`~aether.schema.records.AlertRecord`'s
#: ``severity`` so a rule's severity flows straight onto the alert it raises.
AlertSeverity = Literal["info", "low", "medium", "high", "critical"]

#: Delivery channels (PRD §11.16 ALERT-FR-004, §20.4). The driver for each lands
#: with notifications (later M4 slice); the rule only records which are selected.
AlertChannel = Literal["dashboard", "browser", "email", "discord"]

#: Condition operators (PRD §20.2). The model stores them; the engine assigns
#: evaluation semantics. Grouped by what comparand each needs (enforced below):
#: comparand value, list value, numeric threshold, time window, or none.
ConditionOperator = Literal[
    "equals",
    "not_equals",
    "in",
    "not_in",
    "greater_than",
    "less_than",
    "changed_to",
    "changed_from",
    "exists",
    "not_exists",
    "entered_geofence",
    "exited_geofence",
    "geofence_intersects",
    "became_active",
    "source_stale",
    "source_offline",
    "count_within_window",
    "distance_below",
    "distance_above",
    "elevation_crossed",
    "classification_basis",
    "local_rf",
    "watchlist",
]

#: A condition comparand: a JSON scalar or a list of scalars (for ``in``/``not_in``).
#: ``bool`` precedes ``int`` in the union so JSON ``true`` stays a bool, not ``1``.
ConditionScalar = bool | int | float | str
ConditionValue = ConditionScalar | list[ConditionScalar]

#: Operators requiring a scalar comparand in ``value``.
_COMPARAND_OPERATORS = frozenset(
    {
        "equals",
        "not_equals",
        "greater_than",
        "less_than",
        "changed_to",
        "changed_from",
        "classification_basis",
    }
)
#: Operators requiring a numeric scalar comparand (a subset of the above).
_NUMERIC_OPERATORS = frozenset({"greater_than", "less_than"})
#: Operators requiring a non-empty list comparand in ``value``.
_LIST_OPERATORS = frozenset({"in", "not_in"})
#: Operators requiring a numeric ``threshold``.
_THRESHOLD_OPERATORS = frozenset({"distance_below", "distance_above", "elevation_crossed"})

#: ``HH:MM`` 24-hour clock, used by the schedule/quiet-hours windows.
_HHMM_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def _validate_hhmm(value: str, field: str) -> str:
    if not _HHMM_RE.match(value):
        raise ValueError(f"{field} must be 'HH:MM' 24-hour time, got {value!r}")
    return value


class AlertCondition(BaseModel):
    """One predicate over a fused record (PRD §20.1 ``condition``, §20.2 operators).

    A rule ANDs its list of these (see :attr:`AlertRule.conditions`); compound
    templates — e.g. military *and* locally-received — are expressed as several
    conditions rather than a nested boolean tree (kept flat for this slice). The
    cross-field validator enforces the comparand each operator needs so a malformed
    rule is rejected at the API edge (HTTP 422) rather than silently never matching.
    """

    model_config = ConfigDict(extra="forbid")

    field: str = Field(min_length=1, max_length=200)
    operator: ConditionOperator
    value: ConditionValue | None = None
    #: Numeric threshold for ``distance_*``/``elevation_crossed`` (and the count for
    #: ``count_within_window``); metres for distance, degrees for elevation.
    threshold: float | None = None
    #: Time window in seconds for ``count_within_window``.
    window_s: float | None = Field(default=None, gt=0.0)

    @model_validator(mode="after")
    def _operator_comparand(self) -> AlertCondition:
        op = self.operator
        if op in _LIST_OPERATORS:
            if not isinstance(self.value, list) or not self.value:
                raise ValueError(f"operator {op!r} requires a non-empty list value")
        elif op in _COMPARAND_OPERATORS:
            if self.value is None or isinstance(self.value, list):
                raise ValueError(f"operator {op!r} requires a scalar value")
            if op in _NUMERIC_OPERATORS and isinstance(self.value, (bool, str)):
                raise ValueError(f"operator {op!r} requires a numeric value")
        elif op == "count_within_window":
            if self.threshold is None or self.window_s is None:
                raise ValueError("operator 'count_within_window' requires threshold and window_s")
        elif op in _THRESHOLD_OPERATORS and self.threshold is None:
            raise ValueError(f"operator {op!r} requires a numeric threshold")
        return self


class TimeWindow(BaseModel):
    """A daily ``HH:MM``–``HH:MM`` UTC window (PRD §20.5 quiet hours/schedule).

    A window may wrap past midnight (``start`` > ``end``); the engine interprets
    that as spanning into the next day. Times are UTC — the COP stores UTC and the
    UI localizes (PRD §9.5), so a rule's active window has one unambiguous basis.
    """

    model_config = ConfigDict(extra="forbid")

    start: str
    end: str

    @model_validator(mode="after")
    def _valid_times(self) -> TimeWindow:
        _validate_hhmm(self.start, "start")
        _validate_hhmm(self.end, "end")
        return self


class Schedule(BaseModel):
    """When a rule is active (PRD §11.16 ALERT-FR-003 "schedule", §20.5).

    ``days_of_week`` uses Python's convention: 0=Monday … 6=Sunday. An optional
    ``window`` further restricts the active period within each listed day; absent,
    the whole day is active.
    """

    model_config = ConfigDict(extra="forbid")

    days_of_week: list[int] = Field(min_length=1)
    window: TimeWindow | None = None

    @model_validator(mode="after")
    def _valid_days(self) -> Schedule:
        if any(d < 0 or d > 6 for d in self.days_of_week):
            raise ValueError("days_of_week entries must be 0..6 (0=Monday)")
        if len(set(self.days_of_week)) != len(self.days_of_week):
            raise ValueError("days_of_week must not repeat")
        return self


class AlertRuleCreate(BaseModel):
    """Request body for ``POST /api/v2/alert-rules`` — operator-supplied fields only.

    ``id``/timestamps are server-assigned. ``enabled`` defaults on for an
    operator-created rule (seeded templates override it to off, PRD ALERT-FR-008).
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    severity: AlertSeverity
    subject_types: list[str] = Field(min_length=1)
    conditions: list[AlertCondition] = Field(min_length=1)
    enabled: bool = True
    transition: Literal["enter", "exit", "change"] | None = None
    geofence_id: str | None = None
    cooldown_s: float = Field(default=900.0, ge=0.0)
    dedup_key: str | None = Field(default=None, max_length=200)
    channels: list[AlertChannel] = Field(min_length=1)
    schedule: Schedule | None = None
    quiet_hours: TimeWindow | None = None
    description: str | None = Field(default=None, max_length=2000)


class AlertRuleUpdate(BaseModel):
    """Request body for ``PATCH /api/v2/alert-rules/{id}`` — every field optional.

    A field left unset (``None``/absent) keeps its stored value; the patch is applied
    field-by-field by :meth:`AlertRule.with_update`. As with geofences, a nullable
    field cannot be *cleared* to ``None`` via PATCH in this slice (``None`` means
    "unchanged"); that refinement can come with the UI.
    """

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=200)
    severity: AlertSeverity | None = None
    subject_types: list[str] | None = Field(default=None, min_length=1)
    conditions: list[AlertCondition] | None = Field(default=None, min_length=1)
    enabled: bool | None = None
    transition: Literal["enter", "exit", "change"] | None = None
    geofence_id: str | None = None
    cooldown_s: float | None = Field(default=None, ge=0.0)
    dedup_key: str | None = Field(default=None, max_length=200)
    channels: list[AlertChannel] | None = Field(default=None, min_length=1)
    schedule: Schedule | None = None
    quiet_hours: TimeWindow | None = None
    description: str | None = Field(default=None, max_length=2000)


class AlertRule(BaseModel):
    """A stored alert rule — operator config with a stable id and audit timestamps."""

    model_config = ConfigDict(extra="forbid")

    id: str
    name: str = Field(min_length=1, max_length=200)
    severity: AlertSeverity
    subject_types: list[str] = Field(min_length=1)
    conditions: list[AlertCondition] = Field(min_length=1)
    enabled: bool = True
    transition: Literal["enter", "exit", "change"] | None = None
    geofence_id: str | None = None
    cooldown_s: float = Field(default=900.0, ge=0.0)
    dedup_key: str | None = Field(default=None, max_length=200)
    channels: list[AlertChannel] = Field(min_length=1)
    schedule: Schedule | None = None
    quiet_hours: TimeWindow | None = None
    description: str | None = Field(default=None, max_length=2000)
    created_at: UtcDatetime
    updated_at: UtcDatetime

    @classmethod
    def create(cls, body: AlertRuleCreate, *, id: str, now: UtcDatetime) -> AlertRule:
        """Build a stored rule from a create request at time ``now``."""
        return cls(
            id=id,
            created_at=now,
            updated_at=now,
            **body.model_dump(),
        )

    def with_update(self, patch: AlertRuleUpdate, *, now: UtcDatetime) -> AlertRule:
        """Return a copy with ``patch``'s set fields applied and ``updated_at=now``.

        ``created_at`` is preserved; only fields the patch actually sets (present and
        non-``None``) change. Changed values are taken as their model values — not
        ``model_dump(exclude_unset=True)``, which would strip nested condition/schedule
        defaults — then the merged copy is re-validated so a patched condition list
        still satisfies the per-operator comparand rules.
        """
        changes: dict[str, Any] = {
            name: getattr(patch, name)
            for name in patch.model_fields_set
            if getattr(patch, name) is not None
        }
        merged = self.model_copy(update={**changes, "updated_at": now})
        return AlertRule.model_validate(merged.model_dump())

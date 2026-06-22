"""Alert-rule condition evaluation — the stateless leaf-predicate core (PRD §20.1–20.2).

A rule's :attr:`~aether.schema.alert_rule.AlertRule.conditions` is an **AND of leaf
predicates** (:class:`~aether.schema.alert_rule.AlertCondition`). This module turns
one leaf — and the whole AND — into a boolean *level predicate* over a single
record, resolving each leaf's dotted ``field`` path against the **dumped** record
(``dump_record``, PRD §14): ``attributes.squawk`` reads
``dump["attributes"]["squawk"]``, ``classification.military`` reads the nested
classification object, and bare names like ``locally_received``/``status`` read the
top level. Evaluating against the *dumped* form (JSON scalars) — not the Pydantic
model — means a rule's JSON comparand (`true`, `"7700"`, a number) compares against
a like-typed value, the same contract the rule model documents.

Scope of this slice (M4.6a): only the operators that a *single record snapshot*
fully determines (:data:`STATELESS_OPERATORS`). The remaining §20.2 operators are
**contextual** — they need prior per-subject state (``changed_to``/``changed_from``),
a geofence shape (``entered_geofence``/``exited_geofence``), the station position or
a geometry (``distance_*``/``elevation_crossed``), or a time window
(``count_within_window``) — and belong to the stateful engine slice (M4.6b). Asking
this module to evaluate one raises :class:`UnsupportedOperator` rather than silently
returning ``False``: a rule that *can't* be evaluated here must fail loudly, never
masquerade as "did not match" (PRD §37 honest degradation). The engine layers
transition/cooldown/dedup/schedule/quiet-hours on top of this level predicate;
nothing here is stateful or clock-aware.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any

from aether.schema.alert_rule import AlertCondition, ConditionOperator

log = logging.getLogger(__name__)

#: Sentinel for "the field path did not resolve" — distinct from a resolved ``None``
#: (a field that exists but is null, e.g. ``classification.military``). ``exists``
#: treats both as absent; ``not_equals``/``not_in`` distinguish them from a concrete
#: comparand. A private unique object so no real payload value can collide with it.
_MISSING: Any = object()

#: Operators a single record snapshot fully determines (this slice). The leaf's
#: ``field`` is resolved against the dumped record and compared per :func:`evaluate_leaf`.
#: ``source_stale``/``source_offline`` are level reads of a source-status ``status``
#: field here; the *transition* ("became stale/offline", PRD §20.2) is the engine's
#: ``transition`` edge layered on this predicate (M4.6b), not a separate operator.
STATELESS_OPERATORS: frozenset[ConditionOperator] = frozenset(
    {
        "equals",
        "not_equals",
        "in",
        "not_in",
        "greater_than",
        "less_than",
        "exists",
        "not_exists",
        "classification_basis",
        "local_rf",
        "watchlist",
        "source_stale",
        "source_offline",
    }
)

#: Operators that need state/geometry/time beyond one record — owned by the engine
#: slice (M4.6b). Listed explicitly (rather than "everything not stateless") so a new
#: operator added to the model surfaces here as a conscious classification choice.
CONTEXTUAL_OPERATORS: frozenset[ConditionOperator] = frozenset(
    {
        "changed_to",
        "changed_from",
        "entered_geofence",
        "exited_geofence",
        "geofence_intersects",
        "became_active",
        "count_within_window",
        "distance_below",
        "distance_above",
        "elevation_crossed",
    }
)


class UnsupportedOperator(ValueError):
    """A condition uses an operator this stateless core cannot evaluate alone.

    Raised by :func:`evaluate_leaf`/:func:`evaluate_conditions` for a
    :data:`CONTEXTUAL_OPERATORS` member. The engine slice (M4.6b) routes those through
    its stateful evaluator; here they must fail loudly so a contextual rule is never
    silently reported as non-matching. Carries the offending operator for the caller.
    """

    def __init__(self, operator: str) -> None:
        super().__init__(f"operator {operator!r} needs engine context; not evaluable statelessly")
        self.operator = operator


def resolve_field(dump: dict[str, Any], path: str) -> Any:
    """Resolve a dotted ``field`` path against a dumped record; ``_MISSING`` if absent.

    Walks ``path`` segment by segment through nested mappings (``a.b.c``). Returns
    :data:`_MISSING` the moment a segment is absent or the current node is not a
    mapping — so a missing path is distinguishable from a resolved ``None`` (a field
    that exists but is null). The dump is the JSON-mode serialization, so values are
    JSON scalars/containers, matching a rule's JSON comparand types.
    """
    current: Any = dump
    for segment in path.split("."):
        if isinstance(current, dict) and segment in current:
            current = current[segment]
        else:
            return _MISSING
    return current


def _as_number(value: Any) -> float | None:
    """Return ``value`` as a float for ordered comparison, or ``None`` if not numeric.

    ``bool`` is excluded though it subclasses ``int``: ``greater_than``/``less_than``
    are numeric-threshold operators (the rule model already rejects a bool comparand),
    so comparing a boolean field ordinally is a type error, reported as "no match"
    rather than the surprising ``True > 0``.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _truthiness_match(present: bool, value: Any, comparand: Any) -> bool:
    """Shared ``local_rf``/``watchlist`` semantics: does the field's truthiness equal a target.

    The comparand defaults to ``True`` (the common "is locally-received / is
    watchlisted" rule); an explicit ``False`` matches the negative. A missing field
    counts as falsy, so ``local_rf: false`` matches a record with no such field.
    """
    target = True if comparand is None else bool(comparand)
    return bool(present and value) == target


def evaluate_leaf(condition: AlertCondition, dump: dict[str, Any]) -> bool:
    """Evaluate one leaf predicate against a dumped record (level, stateless).

    Resolves ``condition.field`` and applies ``condition.operator``. Operators with
    a defined meaning on an *absent* field (``exists``/``not_exists`` and the negative
    comparisons ``not_equals``/``not_in``) are handled before the presence guard, so a
    missing field is "not equal to 7700" → ``True`` rather than a spurious ``False``;
    the positive comparisons require a present, non-null value. Raises
    :class:`UnsupportedOperator` for a :data:`CONTEXTUAL_OPERATORS` member.
    """
    op = condition.operator
    if op not in STATELESS_OPERATORS:
        raise UnsupportedOperator(op)

    value = resolve_field(dump, condition.field)
    present = value is not _MISSING and value is not None

    # Operators defined even when the field is absent/null.
    if op == "exists":
        return present
    if op == "not_exists":
        return not present
    if op == "not_equals":
        return not (present and value == condition.value)
    if op == "not_in":
        return not (present and value in _as_list(condition.value))
    if op == "source_stale":
        return present and value == "stale"
    if op == "source_offline":
        return present and value == "offline"
    if op == "local_rf":
        return _truthiness_match(present, value, condition.value)
    if op == "watchlist":
        return _truthiness_match(present, value, condition.value)

    # Positive comparisons: an absent/null field cannot match a concrete comparand.
    if not present:
        return False
    if op == "equals":
        return bool(value == condition.value)
    if op == "classification_basis":
        return bool(value == condition.value)
    if op == "in":
        return value in _as_list(condition.value)
    if op == "greater_than":
        a, b = _as_number(value), _as_number(condition.value)
        return a is not None and b is not None and a > b
    if op == "less_than":
        a, b = _as_number(value), _as_number(condition.value)
        return a is not None and b is not None and a < b

    # Unreachable: STATELESS_OPERATORS membership is exhaustive above. Defensive so a
    # future operator added to the set without a branch fails loudly, not silently.
    raise UnsupportedOperator(op)  # pragma: no cover


def _as_list(value: Any) -> list[Any]:
    """Coerce an ``in``/``not_in`` comparand to a list for membership testing.

    The rule model already validates these operators carry a non-empty list, so this
    is a typing convenience; a stray scalar degrades to a single-element list rather
    than raising, keeping evaluation total.
    """
    return value if isinstance(value, list) else [value]


def is_stateless(conditions: Iterable[AlertCondition]) -> bool:
    """Return whether every leaf can be evaluated by this core (no contextual operator).

    Lets a caller (the engine's level path, the ``/test`` preview) decide up front
    whether :func:`evaluate_conditions` applies, instead of catching
    :class:`UnsupportedOperator` from a half-evaluated AND.
    """
    return all(c.operator in STATELESS_OPERATORS for c in conditions)


def evaluate_conditions(conditions: Iterable[AlertCondition], dump: dict[str, Any]) -> bool:
    """Evaluate the AND of leaf predicates against a dumped record (PRD §20.1).

    Short-circuits on the first non-matching leaf. Raises :class:`UnsupportedOperator`
    if any leaf is contextual — the engine (M4.6b) is responsible for routing such a
    rule through its stateful evaluator and must not call this for it; failing loud
    keeps a not-yet-evaluable rule from reading as "did not match".
    """
    return all(evaluate_leaf(condition, dump) for condition in conditions)

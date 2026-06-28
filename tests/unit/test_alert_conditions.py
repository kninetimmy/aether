"""Stateless alert-condition evaluation core (M4.6a, PRD §20.1–20.2).

Exercises :mod:`aether.alerts.conditions` against *real dumped records* (via
``dump_record``) so the dotted field-path contract is tested against the actual
schema serialization (``attributes.squawk``, ``classification.military``,
``locally_received``, source-status ``status``) — not a hand-rolled dict that could
drift from the model. Also pins the honest-degradation contract: a contextual
operator raises rather than silently reporting "no match".
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from aether.alerts.conditions import (
    CONTEXTUAL_OPERATORS,
    STATELESS_OPERATORS,
    UnsupportedOperator,
    evaluate_conditions,
    evaluate_leaf,
    is_stateless,
    resolve_field,
)
from aether.schema.alert_rule import AlertCondition
from aether.schema.records import Classification, SourceStatusRecord, TrackRecord
from aether.schema.validation import dump_record

T0 = datetime(2026, 6, 19, 12, 0, 0, tzinfo=UTC)


def _aircraft_dump(
    *,
    squawk: str | None = None,
    locally_received: bool = True,
    classification: Classification | None = None,
    altitude_m: float | None = None,
) -> dict[str, Any]:
    """Dump a realistic aircraft :class:`TrackRecord` to its JSON-mode dict."""
    attributes = {"squawk": squawk} if squawk is not None else {}
    record = TrackRecord(
        id="aircraft:icao:abc123",
        source="local_adsb",
        observed_at=T0,
        received_at=T0,
        published_at=T0,
        correlation_key="aircraft:icao:abc123",
        track_type="aircraft",
        altitude_m=altitude_m,
        locally_received=locally_received,
        classification=classification,
        attributes=attributes,
    )
    return dump_record(record)


def _source_status_dump(status: str) -> dict[str, Any]:
    record = SourceStatusRecord(
        id="source_status:local_adsb",
        source="local_adsb",
        observed_at=T0,
        received_at=T0,
        published_at=T0,
        status=status,  # type: ignore[arg-type]
    )
    return dump_record(record)


def _leaf(field: str, operator: str, **kw: Any) -> AlertCondition:
    return AlertCondition(field=field, operator=operator, **kw)  # type: ignore[arg-type]


# --- resolve_field --------------------------------------------------------------


def test_resolve_field_top_level_nested_and_missing() -> None:
    dump = _aircraft_dump(squawk="7700", classification=Classification(military=True))
    assert resolve_field(dump, "locally_received") is True
    assert resolve_field(dump, "attributes.squawk") == "7700"
    assert resolve_field(dump, "classification.military") is True
    # A missing path resolves to the sentinel, which is falsy for `exists` below but
    # is NOT None — so a present-null field stays distinguishable from an absent one.
    assert resolve_field(dump, "attributes.nonexistent") is not None
    assert resolve_field(dump, "no.such.path") is not None


def test_resolve_field_distinguishes_present_null_from_missing() -> None:
    # classification omitted entirely → the whole object is null in the dump.
    dump = _aircraft_dump(squawk="7700")
    assert resolve_field(dump, "classification") is None  # present, explicitly null
    assert evaluate_leaf(_leaf("classification", "exists"), dump) is False
    # A path *under* a null object does not resolve (can't index None).
    assert evaluate_leaf(_leaf("classification.military", "exists"), dump) is False


# --- equals / not_equals --------------------------------------------------------


def test_equals_matches_and_misses() -> None:
    dump = _aircraft_dump(squawk="7700")
    assert evaluate_leaf(_leaf("attributes.squawk", "equals", value="7700"), dump) is True
    assert evaluate_leaf(_leaf("attributes.squawk", "equals", value="1200"), dump) is False


def test_equals_on_missing_field_is_false_but_not_equals_is_true() -> None:
    dump = _aircraft_dump()  # no squawk
    assert evaluate_leaf(_leaf("attributes.squawk", "equals", value="7700"), dump) is False
    # A field that isn't there is "not equal to 7700".
    assert evaluate_leaf(_leaf("attributes.squawk", "not_equals", value="7700"), dump) is True


def test_boolean_equals_is_typed_not_truthy() -> None:
    dump = _aircraft_dump(classification=Classification(military=True))
    assert evaluate_leaf(_leaf("classification.military", "equals", value=True), dump) is True
    assert evaluate_leaf(_leaf("classification.military", "equals", value=False), dump) is False


# --- in / not_in ----------------------------------------------------------------


def test_in_and_not_in() -> None:
    dump = _aircraft_dump(squawk="7600")
    emergencies = ["7500", "7600", "7700"]
    assert evaluate_leaf(_leaf("attributes.squawk", "in", value=emergencies), dump) is True
    assert evaluate_leaf(_leaf("attributes.squawk", "not_in", value=emergencies), dump) is False
    benign = _aircraft_dump(squawk="1200")
    assert evaluate_leaf(_leaf("attributes.squawk", "in", value=emergencies), benign) is False
    # Missing field is "not in" any concrete list.
    assert evaluate_leaf(_leaf("attributes.squawk", "not_in", value=emergencies), _aircraft_dump())


# --- greater_than / less_than ---------------------------------------------------


def test_numeric_comparisons() -> None:
    dump = _aircraft_dump(altitude_m=2500.0)
    assert evaluate_leaf(_leaf("altitude_m", "greater_than", value=1000.0), dump) is True
    assert evaluate_leaf(_leaf("altitude_m", "less_than", value=1000.0), dump) is False
    assert evaluate_leaf(_leaf("altitude_m", "less_than", value=3000.0), dump) is True


def test_numeric_comparison_on_nonnumeric_field_is_false() -> None:
    # A squawk is a string; ordering it numerically is a type error → no match,
    # never a surprising truthy/string comparison.
    dump = _aircraft_dump(squawk="7700")
    assert evaluate_leaf(_leaf("attributes.squawk", "greater_than", value=10.0), dump) is False


def test_numeric_comparison_on_missing_field_is_false() -> None:
    dump = _aircraft_dump()  # altitude_m omitted → null in dump
    assert evaluate_leaf(_leaf("altitude_m", "greater_than", value=1000.0), dump) is False


# --- exists / not_exists --------------------------------------------------------


def test_exists_and_not_exists() -> None:
    with_squawk = _aircraft_dump(squawk="7700")
    without = _aircraft_dump()
    assert evaluate_leaf(_leaf("attributes.squawk", "exists"), with_squawk) is True
    assert evaluate_leaf(_leaf("attributes.squawk", "exists"), without) is False
    assert evaluate_leaf(_leaf("attributes.squawk", "not_exists"), without) is True


# --- classification_basis -------------------------------------------------------


def test_classification_basis() -> None:
    dump = _aircraft_dump(
        classification=Classification(military=True, basis="address_block", confidence="low")
    )
    assert evaluate_leaf(
        _leaf("classification.basis", "classification_basis", value="address_block"), dump
    )
    assert not evaluate_leaf(
        _leaf("classification.basis", "classification_basis", value="provider"), dump
    )


# --- local_rf / watchlist -------------------------------------------------------


def test_local_rf_truthiness() -> None:
    local = _aircraft_dump(locally_received=True)
    network = _aircraft_dump(locally_received=False)
    # Default comparand is True ("is locally received").
    assert evaluate_leaf(_leaf("locally_received", "local_rf"), local) is True
    assert evaluate_leaf(_leaf("locally_received", "local_rf"), network) is False
    # Explicit False matches the negative; a missing field counts as falsy.
    assert evaluate_leaf(_leaf("locally_received", "local_rf", value=False), network) is True


def test_watchlist_operator_is_contextual_not_stateless() -> None:
    """The watchlist operator is contextual (membership-based); evaluate_leaf raises."""
    assert "watchlist" in CONTEXTUAL_OPERATORS
    assert "watchlist" not in STATELESS_OPERATORS
    with pytest.raises(UnsupportedOperator) as exc_info:
        evaluate_leaf(_leaf("watchlist", "watchlist"), _aircraft_dump())
    assert exc_info.value.operator == "watchlist"


def test_watchlist_operator_makes_rule_not_stateless() -> None:
    conditions = [_leaf("watchlist", "watchlist")]
    assert is_stateless(conditions) is False


# --- source_stale / source_offline ----------------------------------------------


def test_source_status_level_reads() -> None:
    offline = _leaf("status", "source_offline")
    stale = _leaf("status", "source_stale")
    assert evaluate_leaf(offline, _source_status_dump("offline")) is True
    assert evaluate_leaf(offline, _source_status_dump("connected")) is False
    assert evaluate_leaf(stale, _source_status_dump("stale")) is True
    assert evaluate_leaf(stale, _source_status_dump("offline")) is False


# --- AND of leaves: the shipping templates --------------------------------------


def test_military_local_template_conditions() -> None:
    """`rule-aircraft-military-local`: military AND locally-received (demo03 vs demo04)."""
    conditions = [
        _leaf("classification.military", "equals", value=True),
        _leaf("locally_received", "equals", value=True),
    ]
    local_mil = _aircraft_dump(
        locally_received=True, classification=Classification(military=True, basis="provider")
    )
    network_mil = _aircraft_dump(
        locally_received=False, classification=Classification(military=True, basis="address_block")
    )
    local_civ = _aircraft_dump(locally_received=True)
    assert evaluate_conditions(conditions, local_mil) is True  # demo03-like → fires
    assert evaluate_conditions(conditions, network_mil) is False  # demo04-like → network, no fire
    assert evaluate_conditions(conditions, local_civ) is False  # local but not military


def test_squawk_template_condition() -> None:
    conditions = [_leaf("attributes.squawk", "equals", value="7700")]
    assert evaluate_conditions(conditions, _aircraft_dump(squawk="7700")) is True
    assert evaluate_conditions(conditions, _aircraft_dump(squawk="1200")) is False
    assert evaluate_conditions(conditions, _aircraft_dump()) is False


def test_and_short_circuits_on_first_false() -> None:
    # Second leaf is contextual; if the first (false) leaf short-circuits, the AND
    # returns False WITHOUT raising on the unreachable contextual leaf.
    conditions = [
        _leaf("attributes.squawk", "equals", value="7700"),
        _leaf("attributes.x", "changed_to", value="y"),
    ]
    assert evaluate_conditions(conditions, _aircraft_dump(squawk="1200")) is False


# --- contextual operators fail loud, not silent ---------------------------------


@pytest.mark.parametrize("operator", sorted(CONTEXTUAL_OPERATORS))
def test_contextual_operators_raise(operator: str) -> None:
    cond = _leaf("attributes.squawk", operator, value="x", threshold=1.0, window_s=60.0)
    with pytest.raises(UnsupportedOperator) as exc:
        evaluate_leaf(cond, _aircraft_dump(squawk="7700"))
    assert exc.value.operator == operator


def test_evaluate_conditions_raises_when_a_leaf_is_contextual() -> None:
    conditions = [
        _leaf("attributes.squawk", "equals", value="7700"),  # matches
        _leaf("geofence", "entered_geofence"),  # contextual → must surface
    ]
    with pytest.raises(UnsupportedOperator):
        evaluate_conditions(conditions, _aircraft_dump(squawk="7700"))


def test_is_stateless_classifies_rules() -> None:
    assert is_stateless([_leaf("attributes.squawk", "equals", value="7700")]) is True
    assert is_stateless([_leaf("x", "entered_geofence")]) is False
    assert is_stateless([]) is True  # vacuous AND


def test_operator_partition_is_total_and_disjoint() -> None:
    """Every model operator is classified exactly once (stateless XOR contextual)."""
    from typing import get_args

    from aether.schema.alert_rule import ConditionOperator

    all_ops = set(get_args(ConditionOperator))
    assert STATELESS_OPERATORS | CONTEXTUAL_OPERATORS == all_ops
    assert STATELESS_OPERATORS & CONTEXTUAL_OPERATORS == set()

"""Alert-rule model, condition validation, and patch semantics (M4.5)."""

from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from aether.schema.alert_rule import (
    AlertCondition,
    AlertRule,
    AlertRuleCreate,
    AlertRuleUpdate,
    Schedule,
    TimeWindow,
)

T0 = datetime(2026, 6, 19, 12, 0, 0, tzinfo=UTC)
T1 = datetime(2026, 6, 19, 13, 0, 0, tzinfo=UTC)


def _squawk_cond() -> AlertCondition:
    return AlertCondition(field="attributes.squawk", operator="equals", value="7700")


def _create(**overrides: Any) -> AlertRuleCreate:
    base: dict[str, Any] = dict(
        name="rule",
        severity="high",
        subject_types=["aircraft"],
        conditions=[_squawk_cond()],
        channels=["dashboard"],
    )
    base.update(overrides)
    return AlertRuleCreate(**base)


def _rule(**overrides: Any) -> AlertRule:
    return AlertRule.create(_create(**overrides), id="rule-1", now=T0)


# -- create / stored shape ----------------------------------------------------


def test_create_stamps_id_and_timestamps_and_defaults() -> None:
    rule = _rule()
    assert rule.id == "rule-1"
    assert rule.created_at == T0
    assert rule.updated_at == T0
    assert rule.enabled is True  # operator-created defaults on
    assert rule.cooldown_s == 900.0
    assert rule.transition is None


def test_create_preserves_compound_conditions_and_bool_value() -> None:
    rule = _rule(
        conditions=[
            AlertCondition(field="classification.military", operator="equals", value=True),
            AlertCondition(field="locally_received", operator="equals", value=True),
        ]
    )
    assert len(rule.conditions) == 2
    # JSON ``true`` stays a bool, not coerced to int 1 (union ordering).
    assert rule.conditions[0].value is True
    assert isinstance(rule.conditions[0].value, bool)


# -- field/list-level validation ----------------------------------------------


def test_severity_must_be_in_the_ladder() -> None:
    with pytest.raises(ValidationError):
        _create(severity="catastrophic")


def test_channels_and_conditions_and_subject_types_must_be_non_empty() -> None:
    with pytest.raises(ValidationError):
        _create(channels=[])
    with pytest.raises(ValidationError):
        _create(conditions=[])
    with pytest.raises(ValidationError):
        _create(subject_types=[])


def test_unknown_channel_rejected() -> None:
    with pytest.raises(ValidationError):
        _create(channels=["pager"])


def test_cooldown_must_be_non_negative() -> None:
    with pytest.raises(ValidationError):
        _create(cooldown_s=-1.0)


# -- condition operator/comparand validation ----------------------------------


def test_in_operator_requires_non_empty_list() -> None:
    AlertCondition(field="attributes.squawk", operator="in", value=["7500", "7600"])  # ok
    with pytest.raises(ValidationError):
        AlertCondition(field="attributes.squawk", operator="in", value="7500")
    with pytest.raises(ValidationError):
        AlertCondition(field="attributes.squawk", operator="in", value=[])


def test_comparand_operator_requires_scalar_value() -> None:
    with pytest.raises(ValidationError):
        AlertCondition(field="x", operator="equals")  # value missing
    with pytest.raises(ValidationError):
        AlertCondition(field="x", operator="equals", value=["a"])  # list, not scalar


def test_numeric_operator_rejects_non_numeric() -> None:
    AlertCondition(field="altitude_m", operator="greater_than", value=1000)  # ok
    with pytest.raises(ValidationError):
        AlertCondition(field="altitude_m", operator="greater_than", value="1000")
    with pytest.raises(ValidationError):
        AlertCondition(field="altitude_m", operator="greater_than", value=True)


def test_count_within_window_requires_window_and_threshold() -> None:
    AlertCondition(field="x", operator="count_within_window", threshold=5, window_s=60)  # ok
    with pytest.raises(ValidationError):
        AlertCondition(field="x", operator="count_within_window", threshold=5)
    with pytest.raises(ValidationError):
        AlertCondition(field="x", operator="count_within_window", window_s=60)


def test_threshold_operators_require_threshold() -> None:
    AlertCondition(field="geometry", operator="distance_below", threshold=5000.0)  # ok
    with pytest.raises(ValidationError):
        AlertCondition(field="geometry", operator="distance_below")


def test_value_free_operators_need_no_comparand() -> None:
    # exists / source_offline / entered_geofence carry no comparand.
    AlertCondition(field="attributes.squawk", operator="exists")
    AlertCondition(field="status", operator="source_offline")
    AlertCondition(field="geometry", operator="entered_geofence")


def test_window_s_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        AlertCondition(field="x", operator="count_within_window", threshold=1, window_s=0)


# -- schedule / quiet-hours validation ----------------------------------------


def test_time_window_rejects_bad_clock() -> None:
    TimeWindow(start="22:00", end="06:00")  # ok (wraps midnight — allowed)
    with pytest.raises(ValidationError):
        TimeWindow(start="24:00", end="06:00")
    with pytest.raises(ValidationError):
        TimeWindow(start="9:00", end="06:00")  # not zero-padded HH


def test_schedule_days_must_be_in_range_and_unique() -> None:
    Schedule(days_of_week=[0, 5, 6])  # ok: Mon, Sat, Sun
    with pytest.raises(ValidationError):
        Schedule(days_of_week=[7])
    with pytest.raises(ValidationError):
        Schedule(days_of_week=[1, 1])


def test_rule_accepts_schedule_and_quiet_hours() -> None:
    rule = _rule(
        schedule=Schedule(
            days_of_week=[0, 1, 2, 3, 4], window=TimeWindow(start="08:00", end="18:00")
        ),
        quiet_hours=TimeWindow(start="23:00", end="07:00"),
    )
    assert rule.schedule is not None
    assert rule.quiet_hours is not None
    assert rule.schedule.window is not None


# -- patch semantics ----------------------------------------------------------


def test_with_update_changes_only_set_fields_and_bumps_updated_at() -> None:
    rule = _rule()
    patched = rule.with_update(AlertRuleUpdate(name="renamed", enabled=False), now=T1)
    assert patched.name == "renamed"
    assert patched.enabled is False
    assert patched.severity == "high"  # untouched
    assert patched.created_at == T0  # preserved
    assert patched.updated_at == T1  # bumped
    assert patched.id == rule.id


def test_with_update_can_replace_conditions_and_revalidates() -> None:
    rule = _rule()
    new_cond = AlertCondition(field="attributes.squawk", operator="in", value=["7500", "7700"])
    patched = rule.with_update(AlertRuleUpdate(conditions=[new_cond]), now=T1)
    assert patched.conditions[0].operator == "in"
    assert patched.conditions[0].value == ["7500", "7700"]


def test_update_body_rejects_bad_condition_at_construction() -> None:
    # A malformed condition can't even enter a patch body — the API returns 422
    # before with_update runs, so a bad rule is never persisted.
    with pytest.raises(ValidationError):
        AlertRuleUpdate(conditions=[AlertCondition(field="x", operator="in", value="not-a-list")])


def test_with_update_preserves_nested_schedule_round_trip() -> None:
    # The model_dump→model_validate round-trip inside with_update must keep nested
    # config intact (the discriminator/default-preservation concern), not drop it.
    rule = _rule()
    patched = rule.with_update(
        AlertRuleUpdate(
            schedule=Schedule(days_of_week=[5, 6], window=TimeWindow(start="00:00", end="06:00"))
        ),
        now=T1,
    )
    assert patched.schedule is not None
    assert patched.schedule.days_of_week == [5, 6]
    assert patched.schedule.window is not None
    assert patched.schedule.window.start == "00:00"
    assert patched.conditions == rule.conditions  # untouched

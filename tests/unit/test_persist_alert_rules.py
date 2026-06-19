"""Alert-rule CRUD persistence + idempotent template seeding (M4.5)."""

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from aether.alerts.templates import default_rule_templates
from aether.persist.alert_rules import (
    delete_alert_rule,
    get_alert_rule,
    insert_alert_rule,
    list_alert_rules,
    seed_alert_rules,
    update_alert_rule,
)
from aether.persist.database import Database
from aether.schema.alert_rule import AlertCondition, AlertRule, AlertRuleCreate, AlertRuleUpdate

T0 = datetime(2026, 6, 19, 12, 0, 0, tzinfo=UTC)
T1 = datetime(2026, 6, 19, 13, 0, 0, tzinfo=UTC)


def _migrated_db(tmp_path: Path) -> str:
    """A store with the schema applied (migration v3 creates ``alert_rules``)."""
    path = str(tmp_path / "alerts.db")
    db = Database(path)
    db.open()  # runs all migrations
    db.close()
    return path


def _rule(
    rule_id: str, *, name: str = "rule", severity: str = "high", now: datetime = T0
) -> AlertRule:
    return AlertRule.create(
        AlertRuleCreate(
            name=name,
            severity=severity,  # type: ignore[arg-type]
            subject_types=["aircraft"],
            conditions=[AlertCondition(field="attributes.squawk", operator="equals", value="7700")],
            channels=["dashboard"],
        ),
        id=rule_id,
        now=now,
    )


def test_insert_then_list_and_get_round_trip(tmp_path: Path) -> None:
    path = _migrated_db(tmp_path)
    insert_alert_rule(path, _rule("r-a", name="alpha"))
    insert_alert_rule(path, _rule("r-b", name="bravo"))

    listed = list_alert_rules(path)
    assert [r.id for r in listed] == ["r-a", "r-b"]  # oldest-first by created_at, id

    one = get_alert_rule(path, "r-a")
    assert one is not None
    assert one.name == "alpha"
    assert one.conditions[0].value == "7700"


def test_update_replaces_mutable_fields_including_severity(tmp_path: Path) -> None:
    path = _migrated_db(tmp_path)
    rule = _rule("r-a", name="alpha", severity="high")
    insert_alert_rule(path, rule)

    patch = AlertRuleUpdate(name="renamed", severity="low", enabled=False)
    updated = rule.with_update(patch, now=T1)
    assert update_alert_rule(path, updated) is True

    fetched = get_alert_rule(path, "r-a")
    assert fetched is not None
    assert fetched.name == "renamed"
    assert fetched.severity == "low"
    assert fetched.enabled is False
    assert fetched.created_at == T0  # preserved
    assert fetched.updated_at == T1


def test_update_missing_row_returns_false(tmp_path: Path) -> None:
    path = _migrated_db(tmp_path)
    assert update_alert_rule(path, _rule("ghost")) is False


def test_delete_reports_existence(tmp_path: Path) -> None:
    path = _migrated_db(tmp_path)
    insert_alert_rule(path, _rule("r-a"))
    assert delete_alert_rule(path, "r-a") is True
    assert delete_alert_rule(path, "r-a") is False  # already gone
    assert get_alert_rule(path, "r-a") is None


def test_reads_tolerate_missing_store_file(tmp_path: Path) -> None:
    missing = str(tmp_path / "never.db")
    assert list_alert_rules(missing) == []
    assert get_alert_rule(missing, "anything") is None


def test_reads_tolerate_uncreated_table(tmp_path: Path) -> None:
    path = str(tmp_path / "empty.db")
    sqlite3.connect(path).close()  # create the file, no schema
    assert list_alert_rules(path) == []
    assert get_alert_rule(path, "anything") is None


# -- template seeding ---------------------------------------------------------


def test_seed_inserts_disabled_templates_and_is_idempotent(tmp_path: Path) -> None:
    path = _migrated_db(tmp_path)
    templates = default_rule_templates(T0)

    inserted = seed_alert_rules(path, templates)
    assert inserted == len(templates)

    stored = list_alert_rules(path)
    assert {r.id for r in stored} == {t.id for t in templates}
    assert all(r.enabled is False for r in stored)  # ALERT-FR-008: disabled by default

    # A second seed inserts nothing — stable ids make it a no-op.
    assert seed_alert_rules(path, default_rule_templates(T1)) == 0
    assert len(list_alert_rules(path)) == len(templates)


def test_seed_does_not_clobber_operator_edits(tmp_path: Path) -> None:
    path = _migrated_db(tmp_path)
    templates = default_rule_templates(T0)
    seed_alert_rules(path, templates)

    # Operator enables and renames a seeded template.
    target = templates[0]
    edited = get_alert_rule(path, target.id)
    assert edited is not None
    edit = AlertRuleUpdate(enabled=True, name="my rule")
    update_alert_rule(path, edited.with_update(edit, now=T1))

    # Re-seeding leaves the edit untouched (INSERT OR IGNORE on the existing id).
    assert seed_alert_rules(path, default_rule_templates(T1)) == 0
    after = get_alert_rule(path, target.id)
    assert after is not None
    assert after.enabled is True
    assert after.name == "my rule"

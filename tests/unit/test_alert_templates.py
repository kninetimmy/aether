"""Default alert-rule templates (PRD §12, ALERT-FR-008).

Guards the shape contract the seeder relies on: every template ships disabled with a
stable, unique id, and the M5 environmental templates (earthquakes, USGS-FR-005)
target the specific ``earthquake`` feature layer with a reported-magnitude condition.
"""

from __future__ import annotations

from datetime import UTC, datetime

from aether.alerts.templates import default_rule_templates

T0 = datetime(2026, 6, 19, 12, 0, 0, tzinfo=UTC)


def test_all_templates_ship_disabled_with_unique_ids() -> None:
    templates = default_rule_templates(T0)
    assert templates, "expected at least one seeded template"
    assert all(not t.enabled for t in templates)  # ALERT-FR-008: provided but off
    ids = [t.id for t in templates]
    assert len(ids) == len(set(ids))  # stable ids are unique → idempotent seeding


def test_earthquake_templates_target_earthquake_layer() -> None:
    by_id = {t.id: t for t in default_rule_templates(T0)}

    significant = by_id["rule-earthquake-significant"]
    assert significant.subject_types == ["earthquake"]
    assert significant.transition == "change"  # one alert per new/revised quake
    cond = significant.conditions[0]
    assert (cond.field, cond.operator, cond.value) == (
        "attributes.magnitude",
        "greater_than",
        4.5,
    )

    nearby = by_id["rule-earthquake-nearby"]
    assert nearby.subject_types == ["earthquake"]
    ops = {c.operator for c in nearby.conditions}
    assert ops == {"greater_than", "distance_below"}  # magnitude AND distance-from-station

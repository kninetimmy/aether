"""Default alert-rule templates (PRD §12, ALERT-FR-008).

Guards the shape contract the seeder relies on: every template ships disabled with a
stable, unique id, and the M5 environmental templates target the specific feature
layer — earthquakes (USGS-FR-005) by reported magnitude, FIRMS active fire
(FIRMS-FR-005) by reported FRP and distance from the station.
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


def test_fire_templates_target_fire_detection_layer() -> None:
    by_id = {t.id: t for t in default_rule_templates(T0)}

    intensity = by_id["rule-fire-high-intensity"]
    assert intensity.subject_types == ["fire_detection"]
    assert intensity.transition == "change"  # one alert per newly-seen detection
    cond = intensity.conditions[0]
    assert (cond.field, cond.operator, cond.value) == (
        "attributes.frp_mw",
        "greater_than",
        50.0,
    )

    nearby = by_id["rule-fire-nearby"]
    assert nearby.subject_types == ["fire_detection"]
    # PRD §12 #13 is "within a configured radius" — distance from the station only.
    assert [c.operator for c in nearby.conditions] == ["distance_below"]

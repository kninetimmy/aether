"""Default alert-rule templates (PRD §12, §11.16 ALERT-FR-008).

These are the editable starting points the operator can enable and tune. Every
template ships **disabled** (ALERT-FR-008: "Rule templates shall be provided but
may be disabled by default") with a *stable* id so seeding is idempotent — a
re-seed never duplicates one, and an operator's edits to a seeded rule survive a
restart (the seeder only inserts ids that are absent).

Scope of this slice: only templates whose conditions reference fields/sources that
already exist in the COP and have settled semantics — aircraft emergency squawks,
locally-received military aircraft, and source-offline. The remaining PRD §12
templates are seeded by the milestone that owns their source/feature, so a template
never references a field whose meaning isn't defined yet:

- geofence enter/exit/altitude (#6, #7), watchlist aircraft (#5): the alert engine
  slice (geofence containment + watchlist matching).
- APRS emergency/message (#8, #10), AIS (#22, #23): with their detail semantics.
- lightning/FIRMS/earthquake/TFR/satellite/balloon (#11–#21): M5/M6.
- disk-budget/time-sync (#25, #26): with the system-event sources that raise them.

Templates are defined as ``(id, AlertRuleCreate)`` pairs and stamped with ``now``
at seed time (:func:`default_rule_templates`), so this module stays clock-free
(importable without side effects) and the ids remain the durable identity.
"""

from __future__ import annotations

from aether.schema.alert_rule import AlertCondition, AlertRule, AlertRuleCreate
from aether.schema.common import UtcDatetime


def _squawk_template(code: str, meaning: str) -> tuple[str, AlertRuleCreate]:
    return (
        f"rule-aircraft-{code}",
        AlertRuleCreate(
            name=f"Emergency squawk {code}",
            severity="high",
            subject_types=["aircraft"],
            conditions=[AlertCondition(field="attributes.squawk", operator="equals", value=code)],
            enabled=False,
            transition="enter",
            channels=["dashboard", "browser"],
            description=(
                f"Aircraft transponder reports squawk {code} ({meaning}). Reported code, "
                "not an independently verified emergency (PRD ADSB-FR-004)."
            ),
        ),
    )


#: ``(stable_id, create_body)`` pairs. Append-only across milestones; never reuse
#: or renumber an id once released (an operator may have enabled/edited it).
_TEMPLATES: tuple[tuple[str, AlertRuleCreate], ...] = (
    _squawk_template("7500", "unlawful interference / hijack"),
    _squawk_template("7600", "radio communication failure"),
    _squawk_template("7700", "general emergency"),
    (
        "rule-aircraft-military-local",
        AlertRuleCreate(
            name="Locally received military aircraft",
            severity="medium",
            subject_types=["aircraft"],
            conditions=[
                AlertCondition(field="classification.military", operator="equals", value=True),
                AlertCondition(field="locally_received", operator="equals", value=True),
            ],
            enabled=False,
            transition="enter",
            channels=["dashboard"],
            description=(
                "Aircraft received by the local antenna and classified military by provider "
                "report or ICAO address block. Classification is not authoritative "
                "(PRD MIL-FR-005)."
            ),
        ),
    ),
    (
        "rule-source-offline",
        AlertRuleCreate(
            name="Source offline",
            severity="medium",
            subject_types=["source"],
            conditions=[AlertCondition(field="status", operator="source_offline")],
            enabled=False,
            transition="enter",
            cooldown_s=1800.0,
            channels=["dashboard"],
            description="A data source stopped reporting beyond its staleness window (§12 #24).",
        ),
    ),
)


def default_rule_templates(now: UtcDatetime) -> list[AlertRule]:
    """Build the default templates as stored rules stamped at ``now`` (all disabled).

    Ids are stable so seeding is idempotent; ``now`` only stamps ``created_at``/
    ``updated_at`` for rules that don't exist yet.
    """
    return [AlertRule.create(body, id=rule_id, now=now) for rule_id, body in _TEMPLATES]

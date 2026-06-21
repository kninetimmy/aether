"""Default alert-rule templates (PRD §12, §11.16 ALERT-FR-008).

These are the editable starting points the operator can enable and tune. Every
template ships **disabled** (ALERT-FR-008: "Rule templates shall be provided but
may be disabled by default") with a *stable* id so seeding is idempotent — a
re-seed never duplicates one, and an operator's edits to a seeded rule survive a
restart (the seeder only inserts ids that are absent).

Scope: only templates whose conditions reference fields/sources that already exist in
the COP and have settled semantics — aircraft emergency squawks, locally-received
military aircraft, source-offline, and (M5) earthquakes once the USGS layer ships
(USGS-FR-005). The remaining PRD §12 templates are seeded by the milestone that owns
their source/feature, so a template never references a field whose meaning isn't
defined yet:

- geofence enter/exit/altitude (#6, #7), watchlist aircraft (#5): the alert engine
  slice (geofence containment + watchlist matching).
- APRS emergency/message (#8, #10), AIS (#22, #23): with their detail semantics.
- lightning/TFR/satellite/balloon (#11/#12, #15–#21): the M5/M6 slice that lands each
  source (earthquakes #14 and FIRMS detections #13 are done; the rest seed with theirs).
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
    # -- M5 environmental alerts (earthquakes, USGS-FR-005, PRD §12 #11/#19) --------
    # An earthquake is a point-in-time occurrence, so these use the ``change``
    # transition (one alert per new/revised quake, cooldown-gated) rather than an
    # open-until-resolved level — there is no natural "exit" for a quake. Magnitude
    # is the reported USGS value; the COP is NOT authoritative (PRD §11.2/§37).
    (
        "rule-earthquake-significant",
        AlertRuleCreate(
            name="Significant earthquake",
            severity="medium",
            subject_types=["earthquake"],
            conditions=[
                AlertCondition(field="attributes.magnitude", operator="greater_than", value=4.5)
            ],
            enabled=False,
            transition="change",
            channels=["dashboard", "browser"],
            description=(
                "USGS reports an earthquake greater than M4.5 inside the AOI. Reported "
                "magnitude, not an independently verified one; an 'automatic' (un-reviewed) "
                "solution may be revised (PRD USGS-FR-003)."
            ),
        ),
    ),
    (
        "rule-earthquake-nearby",
        AlertRuleCreate(
            name="Nearby earthquake",
            severity="high",
            subject_types=["earthquake"],
            conditions=[
                AlertCondition(field="attributes.magnitude", operator="greater_than", value=3.0),
                # Distance from the home station, in metres (~135 NM). With no station
                # configured (lat/lon 0,0) this leaf is unevaluable, so the rule stays
                # inert and visibly does not fire — never measures from null island
                # (PRD §5/§37). Set AETHER_STATION_LAT/_LON to activate it.
                AlertCondition(field="geometry", operator="distance_below", threshold=250_000.0),
            ],
            enabled=False,
            transition="change",
            channels=["dashboard", "browser"],
            description=(
                "USGS reports an earthquake greater than M3.0 within ~135 NM of the home "
                "station. Requires a configured station position; reported magnitude, not "
                "verified (PRD USGS-FR-003)."
            ),
        ),
    ),
    # -- M5 environmental alerts (FIRMS active fire, PRD §12 #13, FIRMS-FR-005) ------
    # A FIRMS record is one satellite thermal-anomaly pixel at one acquisition time —
    # NOT a confirmed wildfire (FIRMS-FR-005) and not a continuously-updated track. So,
    # like earthquakes, these fire on the ``change`` transition (one alert per newly-seen
    # detection, cooldown-gated) rather than holding an open level with no natural exit.
    # "high-intensity" filters on reported fire radiative power (FRP, MW) and works AOI-
    # wide; "nearby" filters on distance from the home station and is inert (visibly does
    # not fire) until a station is configured — it never measures from null island.
    (
        "rule-fire-high-intensity",
        AlertRuleCreate(
            name="High-intensity fire detection",
            severity="medium",
            subject_types=["fire_detection"],
            conditions=[
                AlertCondition(field="attributes.frp_mw", operator="greater_than", value=50.0)
            ],
            enabled=False,
            transition="change",
            channels=["dashboard", "browser"],
            description=(
                "NASA FIRMS reports an active-fire detection above 50 MW fire radiative "
                "power inside the AOI. A satellite thermal anomaly, NOT a confirmed "
                "wildfire; FRP is the reported pixel value (PRD FIRMS-FR-004/005)."
            ),
        ),
    ),
    (
        "rule-fire-nearby",
        AlertRuleCreate(
            name="Nearby fire detection",
            severity="high",
            subject_types=["fire_detection"],
            conditions=[
                # Distance from the home station, in metres (~27 NM). With no station
                # configured (lat/lon 0,0) this leaf is unevaluable, so the rule stays
                # inert and visibly does not fire — never measures from null island
                # (PRD §5/§37). Set AETHER_STATION_LAT/_LON to activate it.
                AlertCondition(field="geometry", operator="distance_below", threshold=50_000.0),
            ],
            enabled=False,
            transition="change",
            channels=["dashboard", "browser"],
            description=(
                "NASA FIRMS reports an active-fire detection within ~27 NM of the home "
                "station. Requires a configured station position; a satellite thermal "
                "anomaly, NOT a confirmed wildfire (PRD §12 #13, FIRMS-FR-005)."
            ),
        ),
    ),
)


def default_rule_templates(now: UtcDatetime) -> list[AlertRule]:
    """Build the default templates as stored rules stamped at ``now`` (all disabled).

    Ids are stable so seeding is idempotent; ``now`` only stamps ``created_at``/
    ``updated_at`` for rules that don't exist yet.
    """
    return [AlertRule.create(body, id=rule_id, now=now) for rule_id, body in _TEMPLATES]

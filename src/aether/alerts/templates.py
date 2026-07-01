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
- lightning/satellite/balloon (#11/#12, #17–#21): the M5/M6 slice that lands each
  source (earthquakes #14, FIRMS detections #13, TFR-intersects-geofence #15, and
  TFR-becomes-active #16 are done; the rest seed with theirs).
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
    # -- M6 airspace alerts (FAA TFR, AIRSPACE-FR, PRD §12 #15) ----------------------
    # A TFR is an areal, time-bounded feature, so this is the first *areal* alert:
    # geofence_intersects tests the TFR polygon against the operator's geofence shape
    # (horizontal overlap only — the TFR's vertical limits are text, not a single
    # altitude). It rides the ``enter`` transition, so it fires once when an intersecting
    # TFR first appears and auto-resolves when the TFR ages out of live state. It ships
    # WITHOUT a geofence_id (and disabled): the operator points it at one of their fences
    # and enables it — with no geofence set the rule is unevaluable and visibly never
    # fires (never a phantom overlap; PRD §37).
    (
        "rule-tfr-intersects-geofence",
        AlertRuleCreate(
            name="TFR intersecting a geofence",
            severity="medium",
            subject_types=["tfr"],
            conditions=[AlertCondition(field="geometry", operator="geofence_intersects")],
            enabled=False,
            transition="enter",
            channels=["dashboard", "browser"],
            description=(
                "An FAA TFR whose area overlaps a configured geofence is on the map. Set "
                "this rule's geofence and enable it; horizontal overlap only (the TFR's "
                "altitude limits are not compared). Not a flight-planning product; consult "
                "official FAA sources (PRD AIRSPACE-FR-007)."
            ),
        ),
    ),
    # A TFR is time-bounded (valid_from..valid_until), so this fires on the *activation*
    # edge: ``became_active`` is True once the clock reaches valid_from. No observation
    # arrives at that instant (a re-poll may dedupe the unchanged revision), so the
    # live-state sweep re-drives a TFR crossing valid_from to deliver the rising edge
    # (PRD §32 #16). It rides ``enter`` — fires once on activation (or first sight if a
    # TFR is already active) and auto-resolves when the TFR expires out of live state.
    # AOI-wide and disabled by default; pair it with a geofence/distance rule to scope
    # which activations matter. A TFR with no parsed effective time is unevaluable, so
    # the rule visibly never fires on it rather than inventing an activation (PRD §37).
    (
        "rule-tfr-became-active",
        AlertRuleCreate(
            name="TFR became active",
            severity="medium",
            subject_types=["tfr"],
            conditions=[AlertCondition(field="valid_from", operator="became_active")],
            enabled=False,
            transition="enter",
            channels=["dashboard", "browser"],
            description=(
                "An FAA TFR reached its effective time and is now active inside the AOI "
                "(or was already active when first received). Fires once on activation and "
                "auto-resolves when the TFR expires. AOI-wide; pair with a geofence or "
                "distance rule to scope it. Not a flight-planning product; consult official "
                "FAA sources (PRD AIRSPACE-FR-007)."
            ),
        ),
    ),
    # -- M6 orbital alerts (watched satellite rise, PRD §12/§32 #17, ALERT-FR-008) ---
    # A watched satellite is a continuous track (valid_from has no meaning here), so this
    # rides ``enter``: it fires once on the rising edge as the SGP4-PREDICTED elevation
    # crosses up through the threshold and auto-resolves on set (the level drops back below
    # it, or the satellite ages out of live state once it falls under the display floor).
    # It keys off ``attributes.elevation_deg`` — the AUTHORITATIVE SGP4 az/el set by the
    # CelesTrak adapter — NOT the flat-earth ``geo.elevation_angle_deg`` of the
    # ``elevation_crossed`` operator (which is ~10 deg wrong near the horizon for orbits).
    # The ``watchlist`` leaf scopes it to the operator's orbital watchlist
    # (orbital:celestrak:<norad>); with an empty watchlist it is visibly inert (never
    # fires) rather than alerting AOI-wide. AOI/floor-bounded and disabled by default.
    (
        "rule-satellite-rise",
        AlertRuleCreate(
            name="Watched satellite rise",
            severity="info",
            subject_types=["orbital_object"],
            conditions=[
                AlertCondition(
                    field="attributes.elevation_deg", operator="greater_than", value=10.0
                ),
                AlertCondition(field="watchlist", operator="watchlist"),
            ],
            enabled=False,
            transition="enter",
            channels=["dashboard", "browser"],
            description=(
                "A satellite on your watchlist has risen above 10 deg elevation as seen "
                "from the home station. Position and elevation are SGP4-PREDICTED from the "
                "latest CelesTrak element sets (accuracy degrades with element age), not "
                "observed, and are not a precise antenna look-angle. Fires once on rise "
                "and auto-resolves on set; detection is bounded by the configured display "
                "elevation floor. Requires a configured station position and the satellite "
                "on your watchlist (orbital:celestrak:<norad>). Not authoritative for any "
                "operational use (PRD §32 #17)."
            ),
        ),
    ),
    # -- M6 orbital alerts (watched satellite culmination, PRD §12/§32 #18) ----------
    # Culmination is mid-pass and well above the display floor, so its rising edge lands
    # on an ordinary emitted fast-tier tick (like #17's rise edge): a plain ``enter`` rule,
    # no engine change. ``culmination_reached`` is True once the clock reaches the SGP4-
    # PREDICTED max-elevation instant the adapter stamps in attributes.pass_culmination_at;
    # the open alert auto-resolves when the satellite sets out of live state. The watchlist
    # leaf scopes it to orbital:celestrak:<norad>; empty watchlist ⇒ visibly inert.
    (
        "rule-satellite-culmination",
        AlertRuleCreate(
            name="Watched satellite culmination",
            severity="info",
            subject_types=["orbital_object"],
            conditions=[
                AlertCondition(
                    field="attributes.pass_culmination_at", operator="culmination_reached"
                ),
                AlertCondition(field="watchlist", operator="watchlist"),
            ],
            enabled=False,
            transition="enter",
            channels=["dashboard", "browser"],
            description=(
                "A satellite on your watchlist has reached its maximum elevation "
                "(culmination) over the home station — the highest point of this pass. The "
                "culmination time is SGP4-PREDICTED from the latest CelesTrak element sets "
                "(accuracy degrades with element age), not observed, and is not a precise "
                "antenna look-angle. Fires once at predicted culmination and auto-resolves "
                "when the satellite sets out of live state. Requires a configured station "
                "position and the satellite on your watchlist (orbital:celestrak:<norad>). "
                "Not authoritative for any operational use (PRD §32 #18)."
            ),
        ),
    ),
    # -- M6 orbital alerts (watched satellite pass end, PRD §12/§32 #19) --------------
    # Same conditions as the rise rule (#17) but transition="exit". The set tick is filtered
    # below the display floor by the adapter, so the falling edge never arrives as an upsert;
    # the engine fires this on the track's removal from live state (~valid_s, about 30 s after
    # the actual floor-crossing) as a single point-in-time alert. The ~30 s latency is inherent
    # and called out in the description. Watchlist-scoped; AOI/floor-bounded; disabled by default.
    (
        "rule-satellite-pass-end",
        AlertRuleCreate(
            name="Watched satellite pass end",
            severity="info",
            subject_types=["orbital_object"],
            conditions=[
                AlertCondition(
                    field="attributes.elevation_deg", operator="greater_than", value=10.0
                ),
                AlertCondition(field="watchlist", operator="watchlist"),
            ],
            enabled=False,
            transition="exit",
            channels=["dashboard", "browser"],
            description=(
                "A satellite on your watchlist has set below 10 deg elevation as seen from "
                "the home station — the pass has ended. Because the predicted position is "
                "dropped from the map the instant it falls below the display elevation floor, "
                "this fires when the track ages out of live state (~valid_s, about 30 s after "
                "the actual floor-crossing), not at the exact set instant; treat the timing as "
                "approximate. Position and elevation are SGP4-PREDICTED from the latest "
                "CelesTrak element sets (accuracy degrades with element age), not observed. "
                "Requires a configured station position and the satellite on your watchlist "
                "(orbital:celestrak:<norad>). Not authoritative for any operational use "
                "(PRD §32 #19)."
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

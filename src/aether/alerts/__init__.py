"""Alert subsystem (PRD §11.16, §20).

This package owns alert-rule *defaults* (:mod:`aether.alerts.templates`), the
stateless **condition-evaluation core** (:mod:`aether.alerts.conditions`) — dotted
field-path resolution + the level leaf operators — and the stateful **evaluation
engine** (:mod:`aether.alerts.engine`) that layers transition edges, cooldown,
dedup, schedule/quiet-hours, and lifecycle (open→acknowledged→resolved) on top of
that level predicate. Still to land in later M4 slices: the *contextual* operators
(geofence containment, distance/elevation, time windows, ``changed_*`` — M4.6c) and
the notification *delivery* drivers (email/Discord/browser — M4.7). Alert-rule
*model* + *persistence* live with the other config schemas
(:mod:`aether.schema.alert_rule`, :mod:`aether.persist.alert_rules`).
"""

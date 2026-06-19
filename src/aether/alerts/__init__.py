"""Alert subsystem (PRD §11.16, §20).

This package owns alert-rule *defaults* (:mod:`aether.alerts.templates`) and the
stateless **condition-evaluation core** (:mod:`aether.alerts.conditions`) — dotted
field-path resolution + the level leaf operators that the engine's AND-of-leaves
predicate is built from. The stateful evaluation *engine* (transition/cooldown/
dedup/lifecycle, the contextual operators) and the notification drivers land in
later M4 slices. Alert-rule *model* + *persistence* live with the other config
schemas (:mod:`aether.schema.alert_rule`, :mod:`aether.persist.alert_rules`).
"""

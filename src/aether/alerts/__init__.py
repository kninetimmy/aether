"""Alert subsystem (PRD §11.16, §20).

This package owns alert-rule *defaults* now (:mod:`aether.alerts.templates`); the
evaluation engine and notification drivers land in later M4 slices. Alert-rule
*model* + *persistence* live with the other config schemas
(:mod:`aether.schema.alert_rule`, :mod:`aether.persist.alert_rules`).
"""

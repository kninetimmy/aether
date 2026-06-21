// TypeScript mirror of the alert-rule config model (PRD §20.1, §11.16) and the
// /api/v2/alert-rules CRUD + /test contract (PRD §21.4). These shapes track
// src/aether/schema/alert_rule.py and the routers in
// src/aether/backend/alert_rules_api.py — keep them in sync when the model bumps.
//
// An alert rule is operator CONFIG (persisted in SQLite), not an observed record,
// so it lives here rather than in the record union (records.ts). The AlertRecord a
// rule raises is the record-union member; this file is only the rule itself plus
// the dry-run preview shape.

import type { Severity } from "./records";

/** Delivery channels (PRD §11.16 ALERT-FR-004, §20.4). */
export type AlertChannel = "dashboard" | "browser" | "email" | "discord";

/** Severity ladder — mirrors AlertRecord.severity (records.ts `Severity`). */
export type AlertSeverity = Severity;

/**
 * Condition operators (PRD §20.2). Grouped by the comparand each needs — the
 * editor uses `alertConditions.ts` to know which input to show and the backend
 * re-validates (HTTP 422) so a malformed rule never silently never-matches.
 */
export type ConditionOperator =
  | "equals"
  | "not_equals"
  | "in"
  | "not_in"
  | "greater_than"
  | "less_than"
  | "changed_to"
  | "changed_from"
  | "exists"
  | "not_exists"
  | "entered_geofence"
  | "exited_geofence"
  | "source_stale"
  | "source_offline"
  | "count_within_window"
  | "distance_below"
  | "distance_above"
  | "elevation_crossed"
  | "classification_basis"
  | "local_rf"
  | "watchlist";

/** A condition comparand: a JSON scalar or a list of scalars (for in/not_in). */
export type ConditionScalar = boolean | number | string;
export type ConditionValue = ConditionScalar | ConditionScalar[];

/** One predicate over a fused record (PRD §20.1 condition, §20.2 operators). */
export interface AlertCondition {
  field: string;
  operator: ConditionOperator;
  value?: ConditionValue | null;
  /** Numeric threshold for distance_* / elevation_crossed, and the count for count_within_window. */
  threshold?: number | null;
  /** Time window in seconds for count_within_window. */
  window_s?: number | null;
}

/** A daily HH:MM–HH:MM UTC window (PRD §20.5 quiet hours / schedule). */
export interface TimeWindow {
  start: string;
  end: string;
}

/** When a rule is active (PRD §11.16 ALERT-FR-003). days_of_week: 0=Monday…6=Sunday. */
export interface Schedule {
  days_of_week: number[];
  window?: TimeWindow | null;
}

export type AlertTransition = "enter" | "exit" | "change";

/** Fields shared by create/patch/stored shapes (the operator-editable surface). */
export interface AlertRuleFields {
  name: string;
  severity: AlertSeverity;
  subject_types: string[];
  conditions: AlertCondition[];
  enabled: boolean;
  transition?: AlertTransition | null;
  geofence_id?: string | null;
  cooldown_s: number;
  dedup_key?: string | null;
  channels: AlertChannel[];
  schedule?: Schedule | null;
  quiet_hours?: TimeWindow | null;
  description?: string | null;
}

/** Request body for POST /api/v2/alert-rules (server assigns id/timestamps). */
export type AlertRuleCreate = AlertRuleFields;

/**
 * Request body for PATCH /api/v2/alert-rules/{id} — every field optional. A field
 * left unset keeps its stored value; as on the backend, a nullable field cannot be
 * CLEARED to null via PATCH in this slice (null/absent means "unchanged").
 */
export type AlertRuleUpdate = Partial<AlertRuleFields>;

/** A stored alert rule — operator config with a stable id and audit timestamps. */
export interface AlertRule extends AlertRuleFields {
  id: string;
  created_at: string;
  updated_at: string;
}

/** Response for GET /api/v2/alert-rules. */
export interface AlertRuleList {
  count: number;
  alert_rules: AlertRule[];
}

/** One subject's current match in a rule dry-run (PRD §21.4 test). */
export interface RulePreviewMatch {
  subject_id: string | null;
  subject_type: string;
  /** null when the rule uses a contextual operator the preview core can't evaluate. */
  matched: boolean | null;
}

/**
 * Response for POST /api/v2/alert-rules/{id}/test — a side-effect-free dry run
 * against current live state (no firing, no state change). `evaluable` is false
 * when a contextual operator can't be previewed, in which case each `matched` is
 * null (honest "unknown", never a misleading false — PRD §37).
 */
export interface RulePreview {
  rule_id: string;
  evaluable: boolean;
  subject_types: string[];
  evaluated: number;
  matched: number;
  matches: RulePreviewMatch[];
}

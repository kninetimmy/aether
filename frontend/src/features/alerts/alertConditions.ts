// Condition-editor support: operator → comparand shape, and form ⇄ AlertCondition
// conversion (PRD §20.2). Pure and unit-tested; the editor (AlertRuleEditor) stays
// presentational and leans on this for "which input does this operator need" and
// "turn these form strings into a typed condition" (or a clear error).
//
// TYPING MATTERS. The engine compares a rule's comparand against the DUMPED JSON
// value (`value == condition.value`, conditions.py) — so a squawk stored as the
// string "7700" only matches a string comparand "7700", never the number 7700.
// Rather than guess from the text, every value-bearing condition carries an explicit
// value TYPE (string/number/boolean); the operator forces `number` where the model
// demands it (greater_than/less_than). This keeps the rule honest about what it
// compares instead of silently coercing and never matching.

import type {
  AlertCondition,
  ConditionOperator,
  ConditionScalar,
} from "../../types/alertRules";

/** The comparand shape an operator needs — drives which inputs the editor shows. */
export type ComparandKind =
  | "none" // no comparand (exists, geofence, source_*, local_rf, watchlist)
  | "scalar" // a single typed value (equals, changed_to, …)
  | "numeric" // a single numeric value (greater_than, less_than)
  | "list" // a non-empty typed list (in, not_in)
  | "threshold" // a numeric threshold (distance_*, elevation_crossed)
  | "count"; // a numeric count + a window in seconds (count_within_window)

/** Explicit comparand value type — the operator forces `number` where required. */
export type ValueType = "string" | "number" | "boolean";

interface OperatorMeta {
  kind: ComparandKind;
  /** Human label for the operator <select>. */
  label: string;
}

/**
 * Per-operator metadata. The `kind` mirrors the comparand groupings the backend
 * model enforces (schema/alert_rule.py): list operators need a non-empty list,
 * comparand operators need a scalar, greater/less need a numeric scalar, the
 * distance/elevation operators need a threshold, count needs threshold+window, and
 * the rest take no comparand. Insertion order here is the order shown in the editor.
 *
 * `local_rf`/`watchlist` take an OPTIONAL bool comparand on the backend (default
 * true = "is local-RF / is watchlisted"); this slice models them as `none` and lets
 * them default to true — the common case — deferring the explicit-false variant.
 */
export const OPERATOR_META: Record<ConditionOperator, OperatorMeta> = {
  equals: { kind: "scalar", label: "equals" },
  not_equals: { kind: "scalar", label: "not equals" },
  in: { kind: "list", label: "in (any of)" },
  not_in: { kind: "list", label: "not in" },
  greater_than: { kind: "numeric", label: "greater than" },
  less_than: { kind: "numeric", label: "less than" },
  changed_to: { kind: "scalar", label: "changed to" },
  changed_from: { kind: "scalar", label: "changed from" },
  exists: { kind: "none", label: "exists" },
  not_exists: { kind: "none", label: "does not exist" },
  entered_geofence: { kind: "none", label: "entered geofence" },
  exited_geofence: { kind: "none", label: "exited geofence" },
  source_stale: { kind: "none", label: "source stale" },
  source_offline: { kind: "none", label: "source offline" },
  count_within_window: { kind: "count", label: "count within window" },
  distance_below: { kind: "threshold", label: "distance below (m)" },
  distance_above: { kind: "threshold", label: "distance above (m)" },
  elevation_crossed: { kind: "threshold", label: "elevation crossed (°)" },
  classification_basis: { kind: "scalar", label: "classification basis" },
  local_rf: { kind: "none", label: "locally received" },
  watchlist: { kind: "none", label: "on watchlist" },
};

/** Operators in editor display order. */
export const OPERATORS: ConditionOperator[] = Object.keys(
  OPERATOR_META,
) as ConditionOperator[];

export function comparandKind(op: ConditionOperator): ComparandKind {
  return OPERATOR_META[op].kind;
}

/** String-backed form state for one condition row (the editor binds inputs to this). */
export interface ConditionForm {
  field: string;
  operator: ConditionOperator;
  /** Comparand text — a scalar, or comma-separated for a list operator. */
  valueText: string;
  /** How to interpret valueText; ignored for none/threshold/count kinds. */
  valueType: ValueType;
  /** Threshold text for threshold/count operators. */
  thresholdText: string;
  /** Window-seconds text for the count operator. */
  windowText: string;
}

export function emptyConditionForm(): ConditionForm {
  return {
    field: "",
    operator: "equals",
    valueText: "",
    valueType: "string",
    thresholdText: "",
    windowText: "",
  };
}

/** Coerce one comparand token to its typed scalar; throws on a malformed value. */
function coerceScalar(text: string, type: ValueType): ConditionScalar {
  const trimmed = text.trim();
  if (type === "number") {
    const n = Number(trimmed);
    if (trimmed === "" || !Number.isFinite(n)) {
      throw new Error(`"${text}" is not a number`);
    }
    return n;
  }
  if (type === "boolean") {
    if (trimmed === "true") return true;
    if (trimmed === "false") return false;
    throw new Error(`"${text}" is not true/false`);
  }
  return text; // string: preserve verbatim (don't trim — values can be space-bearing)
}

function parseNumber(text: string, label: string): number {
  const n = Number(text.trim());
  if (text.trim() === "" || !Number.isFinite(n)) {
    throw new Error(`${label} must be a number`);
  }
  return n;
}

/**
 * Build a typed {@link AlertCondition} from a form row, or throw an Error whose
 * message the editor surfaces inline. Mirrors the backend's per-operator comparand
 * rules so an obviously-bad row is caught before the round-trip (the server
 * re-validates and would 422 anyway).
 */
export function buildCondition(form: ConditionForm): AlertCondition {
  const field = form.field.trim();
  if (!field) throw new Error("field is required");
  const kind = comparandKind(form.operator);
  const base: AlertCondition = { field, operator: form.operator };

  if (kind === "none") return base;

  if (kind === "list") {
    const tokens = form.valueText
      .split(",")
      .map((t) => t.trim())
      .filter((t) => t.length > 0);
    if (tokens.length === 0) {
      throw new Error("list operators need at least one value");
    }
    return { ...base, value: tokens.map((t) => coerceScalar(t, form.valueType)) };
  }

  if (kind === "scalar") {
    if (form.valueText.trim() === "") throw new Error("a value is required");
    return { ...base, value: coerceScalar(form.valueText, form.valueType) };
  }

  if (kind === "numeric") {
    return { ...base, value: parseNumber(form.valueText, "value") };
  }

  if (kind === "threshold") {
    return { ...base, threshold: parseNumber(form.thresholdText, "threshold") };
  }

  // count: threshold (the count) + window_s
  return {
    ...base,
    threshold: parseNumber(form.thresholdText, "count"),
    window_s: parseNumber(form.windowText, "window seconds"),
  };
}

/** Infer the value type of an existing comparand for round-tripping into the form. */
function valueTypeOf(value: AlertCondition["value"]): ValueType {
  const sample = Array.isArray(value) ? value[0] : value;
  if (typeof sample === "boolean") return "boolean";
  if (typeof sample === "number") return "number";
  return "string";
}

/** Turn a stored condition back into editable form state (the inverse of buildCondition). */
export function conditionToForm(c: AlertCondition): ConditionForm {
  const valueText = Array.isArray(c.value)
    ? c.value.map((v) => String(v)).join(", ")
    : c.value === undefined || c.value === null
      ? ""
      : String(c.value);
  return {
    field: c.field,
    operator: c.operator,
    valueText,
    valueType: valueTypeOf(c.value),
    thresholdText: c.threshold === undefined || c.threshold === null ? "" : String(c.threshold),
    windowText: c.window_s === undefined || c.window_s === null ? "" : String(c.window_s),
  };
}

// Alert-rule editor (PRD §11 "Alert rules shall be editable through the UI", §20.1,
// §21.4): the CONFIG surface for alert rules — list / create / edit / delete plus a
// side-effect-free dry-run (/test). This is the operator's authoring tool; the
// firing alerts it produces are shown by AlertsPanel (a separate, operational panel).
//
// Persistence-aware and honest (PRD §37): alert rules live in SQLite, so when
// persistence is off the CRUD API answers 503. Rather than show an empty list (which
// would read as "no rules"), the panel says persistence is required and offers retry.
// Every API failure is surfaced inline; the COP never crashes on a failed action.
//
// All comparand typing/validation lives in the pure alertConditions helpers; this
// component stays presentational and leans on them for "which input does this operator
// need" and "turn these form strings into a typed condition (or a clear error)". The
// backend re-validates (422), so an obviously-bad rule is caught here AND there.

import { useCallback, useEffect, useState } from "react";
import {
  AlertApiError,
  createAlertRule,
  deleteAlertRule,
  listAlertRules,
  testAlertRule,
  updateAlertRule,
} from "../../api/alertsClient";
import type {
  AlertChannel,
  AlertRule,
  AlertRuleFields,
  AlertSeverity,
  AlertTransition,
  RulePreview,
} from "../../types/alertRules";
import {
  OPERATOR_META,
  OPERATORS,
  buildCondition,
  comparandKind,
  conditionToForm,
  emptyConditionForm,
  type ConditionForm,
  type ValueType,
} from "./alertConditions";

const SEVERITIES: AlertSeverity[] = ["info", "low", "medium", "high", "critical"];
const CHANNELS: AlertChannel[] = ["dashboard", "browser", "email", "discord"];
const TRANSITIONS: AlertTransition[] = ["enter", "exit", "change"];
const VALUE_TYPES: ValueType[] = ["string", "number", "boolean"];

// Common subject-type tokens (engine matches a rule's subject_types against a track's
// track_type, the literal "source" for source-status, or an event's event_type —
// engine.subject_type_of). These are quick-add suggestions only; the field stays
// free-form so a future record/event type is still addressable (PRD §20.1).
const SUBJECT_SUGGESTIONS = [
  "aircraft",
  "vessel",
  "aprs_station",
  "aprs_object",
  "radiosonde",
  "orbital_object",
  "source",
];

/** String-backed editor state for one rule (the inverse maps in formFromRule/buildBody). */
interface RuleFormState {
  name: string;
  severity: AlertSeverity;
  subjectTypesText: string; // comma-separated tokens
  enabled: boolean;
  conditions: ConditionForm[];
  transition: "" | AlertTransition; // "" = none
  geofenceId: string;
  cooldownText: string;
  dedupKey: string;
  channels: Set<AlertChannel>;
  description: string;
}

function newForm(): RuleFormState {
  return {
    name: "",
    severity: "medium",
    subjectTypesText: "",
    enabled: true,
    conditions: [emptyConditionForm()],
    transition: "",
    geofenceId: "",
    cooldownText: "900",
    dedupKey: "",
    channels: new Set<AlertChannel>(["dashboard"]),
    description: "",
  };
}

function formFromRule(rule: AlertRule): RuleFormState {
  return {
    name: rule.name,
    severity: rule.severity,
    subjectTypesText: rule.subject_types.join(", "),
    enabled: rule.enabled,
    conditions: rule.conditions.length
      ? rule.conditions.map(conditionToForm)
      : [emptyConditionForm()],
    transition: rule.transition ?? "",
    geofenceId: rule.geofence_id ?? "",
    cooldownText: String(rule.cooldown_s),
    dedupKey: rule.dedup_key ?? "",
    channels: new Set(rule.channels),
    description: rule.description ?? "",
  };
}

/**
 * Turn editor state into the full rule body, or throw an Error whose message is shown
 * inline. The full shape doubles as a PATCH: nullable fields left blank serialize to
 * null, which the backend reads as "unchanged" (so editing never clobbers a stored
 * schedule/quiet_hours, which this slice doesn't yet edit — that, and clearing a
 * nullable field back to null, are deferred follow-ups, matching the backend's note).
 */
function buildBody(form: RuleFormState): AlertRuleFields {
  const name = form.name.trim();
  if (!name) throw new Error("name is required");

  const subject_types = form.subjectTypesText
    .split(",")
    .map((s) => s.trim())
    .filter((s) => s.length > 0);
  if (subject_types.length === 0) throw new Error("at least one subject type is required");

  const conditions = form.conditions.map((c, i) => {
    try {
      return buildCondition(c);
    } catch (err) {
      throw new Error(`condition ${i + 1}: ${err instanceof Error ? err.message : String(err)}`);
    }
  });

  const channels = [...form.channels];
  if (channels.length === 0) throw new Error("select at least one channel");

  const cooldown_s = Number(form.cooldownText.trim());
  if (form.cooldownText.trim() === "" || !Number.isFinite(cooldown_s) || cooldown_s < 0) {
    throw new Error("cooldown must be a number ≥ 0 seconds");
  }

  return {
    name,
    severity: form.severity,
    subject_types,
    conditions,
    enabled: form.enabled,
    transition: form.transition === "" ? null : form.transition,
    geofence_id: form.geofenceId.trim() || null,
    cooldown_s,
    dedup_key: form.dedupKey.trim() || null,
    channels,
    schedule: null, // schedule/quiet_hours editing deferred; null = "unchanged" on PATCH
    quiet_hours: null,
    description: form.description.trim() || null,
  };
}

/** Human one-liner for an API error (persistence-off gets a specific, actionable line). */
function apiErrorMessage(err: unknown): string {
  if (err instanceof AlertApiError && err.status === 503) {
    return "Alert rules need persistence enabled (AETHER_PERSIST).";
  }
  if (err instanceof Error) return err.message;
  return String(err);
}

export function AlertRuleEditor() {
  const [rules, setRules] = useState<AlertRule[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [editing, setEditing] = useState<AlertRule | "new" | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [previews, setPreviews] = useState<Record<string, RulePreview | string>>({});

  const load = useCallback(async () => {
    setLoadError(null);
    try {
      const res = await listAlertRules();
      setRules(res.alert_rules);
    } catch (err) {
      setRules(null);
      setLoadError(apiErrorMessage(err));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function onDelete(rule: AlertRule): Promise<void> {
    setBusyId(rule.id);
    setActionError(null);
    try {
      await deleteAlertRule(rule.id);
      await load();
    } catch (err) {
      setActionError(`Delete failed: ${apiErrorMessage(err)}`);
    } finally {
      setBusyId(null);
    }
  }

  async function onToggleEnabled(rule: AlertRule): Promise<void> {
    setBusyId(rule.id);
    setActionError(null);
    try {
      await updateAlertRule(rule.id, { enabled: !rule.enabled });
      await load();
    } catch (err) {
      setActionError(`Update failed: ${apiErrorMessage(err)}`);
    } finally {
      setBusyId(null);
    }
  }

  async function onTest(rule: AlertRule): Promise<void> {
    setBusyId(rule.id);
    setActionError(null);
    try {
      const preview = await testAlertRule(rule.id);
      setPreviews((p) => ({ ...p, [rule.id]: preview }));
    } catch (err) {
      setPreviews((p) => ({ ...p, [rule.id]: `test failed: ${apiErrorMessage(err)}` }));
    } finally {
      setBusyId(null);
    }
  }

  return (
    <section className="panel-section" aria-label="Alert rules">
      <h2>
        Alert rules
        {rules && <span className="count">{rules.length}</span>}
        <button
          type="button"
          className="link"
          disabled={editing !== null}
          onClick={() => setEditing("new")}
        >
          + new
        </button>
      </h2>

      {loadError && (
        <div className="rule-loaderror" role="alert">
          <p>{loadError}</p>
          <button type="button" className="link" onClick={() => void load()}>
            retry
          </button>
        </div>
      )}
      {actionError && (
        <p className="alert-error" role="alert">
          {actionError}
        </p>
      )}

      {editing !== null && (
        <RuleForm
          rule={editing === "new" ? null : editing}
          onClose={() => setEditing(null)}
          onSaved={() => {
            setEditing(null);
            void load();
          }}
        />
      )}

      {rules && rules.length === 0 && !loadError && editing === null && (
        <p className="muted">No alert rules yet.</p>
      )}

      <ul className="rule-list">
        {rules?.map((rule) => {
          const busy = busyId === rule.id;
          const preview = previews[rule.id];
          return (
            <li key={rule.id} className={`rule-row${rule.enabled ? "" : " disabled"}`}>
              <div className="rule-head">
                <span className={`rule-sev sev-${rule.severity}`}>{rule.severity}</span>
                <span className="rule-name" title={rule.name}>
                  {rule.name}
                </span>
                {!rule.enabled && <span className="rule-off">off</span>}
              </div>
              <div className="rule-meta muted">
                {rule.subject_types.join(", ")} · {rule.conditions.length}{" "}
                {rule.conditions.length === 1 ? "condition" : "conditions"} ·{" "}
                {rule.channels.join("/")}
              </div>
              <div className="rule-actions">
                <button type="button" disabled={busy} onClick={() => void onTest(rule)}>
                  Test
                </button>
                <button
                  type="button"
                  disabled={busy || editing !== null}
                  onClick={() => setEditing(rule)}
                >
                  Edit
                </button>
                <button type="button" disabled={busy} onClick={() => void onToggleEnabled(rule)}>
                  {rule.enabled ? "Disable" : "Enable"}
                </button>
                <button
                  type="button"
                  className="rule-delete"
                  disabled={busy}
                  onClick={() => void onDelete(rule)}
                >
                  Delete
                </button>
              </div>
              {preview !== undefined && (
                <div className="rule-preview" role="status">
                  {typeof preview === "string" ? preview : describePreview(preview)}
                </div>
              )}
            </li>
          );
        })}
      </ul>
    </section>
  );
}

/** Honest one-line preview summary — never reports a contextual operator as a false. */
function describePreview(p: RulePreview): string {
  if (!p.evaluable) {
    return `Not previewable now — uses a contextual operator (evaluated against ${p.evaluated} subject${p.evaluated === 1 ? "" : "s"}).`;
  }
  return `Dry run: ${p.matched} of ${p.evaluated} current subject${p.evaluated === 1 ? "" : "s"} match.`;
}

/** The create/edit form. `rule === null` is create; otherwise edit that rule. */
function RuleForm({
  rule,
  onClose,
  onSaved,
}: {
  rule: AlertRule | null;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [form, setForm] = useState<RuleFormState>(() => (rule ? formFromRule(rule) : newForm()));
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  function patch(over: Partial<RuleFormState>): void {
    setForm((f) => ({ ...f, ...over }));
  }

  function setCondition(i: number, over: Partial<ConditionForm>): void {
    setForm((f) => ({
      ...f,
      conditions: f.conditions.map((c, j) => (j === i ? { ...c, ...over } : c)),
    }));
  }

  function addCondition(): void {
    setForm((f) => ({ ...f, conditions: [...f.conditions, emptyConditionForm()] }));
  }

  function removeCondition(i: number): void {
    setForm((f) => ({
      ...f,
      // Keep at least one row — a rule needs ≥1 condition (backend min_length=1).
      conditions: f.conditions.length > 1 ? f.conditions.filter((_, j) => j !== i) : f.conditions,
    }));
  }

  function toggleChannel(ch: AlertChannel): void {
    setForm((f) => {
      const next = new Set(f.channels);
      if (next.has(ch)) next.delete(ch);
      else next.add(ch);
      return { ...f, channels: next };
    });
  }

  function addSubject(token: string): void {
    setForm((f) => {
      const have = f.subjectTypesText
        .split(",")
        .map((s) => s.trim())
        .filter((s) => s.length > 0);
      if (have.includes(token)) return f;
      return { ...f, subjectTypesText: [...have, token].join(", ") };
    });
  }

  async function save(): Promise<void> {
    setError(null);
    let body: AlertRuleFields;
    try {
      body = buildBody(form);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      return;
    }
    setSaving(true);
    try {
      if (rule) await updateAlertRule(rule.id, body);
      else await createAlertRule(body);
      onSaved();
    } catch (err) {
      setError(apiErrorMessage(err));
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="rule-form" role="form" aria-label={rule ? "Edit alert rule" : "New alert rule"}>
      <label className="rule-field">
        <span>Name</span>
        <input
          type="text"
          value={form.name}
          onChange={(e) => patch({ name: e.target.value })}
          placeholder="e.g. Emergency squawk"
        />
      </label>

      <label className="rule-field">
        <span>Severity</span>
        <select
          value={form.severity}
          onChange={(e) => patch({ severity: e.target.value as AlertSeverity })}
        >
          {SEVERITIES.map((s) => (
            <option key={s} value={s}>
              {s}
            </option>
          ))}
        </select>
      </label>

      <label className="rule-field">
        <span>Subject types</span>
        <input
          type="text"
          value={form.subjectTypesText}
          onChange={(e) => patch({ subjectTypesText: e.target.value })}
          placeholder="aircraft, vessel, …"
        />
      </label>
      <div className="rule-suggestions">
        {SUBJECT_SUGGESTIONS.map((t) => (
          <button key={t} type="button" className="chip" onClick={() => addSubject(t)}>
            +{t}
          </button>
        ))}
      </div>

      <fieldset className="rule-conditions">
        <legend>Conditions (all must match)</legend>
        {form.conditions.map((c, i) => (
          <ConditionRow
            key={i}
            cond={c}
            onChange={(over) => setCondition(i, over)}
            onRemove={form.conditions.length > 1 ? () => removeCondition(i) : null}
          />
        ))}
        <button type="button" className="link" onClick={addCondition}>
          + condition
        </button>
      </fieldset>

      <fieldset className="rule-channels">
        <legend>Channels</legend>
        {CHANNELS.map((ch) => (
          <label key={ch} className="filter-chip">
            <input
              type="checkbox"
              checked={form.channels.has(ch)}
              onChange={() => toggleChannel(ch)}
            />
            <span>{ch}</span>
          </label>
        ))}
      </fieldset>

      <label className="rule-field">
        <span>Transition</span>
        <select
          value={form.transition}
          onChange={(e) => patch({ transition: e.target.value as "" | AlertTransition })}
        >
          <option value="">none (level)</option>
          {TRANSITIONS.map((t) => (
            <option key={t} value={t}>
              {t}
            </option>
          ))}
        </select>
      </label>

      <label className="rule-field">
        <span>Geofence id</span>
        <input
          type="text"
          value={form.geofenceId}
          onChange={(e) => patch({ geofenceId: e.target.value })}
          placeholder="(optional)"
        />
      </label>

      <label className="rule-field">
        <span>Cooldown (s)</span>
        <input
          type="number"
          min={0}
          value={form.cooldownText}
          onChange={(e) => patch({ cooldownText: e.target.value })}
        />
      </label>

      <label className="rule-field">
        <span>Dedup key</span>
        <input
          type="text"
          value={form.dedupKey}
          onChange={(e) => patch({ dedupKey: e.target.value })}
          placeholder="(optional)"
        />
      </label>

      <label className="rule-field">
        <span>Description</span>
        <textarea
          value={form.description}
          onChange={(e) => patch({ description: e.target.value })}
          placeholder="(optional)"
          rows={2}
        />
      </label>

      <label className="rule-row-inline">
        <input
          type="checkbox"
          checked={form.enabled}
          onChange={(e) => patch({ enabled: e.target.checked })}
        />
        <span>Enabled</span>
      </label>

      {error && (
        <p className="alert-error" role="alert">
          {error}
        </p>
      )}

      <div className="rule-form-actions">
        <button type="button" onClick={onClose} disabled={saving}>
          Cancel
        </button>
        <button
          type="button"
          className="rule-save"
          onClick={() => void save()}
          disabled={saving}
        >
          {saving ? "Saving…" : rule ? "Save changes" : "Create rule"}
        </button>
      </div>
    </div>
  );
}

/** One condition row — the visible comparand inputs follow the operator's comparand kind. */
function ConditionRow({
  cond,
  onChange,
  onRemove,
}: {
  cond: ConditionForm;
  onChange: (over: Partial<ConditionForm>) => void;
  onRemove: (() => void) | null;
}) {
  const kind = comparandKind(cond.operator);
  const showValueType = kind === "scalar" || kind === "list";
  return (
    <div className="cond-row">
      <input
        type="text"
        className="cond-field"
        value={cond.field}
        onChange={(e) => onChange({ field: e.target.value })}
        placeholder="field (e.g. squawk)"
        aria-label="condition field"
      />
      <select
        className="cond-op"
        value={cond.operator}
        onChange={(e) => onChange({ operator: e.target.value as ConditionForm["operator"] })}
        aria-label="condition operator"
      >
        {OPERATORS.map((op) => (
          <option key={op} value={op}>
            {OPERATOR_META[op].label}
          </option>
        ))}
      </select>

      {(kind === "scalar" || kind === "numeric" || kind === "list") && (
        <input
          type="text"
          className="cond-value"
          value={cond.valueText}
          onChange={(e) => onChange({ valueText: e.target.value })}
          placeholder={kind === "list" ? "a, b, c" : "value"}
          aria-label="condition value"
        />
      )}
      {showValueType && (
        <select
          className="cond-type"
          value={cond.valueType}
          onChange={(e) => onChange({ valueType: e.target.value as ValueType })}
          aria-label="condition value type"
        >
          {VALUE_TYPES.map((t) => (
            <option key={t} value={t}>
              {t}
            </option>
          ))}
        </select>
      )}
      {(kind === "threshold" || kind === "count") && (
        <input
          type="number"
          className="cond-threshold"
          value={cond.thresholdText}
          onChange={(e) => onChange({ thresholdText: e.target.value })}
          placeholder={kind === "count" ? "count" : "threshold"}
          aria-label="condition threshold"
        />
      )}
      {kind === "count" && (
        <input
          type="number"
          className="cond-window"
          value={cond.windowText}
          onChange={(e) => onChange({ windowText: e.target.value })}
          placeholder="window s"
          aria-label="condition window seconds"
        />
      )}

      {onRemove && (
        <button type="button" className="cond-remove" onClick={onRemove} aria-label="remove condition">
          ×
        </button>
      )}
    </div>
  );
}

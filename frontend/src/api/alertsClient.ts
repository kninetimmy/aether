// Typed REST client for the alerts domain — alert-rule CRUD + dry-run (PRD §21.4,
// §11.16) and the live-alert lifecycle ack/resolve (PRD §21.4, §20.5).
//
// This is the ONLY place the `/api/v2/alert-rules` and `/api/v2/alerts` endpoints
// are spoken to. Two REST surfaces, one domain:
//   - alert-rules: operator CONFIG persisted in SQLite. CRUD returns 503 when
//     persistence is disabled (AETHER_PERSIST off) — surfaced as AlertApiError so
//     the panel can say so honestly rather than showing an empty list.
//   - alerts: the LIVE alerts the engine raised. ack/resolve transition one in
//     place; the server rebroadcasts the updated record over /ws/v2, so callers do
//     NOT mutate the store — they await the request and let the ws delta land.
//
// Errors are surfaced as a typed AlertApiError carrying the HTTP status (0 =
// transport/parse failure), mirroring replayClient.ts so a caller's catch is
// exhaustive and never sees an untyped throw.

import type {
  AlertRule,
  AlertRuleCreate,
  AlertRuleList,
  AlertRuleUpdate,
  RulePreview,
} from "../types/alertRules";
import type { AlertRecord } from "../types/records";

/** Base paths (mounted in src/aether/backend/main.py). */
const RULES_BASE = "/api/v2/alert-rules";
const ALERTS_BASE = "/api/v2/alerts";

/** A typed alerts-API failure carrying the HTTP status (0 = transport/parse error). */
export class AlertApiError extends Error {
  constructor(
    message: string,
    /** HTTP status, or 0 when the request never completed / response was unparseable. */
    readonly status: number,
  ) {
    super(message);
    this.name = "AlertApiError";
  }
}

/** Pull a human detail out of a non-ok JSON body ({detail: ...}); best-effort. */
async function errorDetail(res: Response): Promise<string> {
  try {
    const body: unknown = await res.json();
    if (
      body !== null &&
      typeof body === "object" &&
      "detail" in body &&
      typeof (body as { detail: unknown }).detail === "string"
    ) {
      return (body as { detail: string }).detail;
    }
  } catch {
    // Non-JSON / empty body — fall back to the status text below.
  }
  return res.statusText || `HTTP ${res.status}`;
}

/**
 * Perform a JSON request and parse the body, mapping every failure mode onto an
 * AlertApiError: a transport/abort failure (status 0), a non-2xx (the HTTP status +
 * server detail), or an unparseable body (status 0). `expectBody=false` skips the
 * parse for 204 responses (DELETE).
 */
async function request<T>(
  url: string,
  init: RequestInit,
  expectBody = true,
): Promise<T> {
  let res: Response;
  try {
    res = await fetch(url, init);
  } catch (err) {
    throw new AlertApiError(
      `request failed: ${err instanceof Error ? err.message : String(err)}`,
      0,
    );
  }
  if (!res.ok) {
    throw new AlertApiError(await errorDetail(res), res.status);
  }
  if (!expectBody) return undefined as T;
  try {
    return (await res.json()) as T;
  } catch (err) {
    throw new AlertApiError(
      `response was not valid JSON: ${err instanceof Error ? err.message : String(err)}`,
      0,
    );
  }
}

const JSON_HEADERS = { "Content-Type": "application/json" } as const;

// --- Alert-rule CRUD + dry-run (config; 503 when persistence is off) ----------

/** List every alert rule. Throws AlertApiError (e.g. 503 persistence-off). */
export async function listAlertRules(): Promise<AlertRuleList> {
  return request<AlertRuleList>(RULES_BASE, { method: "GET" });
}

/** Create a rule; returns the stored rule (with server id + timestamps). */
export async function createAlertRule(body: AlertRuleCreate): Promise<AlertRule> {
  return request<AlertRule>(RULES_BASE, {
    method: "POST",
    headers: JSON_HEADERS,
    body: JSON.stringify(body),
  });
}

/** Patch a rule (only the fields present are changed); returns the updated rule. */
export async function updateAlertRule(
  id: string,
  patch: AlertRuleUpdate,
): Promise<AlertRule> {
  return request<AlertRule>(`${RULES_BASE}/${encodeURIComponent(id)}`, {
    method: "PATCH",
    headers: JSON_HEADERS,
    body: JSON.stringify(patch),
  });
}

/** Delete a rule. Resolves on 204; throws AlertApiError on 404/503/transport. */
export async function deleteAlertRule(id: string): Promise<void> {
  await request<void>(
    `${RULES_BASE}/${encodeURIComponent(id)}`,
    { method: "DELETE" },
    false,
  );
}

/**
 * Dry-run a rule against current live state (no firing, no state change, PRD §21.4).
 * Reports which current subjects the rule matches right now, or evaluable=false when
 * a contextual operator can't be previewed.
 */
export async function testAlertRule(id: string): Promise<RulePreview> {
  return request<RulePreview>(`${RULES_BASE}/${encodeURIComponent(id)}/test`, {
    method: "POST",
  });
}

// --- Live-alert lifecycle (in-memory; no persistence dependency) --------------

/**
 * Acknowledge a live alert and return the transitioned record. The server also
 * rebroadcasts it over /ws/v2, so the caller need not touch the store — the
 * alert_upsert delta updates live state. Throws AlertApiError on 404 (no live
 * alert) / transport.
 */
export async function acknowledgeAlert(id: string): Promise<AlertRecord> {
  return request<AlertRecord>(
    `${ALERTS_BASE}/${encodeURIComponent(id)}/acknowledge`,
    { method: "POST" },
  );
}

/** Resolve a live alert and return the transitioned record (see acknowledgeAlert). */
export async function resolveAlert(id: string): Promise<AlertRecord> {
  return request<AlertRecord>(
    `${ALERTS_BASE}/${encodeURIComponent(id)}/resolve`,
    { method: "POST" },
  );
}

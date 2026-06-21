import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  AlertApiError,
  acknowledgeAlert,
  createAlertRule,
  deleteAlertRule,
  listAlertRules,
  resolveAlert,
  testAlertRule,
  updateAlertRule,
} from "./alertsClient";
import type { AlertRuleCreate } from "../types/alertRules";

// Mock global fetch: exercise the client without a backend, asserting the request
// shape (method/url/body) and the error mapping (status → AlertApiError) — the
// contract the alerts UI relies on (PRD §21.4).

function jsonResponse(body: unknown, init: { status?: number } = {}) {
  const status = init.status ?? 200;
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: `HTTP ${status}`,
    json: async () => body,
  } as unknown as Response;
}

const CREATE_BODY: AlertRuleCreate = {
  name: "Emergency squawk",
  severity: "high",
  subject_types: ["aircraft"],
  conditions: [{ field: "attributes.squawk", operator: "in", value: ["7500", "7600", "7700"] }],
  enabled: true,
  cooldown_s: 900,
  channels: ["dashboard"],
};

let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  fetchMock = vi.fn();
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("alert-rule CRUD", () => {
  it("GETs the rule list", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ count: 0, alert_rules: [] }));
    const out = await listAlertRules();
    expect(out).toEqual({ count: 0, alert_rules: [] });
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/v2/alert-rules");
    expect(init.method).toBe("GET");
  });

  it("POSTs a create body to /api/v2/alert-rules", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ ...CREATE_BODY, id: "rule-1" }, { status: 201 }));
    const out = await createAlertRule(CREATE_BODY);
    expect(out.id).toBe("rule-1");
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/v2/alert-rules");
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body)).toEqual(CREATE_BODY);
  });

  it("PATCHes a rule by id (url-encoded)", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ ...CREATE_BODY, id: "r 1" }));
    await updateAlertRule("r 1", { enabled: false });
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/v2/alert-rules/r%201");
    expect(init.method).toBe("PATCH");
    expect(JSON.parse(init.body)).toEqual({ enabled: false });
  });

  it("DELETEs a rule and resolves on 204 (no body parse)", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(null, { status: 204 }));
    await expect(deleteAlertRule("rule-1")).resolves.toBeUndefined();
    expect(fetchMock.mock.calls[0][1].method).toBe("DELETE");
  });

  it("POSTs /test and returns the preview", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({
        rule_id: "rule-1",
        evaluable: true,
        subject_types: ["aircraft"],
        evaluated: 2,
        matched: 1,
        matches: [],
      }),
    );
    const out = await testAlertRule("rule-1");
    expect(out.matched).toBe(1);
    expect(fetchMock.mock.calls[0][0]).toBe("/api/v2/alert-rules/rule-1/test");
  });

  it("maps a 503 (persistence off) to an AlertApiError carrying the detail", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({ detail: "persistence disabled; alert rules unavailable" }, { status: 503 }),
    );
    const err = await listAlertRules().catch((e) => e);
    expect(err).toBeInstanceOf(AlertApiError);
    expect(err.status).toBe(503);
    expect(err.message).toMatch(/persistence disabled/);
  });

  it("maps a 422 (bad condition) to an AlertApiError", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ detail: "bad rule" }, { status: 422 }));
    const err = await createAlertRule(CREATE_BODY).catch((e) => e);
    expect(err).toBeInstanceOf(AlertApiError);
    expect(err.status).toBe(422);
  });

  it("maps a transport failure to status 0", async () => {
    fetchMock.mockRejectedValueOnce(new Error("network down"));
    const err = await listAlertRules().catch((e) => e);
    expect(err).toBeInstanceOf(AlertApiError);
    expect(err.status).toBe(0);
  });
});

describe("alert lifecycle", () => {
  it("POSTs acknowledge by id", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ id: "al-1", state: "acknowledged" }));
    await acknowledgeAlert("al-1");
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/v2/alerts/al-1/acknowledge");
    expect(init.method).toBe("POST");
  });

  it("POSTs resolve by id", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ id: "al-1", state: "resolved" }));
    await resolveAlert("al-1");
    expect(fetchMock.mock.calls[0][0]).toBe("/api/v2/alerts/al-1/resolve");
  });

  it("maps a 404 (no live alert) to an AlertApiError", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ detail: "no live alert" }, { status: 404 }));
    const err = await acknowledgeAlert("gone").catch((e) => e);
    expect(err).toBeInstanceOf(AlertApiError);
    expect(err.status).toBe(404);
  });
});

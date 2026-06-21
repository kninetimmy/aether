import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { AlertRuleEditor } from "./AlertRuleEditor";
import type { AlertRule } from "../../types/alertRules";

// jsdom client render (mirrors AlertsPanel.test.tsx). Load happens in an effect, so
// helpers below flush the fetch→json→setState microtask chain before asserting.
(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT =
  true;

const T0 = "2026-06-20T00:00:00.000Z";

function rule(over: Partial<AlertRule> = {}): AlertRule {
  return {
    id: "r-1",
    name: "Emergency squawk",
    severity: "critical",
    subject_types: ["aircraft"],
    conditions: [{ field: "squawk", operator: "in", value: ["7500", "7600", "7700"] }],
    enabled: true,
    transition: null,
    geofence_id: null,
    cooldown_s: 900,
    dedup_key: null,
    channels: ["dashboard"],
    schedule: null,
    quiet_hours: null,
    description: null,
    created_at: T0,
    updated_at: T0,
    ...over,
  };
}

function jsonResponse(body: unknown, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: `HTTP ${status}`,
    json: async () => body,
  } as unknown as Response;
}

/** Drive a controlled input/select/textarea past React's value tracker. */
function setValue(
  el: HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement,
  value: string,
): void {
  const proto =
    el instanceof HTMLTextAreaElement
      ? HTMLTextAreaElement.prototype
      : el instanceof HTMLSelectElement
        ? HTMLSelectElement.prototype
        : HTMLInputElement.prototype;
  const setter = Object.getOwnPropertyDescriptor(proto, "value")!.set!;
  act(() => {
    setter.call(el, value);
    el.dispatchEvent(new Event("input", { bubbles: true }));
    if (el instanceof HTMLSelectElement) el.dispatchEvent(new Event("change", { bubbles: true }));
  });
}

let fetchMock: ReturnType<typeof vi.fn>;
let lastRoot: ReturnType<typeof createRoot> | null = null;

async function render(): Promise<HTMLElement> {
  const el = document.createElement("div");
  const root = createRoot(el);
  await act(async () => {
    root.render(<AlertRuleEditor />);
  });
  await flush();
  lastRoot = root;
  return el;
}

/** Let the fetch→json→setState promise chain settle. */
async function flush(): Promise<void> {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();
  });
}

function buttonByText(el: HTMLElement, text: string): HTMLButtonElement {
  const btn = [...el.querySelectorAll("button")].find((b) => b.textContent?.trim() === text);
  if (!btn) throw new Error(`button "${text}" not found`);
  return btn as HTMLButtonElement;
}

beforeEach(() => {
  fetchMock = vi.fn();
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  if (lastRoot) act(() => lastRoot!.unmount());
  lastRoot = null;
  vi.unstubAllGlobals();
});

describe("AlertRuleEditor (rule CRUD UI)", () => {
  it("shows an honest persistence-off message and a retry when CRUD answers 503", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ detail: "persistence disabled" }, 503));
    const el = await render();
    expect(el.textContent).toContain("persistence");
    expect(buttonByText(el, "retry")).toBeTruthy();
  });

  it("lists rules returned by the API", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ count: 1, alert_rules: [rule()] }));
    const el = await render();
    expect(el.querySelector(".rule-name")?.textContent).toBe("Emergency squawk");
    expect(el.textContent).toContain("aircraft");
    expect(el.textContent).toContain("1 condition");
  });

  it("deletes a rule via DELETE and reloads", async () => {
    fetchMock.mockImplementation((_url: string, init?: RequestInit) => {
      if ((init?.method ?? "GET") === "DELETE") return Promise.resolve(jsonResponse(null, 204));
      return Promise.resolve(jsonResponse({ count: 1, alert_rules: [rule()] }));
    });
    const el = await render();
    await act(async () => buttonByText(el, "Delete").click());
    await flush();
    const del = fetchMock.mock.calls.find((c) => (c[1]?.method ?? "GET") === "DELETE");
    expect(del?.[0]).toBe("/api/v2/alert-rules/r-1");
  });

  it("toggles enabled via PATCH { enabled }", async () => {
    fetchMock.mockImplementation((_url: string, init?: RequestInit) => {
      if ((init?.method ?? "GET") === "PATCH")
        return Promise.resolve(jsonResponse(rule({ enabled: false })));
      return Promise.resolve(jsonResponse({ count: 1, alert_rules: [rule()] }));
    });
    const el = await render();
    await act(async () => buttonByText(el, "Disable").click());
    await flush();
    const patch = fetchMock.mock.calls.find((c) => (c[1]?.method ?? "GET") === "PATCH");
    expect(patch?.[0]).toBe("/api/v2/alert-rules/r-1");
    expect(JSON.parse(patch?.[1]?.body as string)).toEqual({ enabled: false });
  });

  it("dry-runs a rule and shows an honest preview summary", async () => {
    fetchMock.mockImplementation((url: string, init?: RequestInit) => {
      if (url.endsWith("/test") && (init?.method ?? "GET") === "POST") {
        return Promise.resolve(
          jsonResponse({
            rule_id: "r-1",
            evaluable: true,
            subject_types: ["aircraft"],
            evaluated: 4,
            matched: 1,
            matches: [],
          }),
        );
      }
      return Promise.resolve(jsonResponse({ count: 1, alert_rules: [rule()] }));
    });
    const el = await render();
    await act(async () => buttonByText(el, "Test").click());
    await flush();
    expect(el.querySelector(".rule-preview")?.textContent).toContain("1 of 4");
  });

  it("creates a rule, POSTing a valid body built from the form", async () => {
    let posted: unknown = null;
    fetchMock.mockImplementation((url: string, init?: RequestInit) => {
      const method = init?.method ?? "GET";
      if (method === "POST" && !url.endsWith("/test")) {
        posted = JSON.parse(init?.body as string);
        return Promise.resolve(jsonResponse(rule({ id: "r-new" })));
      }
      return Promise.resolve(jsonResponse({ count: 0, alert_rules: [] }));
    });
    const el = await render();

    await act(async () => buttonByText(el, "+ new").click());

    const name = el.querySelector('input[placeholder="e.g. Emergency squawk"]') as HTMLInputElement;
    setValue(name, "Low fuel");
    // Add a subject type via the quick-add chip.
    await act(async () => buttonByText(el, "+aircraft").click());
    // Fill the single starting condition (operator defaults to "equals" = scalar).
    setValue(el.querySelector(".cond-field") as HTMLInputElement, "squawk");
    setValue(el.querySelector(".cond-value") as HTMLInputElement, "7700");

    await act(async () => buttonByText(el, "Create rule").click());
    await flush();

    expect(posted).toMatchObject({
      name: "Low fuel",
      subject_types: ["aircraft"],
      channels: ["dashboard"],
      conditions: [{ field: "squawk", operator: "equals", value: "7700" }],
    });
  });

  it("blocks save with an inline error when a condition is incomplete", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ count: 0, alert_rules: [] }));
    const el = await render();
    await act(async () => buttonByText(el, "+ new").click());
    const name = el.querySelector('input[placeholder="e.g. Emergency squawk"]') as HTMLInputElement;
    setValue(name, "Bad rule");
    await act(async () => buttonByText(el, "+aircraft").click());
    // Leave the condition field blank → buildCondition throws → inline error, no POST.
    await act(async () => buttonByText(el, "Create rule").click());
    await flush();
    expect(el.querySelector(".alert-error")?.textContent).toMatch(/condition 1/);
    expect(fetchMock.mock.calls.some((c) => (c[1]?.method ?? "GET") === "POST")).toBe(false);
  });
});

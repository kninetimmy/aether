import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { AlertsPanel } from "./AlertsPanel";
import { emptyState } from "../../state/liveState";
import { useStore } from "../../state/store";
import type { AlertRecord, AlertState } from "../../types/records";

// jsdom client render (mirrors ReplayTimeline.test.tsx): render on a real node and
// read innerHTML; click handlers stay live because we don't unmount before reading.
(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT =
  true;

const T0 = "2026-06-20T00:00:00.000Z";

function alert(id: string, over: Partial<AlertRecord> = {}): AlertRecord {
  return {
    schema_version: 2,
    kind: "alert",
    id,
    source: "alert-engine",
    observed_at: T0,
    received_at: T0,
    published_at: T0,
    correlation_key: id,
    provenance: [],
    tags: [],
    attributes: {},
    rule_id: "rule-1",
    subject_id: "aircraft-1",
    state: "open" as AlertState,
    severity: "high",
    title: `Alert ${id}`,
    summary: "squawk 7700",
    triggered_at: T0,
    acknowledged_at: null,
    resolved_at: null,
    delivery_status: {},
    ...over,
  };
}

function seed(alerts: AlertRecord[]) {
  useStore.setState({
    live: { ...emptyState(), alerts: new Map(alerts.map((a) => [a.id, a])) },
  });
}

function jsonResponse(body: unknown, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: `HTTP ${status}`,
    json: async () => body,
  } as unknown as Response;
}

let fetchMock: ReturnType<typeof vi.fn>;
let lastRoot: ReturnType<typeof createRoot> | null = null;

function render(): HTMLElement {
  const el = document.createElement("div");
  const root = createRoot(el);
  act(() => root.render(<AlertsPanel />));
  lastRoot = root;
  return el;
}

beforeEach(() => {
  fetchMock = vi.fn();
  vi.stubGlobal("fetch", fetchMock);
  useStore.setState({ live: emptyState() });
});

afterEach(() => {
  if (lastRoot) act(() => lastRoot!.unmount());
  lastRoot = null;
  vi.unstubAllGlobals();
  useStore.setState({ live: emptyState() });
});

describe("AlertsPanel (ALERT lifecycle UI)", () => {
  it("shows an empty state when there are no active alerts", () => {
    const el = render();
    expect(el.textContent).toContain("No active alerts");
  });

  it("renders active alerts sorted by severity and hides resolved ones", () => {
    seed([
      alert("a-low", { severity: "low", title: "Low one" }),
      alert("a-crit", { severity: "critical", title: "Crit one" }),
      alert("a-done", { state: "resolved", title: "Done one" }),
    ]);
    const el = render();
    const titles = [...el.querySelectorAll(".alert-title")].map((n) => n.textContent);
    expect(titles).toEqual(["Crit one", "Low one"]); // critical before low; resolved hidden
    expect(el.textContent).toContain("1 resolved alert hidden");
  });

  it("acknowledge POSTs to the acknowledge endpoint", async () => {
    seed([alert("al-1")]);
    fetchMock.mockResolvedValueOnce(jsonResponse({ id: "al-1", state: "acknowledged" }));
    const el = render();
    const ackBtn = [...el.querySelectorAll("button")].find(
      (b) => b.textContent === "Acknowledge",
    ) as HTMLButtonElement;
    await act(async () => ackBtn.click());
    expect(fetchMock.mock.calls[0][0]).toBe("/api/v2/alerts/al-1/acknowledge");
  });

  it("resolve POSTs to the resolve endpoint", async () => {
    seed([alert("al-2")]);
    fetchMock.mockResolvedValueOnce(jsonResponse({ id: "al-2", state: "resolved" }));
    const el = render();
    const resolveBtn = el.querySelector(".alert-resolve") as HTMLButtonElement;
    await act(async () => resolveBtn.click());
    expect(fetchMock.mock.calls[0][0]).toBe("/api/v2/alerts/al-2/resolve");
  });

  it("offers no Acknowledge button for an already-acknowledged alert", () => {
    seed([alert("al-3", { state: "acknowledged" })]);
    const el = render();
    const labels = [...el.querySelectorAll("button")].map((b) => b.textContent);
    expect(labels).toContain("Resolve");
    expect(labels).not.toContain("Acknowledge");
  });

  it("surfaces a failed action inline without crashing", async () => {
    seed([alert("al-4")]);
    fetchMock.mockResolvedValueOnce(jsonResponse({ detail: "no live alert" }, 404));
    const el = render();
    const resolveBtn = el.querySelector(".alert-resolve") as HTMLButtonElement;
    await act(async () => resolveBtn.click());
    expect(el.querySelector(".alert-error")?.textContent).toMatch(/no longer live/);
  });
});

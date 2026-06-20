import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { ReplayTimeline } from "./ReplayTimeline";
import { emptyReplay } from "../../state/replay";
import { useStore } from "../../state/store";
import type { ReplaySessionResponse, TrackRecord } from "../../types/records";

// jsdom client render (mirrors TrackList.test.tsx): zustand's server snapshot is
// memoized at store creation, so we render on a real DOM node and read innerHTML.
(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT =
  true;

const T0 = "2026-06-15T00:00:00.000Z";
const T0_MS = Date.parse(T0);

function track(id: string): TrackRecord {
  return {
    schema_version: 2,
    kind: "track",
    id,
    source: "demo",
    observed_at: T0,
    received_at: T0,
    published_at: T0,
    correlation_key: id,
    provenance: [],
    tags: [],
    attributes: {},
    track_type: "aircraft",
    label: id,
    geometry: { type: "Point", coordinates: [-95, 40] },
    altitude_m: null,
    speed_mps: null,
    heading_deg: null,
    vertical_rate_mps: null,
    locally_received: false,
    classification: null,
    valid_until: null,
    predicted: false,
  };
}

function session(over: Partial<ReplaySessionResponse> = {}): ReplaySessionResponse {
  return {
    session_id: "sess",
    start: T0,
    end: new Date(T0_MS + 5 * 60_000).toISOString(),
    sources: null,
    count: 3,
    truncated: false,
    records: [track("A")],
    ...over,
  };
}

function enterReplay(over: Partial<ReplaySessionResponse> = {}) {
  useStore.setState({
    replay: { ...emptyReplay(), mode: "replay", session: session(over), cursorMs: T0_MS },
  });
}

function render(): HTMLElement {
  const el = document.createElement("div");
  const root = createRoot(el);
  act(() => {
    root.render(<ReplayTimeline />);
  });
  // Intentionally not unmounting before reading so click handlers stay live.
  (render as unknown as { _last?: { root: ReturnType<typeof createRoot>; el: HTMLElement } })._last =
    { root, el };
  return el;
}

function teardown() {
  const last = (render as unknown as { _last?: { root: ReturnType<typeof createRoot> } })._last;
  if (last) act(() => last.root.unmount());
}

describe("ReplayTimeline (HISTORY-FR-007)", () => {
  beforeEach(() => {
    useStore.setState({ replay: emptyReplay() });
  });
  afterEach(() => {
    teardown();
    useStore.setState({ replay: emptyReplay() });
  });

  it("renders transport controls + return-to-live + UTC cursor", () => {
    enterReplay();
    const el = render();
    expect(el.querySelector(".replay-play")).not.toBeNull(); // play/pause
    expect(el.querySelectorAll(".replay-step")).toHaveLength(2); // step back/forward
    expect(el.querySelector('select[aria-label="Playback speed"]')).not.toBeNull();
    expect(el.querySelector(".replay-return")).not.toBeNull(); // return-to-live
    expect(el.querySelector('input[type="range"]')).not.toBeNull(); // jump-to-time
    expect(el.textContent).toContain("2026-06-15"); // UTC cursor time shown
  });

  it("warns when the buffer was truncated", () => {
    enterReplay({ truncated: true });
    const el = render();
    expect(el.querySelector(".replay-truncated")).not.toBeNull();
    expect(el.textContent).toContain("truncated");
  });

  it("the play button toggles store playback and return-to-live exits", () => {
    enterReplay();
    const el = render();
    const play = el.querySelector(".replay-play") as HTMLButtonElement;
    act(() => play.click());
    expect(useStore.getState().replay.playing).toBe(true);

    const ret = el.querySelector(".replay-return") as HTMLButtonElement;
    act(() => ret.click());
    expect(useStore.getState().replay.mode).toBe("live");
    expect(useStore.getState().replay.session).toBeNull();
  });

  it("a step button advances the cursor", () => {
    enterReplay();
    const el = render();
    const steps = el.querySelectorAll(".replay-step");
    const forward = steps[1] as HTMLButtonElement; // ⏭
    act(() => forward.click());
    expect(useStore.getState().replay.cursorMs).toBeGreaterThan(T0_MS);
  });
});

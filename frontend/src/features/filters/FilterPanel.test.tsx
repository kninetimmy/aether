import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { FilterPanel } from "./FilterPanel";
import { emptyState } from "../../state/liveState";
import { defaultFilters, useStore, type OrbitalConfig } from "../../state/store";

// jsdom render on a real node (zustand's server snapshot is memoized at creation),
// returning the container so we can query controls and fire interactions.
(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT =
  true;

function renderPanel(): { el: HTMLElement; unmount: () => void } {
  const el = document.createElement("div");
  const root = createRoot(el);
  act(() => {
    root.render(<FilterPanel />);
  });
  return {
    el,
    unmount: () =>
      act(() => {
        root.unmount();
      }),
  };
}

const ORBITAL_ON: OrbitalConfig = {
  enabled: true,
  groups: ["stations", "amateur"],
  minElevationDeg: 10,
};

describe("FilterPanel orbital controls (M6.6a)", () => {
  beforeEach(() => {
    useStore.setState({
      live: emptyState(),
      filters: defaultFilters(),
      orbitalConfig: null,
    });
  });
  afterEach(() => {
    useStore.setState({
      live: emptyState(),
      filters: defaultFilters(),
      orbitalConfig: null,
    });
  });

  it("omits the orbital fieldset when orbital config is absent", () => {
    const { el, unmount } = renderPanel();
    expect(el.querySelector('[aria-label="Orbital"]')).toBeNull();
    unmount();
  });

  it("omits the orbital fieldset when the adapter is disabled", () => {
    useStore.setState({ orbitalConfig: { ...ORBITAL_ON, enabled: false } });
    const { el, unmount } = renderPanel();
    expect(el.querySelector('[aria-label="Orbital"]')).toBeNull();
    unmount();
  });

  it("renders a category chip per group and an elevation input when enabled", () => {
    useStore.setState({ orbitalConfig: ORBITAL_ON });
    const { el, unmount } = renderPanel();
    const fieldset = el.querySelector('[aria-label="Orbital"]');
    expect(fieldset).not.toBeNull();
    expect(fieldset?.textContent).toContain("stations");
    expect(fieldset?.textContent).toContain("amateur");
    expect(fieldset?.querySelector('input[type="number"]')).not.toBeNull();
    unmount();
  });

  it("toggling a category chip updates orbitalCategory in the store", () => {
    useStore.setState({ orbitalConfig: ORBITAL_ON });
    const { el, unmount } = renderPanel();
    const fieldset = el.querySelector('[aria-label="Orbital"]') as HTMLElement;
    const chip = Array.from(fieldset.querySelectorAll("label")).find((l) =>
      l.textContent?.includes("stations"),
    );
    const box = chip?.querySelector('input[type="checkbox"]') as HTMLInputElement;
    act(() => box.click());
    expect(useStore.getState().filters.orbitalCategory?.has("stations")).toBe(true);
    unmount();
  });
});

describe("FilterPanel watchlist toggle (M6.6a wiring)", () => {
  beforeEach(() => {
    useStore.setState({
      live: emptyState(),
      filters: defaultFilters(),
      orbitalConfig: null,
    });
  });
  afterEach(() => {
    useStore.setState({
      live: emptyState(),
      filters: defaultFilters(),
      orbitalConfig: null,
    });
  });

  it("renders the watchlist-only checkbox and drives filters.watchlistOnly", () => {
    const { el, unmount } = renderPanel();
    const label = Array.from(el.querySelectorAll("label")).find((l) =>
      l.textContent?.includes("Watchlist only"),
    );
    expect(label).not.toBeUndefined();
    const box = label?.querySelector('input[type="checkbox"]') as HTMLInputElement;
    expect(useStore.getState().filters.watchlistOnly).toBe(false);
    act(() => box.click());
    expect(useStore.getState().filters.watchlistOnly).toBe(true);
    unmount();
  });
});

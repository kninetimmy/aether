import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// localStorage round-trip for the TOI watchlist (M3.6c). The store hydrates the
// watchlist Set from localStorage at CREATION time, so each test seeds the store
// then re-imports the module fresh (vi.resetModules) to exercise hydration.

const KEY = "aether.toi.watchlist.v1";

async function freshStore() {
  vi.resetModules();
  return (await import("./store")).useStore;
}

describe("watchlist localStorage hydrate / write-through", () => {
  beforeEach(() => {
    localStorage.clear();
  });
  afterEach(() => {
    localStorage.clear();
  });

  it("hydrates the watchlist Set from a seeded localStorage array", async () => {
    localStorage.setItem(KEY, JSON.stringify(["aircraft:icao:aaa", "mmsi:123"]));
    const useStore = await freshStore();
    const wl = useStore.getState().watchlist;
    expect(wl.has("aircraft:icao:aaa")).toBe(true);
    expect(wl.has("mmsi:123")).toBe(true);
    expect(wl.size).toBe(2);
  });

  it("starts empty when nothing is stored", async () => {
    const useStore = await freshStore();
    expect(useStore.getState().watchlist.size).toBe(0);
  });

  it("tolerates a corrupt blob without throwing (empty set)", async () => {
    localStorage.setItem(KEY, "{not json");
    const useStore = await freshStore();
    expect(useStore.getState().watchlist.size).toBe(0);
  });

  it("toggleWatchlist writes through to localStorage (add then remove)", async () => {
    const useStore = await freshStore();
    useStore.getState().toggleWatchlist("aircraft:icao:aaa");
    expect(useStore.getState().watchlist.has("aircraft:icao:aaa")).toBe(true);
    expect(JSON.parse(localStorage.getItem(KEY) as string)).toEqual([
      "aircraft:icao:aaa",
    ]);

    useStore.getState().toggleWatchlist("aircraft:icao:aaa");
    expect(useStore.getState().watchlist.has("aircraft:icao:aaa")).toBe(false);
    expect(JSON.parse(localStorage.getItem(KEY) as string)).toEqual([]);
  });

  it("removeFromWatchlist persists the removal", async () => {
    localStorage.setItem(KEY, JSON.stringify(["a", "b"]));
    const useStore = await freshStore();
    useStore.getState().removeFromWatchlist("a");
    expect(useStore.getState().watchlist.has("a")).toBe(false);
    expect(JSON.parse(localStorage.getItem(KEY) as string)).toEqual(["b"]);
  });

  it("a fresh store re-hydrates what a previous session persisted", async () => {
    const first = await freshStore();
    first.getState().toggleWatchlist("aprs:N0CALL-9");
    // Simulate a page reload: re-import the module, hydration reads localStorage.
    const second = await freshStore();
    expect(second.getState().watchlist.has("aprs:N0CALL-9")).toBe(true);
  });

  it("setToiMeta shallow-merges label/priority", async () => {
    const useStore = await freshStore();
    useStore.getState().setToiMeta("k", { label: "Target 1" });
    useStore.getState().setToiMeta("k", { priority: 1 });
    expect(useStore.getState().toiMeta.get("k")).toEqual({
      label: "Target 1",
      priority: 1,
    });
  });

  it("selectTrack sets and clears the selection", async () => {
    const useStore = await freshStore();
    useStore.getState().selectTrack("net:aircraft:icao:aaa");
    expect(useStore.getState().selectedTrackId).toBe("net:aircraft:icao:aaa");
    useStore.getState().selectTrack(null);
    expect(useStore.getState().selectedTrackId).toBeNull();
  });
});

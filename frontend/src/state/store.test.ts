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
    // Absorb the M6.6b backend write-through so these cache-focused tests stay
    // hermetic (a 204 makes every PUT/DELETE a no-op success).
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({ ok: true, status: 204, statusText: "", json: async () => null }),
    );
  });
  afterEach(() => {
    vi.unstubAllGlobals();
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

// Backend-authoritative watchlist (M6.6b, PRD §21.5): localStorage is now a cache;
// hydrateWatchlist reconciles to the server, and toggles write through to the API.
describe("watchlist backend reconcile + write-through (M6.6b)", () => {
  function jsonResponse(body: unknown, init: { status?: number; ok?: boolean } = {}) {
    const status = init.status ?? 200;
    return {
      ok: init.ok ?? (status >= 200 && status < 300),
      status,
      statusText: `HTTP ${status}`,
      json: async () => body,
    } as unknown as Response;
  }
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    localStorage.clear();
    fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    localStorage.clear();
  });

  it("hydrateWatchlist replaces the Set + toiMeta from server entries and refreshes the cache", async () => {
    localStorage.setItem(KEY, JSON.stringify(["stale:key"])); // cache to be overwritten
    fetchMock.mockResolvedValueOnce(
      jsonResponse({
        count: 2,
        entries: [
          { key: "aircraft:icao:aaa", label: "T1", priority: 2, notes: null,
            created_at: "2026-06-15T00:00:00+00:00", updated_at: "2026-06-15T00:00:00+00:00" },
          { key: "mmsi:123", label: null, priority: null, notes: null,
            created_at: "2026-06-15T00:00:00+00:00", updated_at: "2026-06-15T00:00:00+00:00" },
        ],
      }),
    );
    const useStore = await freshStore();
    await useStore.getState().hydrateWatchlist();
    const s = useStore.getState();
    expect([...s.watchlist].sort()).toEqual(["aircraft:icao:aaa", "mmsi:123"]);
    expect(s.watchlist.has("stale:key")).toBe(false);
    expect(s.toiMeta.get("aircraft:icao:aaa")).toEqual({ label: "T1", priority: 2 });
    expect(s.toiMeta.has("mmsi:123")).toBe(false); // no meta → no entry
    // Cache refreshed to the authoritative set.
    expect(JSON.parse(localStorage.getItem(KEY) as string).sort()).toEqual([
      "aircraft:icao:aaa",
      "mmsi:123",
    ]);
  });

  it("hydrateWatchlist keeps the localStorage cache when the API fails (offline tolerance)", async () => {
    localStorage.setItem(KEY, JSON.stringify(["cached:key"]));
    fetchMock.mockResolvedValueOnce(jsonResponse({ detail: "off" }, { status: 503 }));
    const useStore = await freshStore();
    await useStore.getState().hydrateWatchlist();
    // The cache-hydrated set is untouched; cache not wiped.
    expect(useStore.getState().watchlist.has("cached:key")).toBe(true);
    expect(JSON.parse(localStorage.getItem(KEY) as string)).toEqual(["cached:key"]);
  });

  it("toggleWatchlist writes through: PUT to add, DELETE to remove", async () => {
    fetchMock.mockResolvedValue(jsonResponse(null, { status: 204 }));
    const useStore = await freshStore();
    useStore.getState().toggleWatchlist("aircraft:icao:aaa");
    expect(fetchMock).toHaveBeenLastCalledWith(
      "/api/v2/watchlist/aircraft%3Aicao%3Aaaa",
      expect.objectContaining({ method: "PUT" }),
    );
    useStore.getState().toggleWatchlist("aircraft:icao:aaa");
    expect(fetchMock).toHaveBeenLastCalledWith(
      "/api/v2/watchlist/aircraft%3Aicao%3Aaaa",
      expect.objectContaining({ method: "DELETE" }),
    );
  });

  it("removeFromWatchlist writes through a DELETE", async () => {
    localStorage.setItem(KEY, JSON.stringify(["a"]));
    fetchMock.mockResolvedValue(jsonResponse(null, { status: 204 }));
    const useStore = await freshStore();
    useStore.getState().removeFromWatchlist("a");
    expect(fetchMock).toHaveBeenLastCalledWith(
      "/api/v2/watchlist/a",
      expect.objectContaining({ method: "DELETE" }),
    );
  });

  it("a failed write-through leaves the optimistic local state + cache in place", async () => {
    fetchMock.mockRejectedValue(new Error("offline"));
    const useStore = await freshStore();
    useStore.getState().toggleWatchlist("aprs:N0CALL-9");
    // Optimistic add survived the rejected write-through.
    expect(useStore.getState().watchlist.has("aprs:N0CALL-9")).toBe(true);
    expect(JSON.parse(localStorage.getItem(KEY) as string)).toEqual(["aprs:N0CALL-9"]);
  });
});

// Orbital runtime config + filter defaults (M6.6a).
describe("orbital config (M6.6a)", () => {
  it("orbitalConfigFromApi maps snake_case → camelCase", async () => {
    const { orbitalConfigFromApi } = await import("./store");
    expect(
      orbitalConfigFromApi({
        enabled: true,
        groups: ["stations", "amateur"],
        min_elevation_deg: 10,
      }),
    ).toEqual({ enabled: true, groups: ["stations", "amateur"], minElevationDeg: 10 });
  });

  it("orbitalConfigFromApi degrades to null when the block is absent", async () => {
    const { orbitalConfigFromApi } = await import("./store");
    expect(orbitalConfigFromApi(null)).toBeNull();
    expect(orbitalConfigFromApi(undefined)).toBeNull();
  });

  it("setOrbitalConfig sets then clears the runtime config", async () => {
    const useStore = await freshStore();
    expect(useStore.getState().orbitalConfig).toBeNull();
    useStore.getState().setOrbitalConfig({
      enabled: true,
      groups: ["stations"],
      minElevationDeg: 10,
    });
    expect(useStore.getState().orbitalConfig?.enabled).toBe(true);
    useStore.getState().setOrbitalConfig(null);
    expect(useStore.getState().orbitalConfig).toBeNull();
  });

  it("resetFilters restores the orbital filter defaults to null", async () => {
    const useStore = await freshStore();
    useStore.getState().setFilters({
      orbitalCategory: new Set(["stations"]),
      orbitalMinElevationDeg: 30,
    });
    expect(useStore.getState().filters.orbitalMinElevationDeg).toBe(30);
    useStore.getState().resetFilters();
    expect(useStore.getState().filters.orbitalCategory).toBeNull();
    expect(useStore.getState().filters.orbitalMinElevationDeg).toBeNull();
  });
});

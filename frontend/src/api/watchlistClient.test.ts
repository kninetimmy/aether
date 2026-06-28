import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  listWatchlist,
  putWatchlistEntry,
  deleteWatchlistEntry,
  type WatchlistEntry,
} from "./watchlistClient";

// Mock global fetch so the client is exercised without a backend. We assert the
// request shape (method/url/body), the key path-encoding, and the error mapping
// (status → WatchlistError) — the contract store.ts relies on (M6.6b, PRD §21.5).

function jsonResponse(body: unknown, init: { status?: number; ok?: boolean } = {}) {
  const status = init.status ?? 200;
  return {
    ok: init.ok ?? (status >= 200 && status < 300),
    status,
    statusText: `HTTP ${status}`,
    json: async () => body,
  } as unknown as Response;
}

const ENTRY: WatchlistEntry = {
  key: "aircraft:icao:abc123",
  label: "Target 1",
  priority: 1,
  notes: null,
  created_at: "2026-06-15T00:00:00+00:00",
  updated_at: "2026-06-15T00:00:00+00:00",
};

let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  fetchMock = vi.fn();
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("listWatchlist", () => {
  it("GETs /api/v2/watchlist and returns the entries array", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ count: 1, entries: [ENTRY] }));
    const out = await listWatchlist();
    expect(out).toEqual([ENTRY]);
    expect(fetchMock).toHaveBeenCalledWith("/api/v2/watchlist");
  });

  it("returns [] when the body has no entries array", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ count: 0 }));
    expect(await listWatchlist()).toEqual([]);
  });

  it("maps a 503 (persistence off) to WatchlistError with the status", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({ detail: "persistence disabled; watchlist unavailable" }, { status: 503 }),
    );
    await expect(listWatchlist()).rejects.toMatchObject({
      name: "WatchlistError",
      status: 503,
    });
  });

  it("maps a transport failure to WatchlistError status 0", async () => {
    fetchMock.mockRejectedValue(new Error("network down"));
    await expect(listWatchlist()).rejects.toMatchObject({
      name: "WatchlistError",
      status: 0,
    });
  });
});

describe("putWatchlistEntry", () => {
  it("PUTs to the encoded key path with a JSON meta body", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(ENTRY));
    await putWatchlistEntry("aircraft:icao:abc123", { label: "T1" });
    const [url, init] = fetchMock.mock.calls[0];
    // Colons are percent-encoded for the path; the server's {key:path} decodes them.
    expect(url).toBe("/api/v2/watchlist/aircraft%3Aicao%3Aabc123");
    expect(init.method).toBe("PUT");
    expect(JSON.parse(init.body)).toEqual({ label: "T1" });
  });

  it("defaults to an empty meta body (membership-only toggle-on)", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(ENTRY));
    await putWatchlistEntry("mmsi:123");
    const [, init] = fetchMock.mock.calls[0];
    expect(JSON.parse(init.body)).toEqual({});
  });

  it("throws WatchlistError on a non-2xx", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ detail: "boom" }, { status: 503 }));
    await expect(putWatchlistEntry("k")).rejects.toMatchObject({ status: 503 });
  });
});

describe("deleteWatchlistEntry", () => {
  it("DELETEs the encoded key path", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(null, { status: 204 }));
    await deleteWatchlistEntry("orbital:celestrak:25544");
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/v2/watchlist/orbital%3Acelestrak%3A25544");
    expect(init.method).toBe("DELETE");
  });

  it("treats a 404 as success (idempotent toggle-off)", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ detail: "gone" }, { status: 404 }));
    await expect(deleteWatchlistEntry("k")).resolves.toBeUndefined();
  });

  it("throws WatchlistError on an unexpected status", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ detail: "boom" }, { status: 503 }));
    await expect(deleteWatchlistEntry("k")).rejects.toMatchObject({ status: 503 });
  });
});

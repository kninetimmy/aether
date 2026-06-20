import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  ReplayError,
  createReplaySession,
  deleteReplaySession,
} from "./replayClient";
import type { ReplaySessionResponse } from "../types/records";

// Mock the global fetch so the client is exercised without a real backend. We
// assert the request shape (method/url/body) and the error mapping (status →
// ReplayError) — the contract the store relies on (M4.8, PRD §19.6/§21.6).

function jsonResponse(body: unknown, init: { status?: number; ok?: boolean } = {}) {
  const status = init.status ?? 200;
  return {
    ok: init.ok ?? (status >= 200 && status < 300),
    status,
    statusText: `HTTP ${status}`,
    json: async () => body,
  } as unknown as Response;
}

const SESSION: ReplaySessionResponse = {
  session_id: "abc123",
  start: "2026-06-15T00:00:00+00:00",
  end: "2026-06-15T01:00:00+00:00",
  sources: null,
  count: 2,
  truncated: false,
  records: [],
};

let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  fetchMock = vi.fn();
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("createReplaySession", () => {
  it("POSTs the window to /api/v2/replay/sessions and returns the buffer", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(SESSION));
    const out = await createReplaySession({
      start: SESSION.start,
      end: SESSION.end,
      sources: ["demo"],
      max_records: 500,
    });
    expect(out).toEqual(SESSION);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/v2/replay/sessions");
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body)).toEqual({
      start: SESSION.start,
      end: SESSION.end,
      sources: ["demo"],
      max_records: 500,
    });
  });

  it("throws a ReplayError carrying the status + detail on a 503 (persistence off)", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse({ detail: "persistence disabled; replay unavailable" }, { status: 503 }),
    );
    const err = await createReplaySession({
      start: SESSION.start,
      end: SESSION.end,
    }).catch((e) => e);
    expect(err).toBeInstanceOf(ReplayError);
    expect(err.status).toBe(503);
    // The detail string is preserved so the UI can show it verbatim.
    expect(err.message).toMatch(/persistence disabled/);
  });

  it("maps a 400 (bad/over-long window) to a ReplayError", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse({ detail: "end must be after start" }, { status: 400 }),
    );
    const err = await createReplaySession({
      start: SESSION.end,
      end: SESSION.start,
    }).catch((e) => e);
    expect(err).toBeInstanceOf(ReplayError);
    expect(err.status).toBe(400);
  });

  it("maps a transport failure to a ReplayError with status 0", async () => {
    fetchMock.mockRejectedValueOnce(new Error("network down"));
    const err = await createReplaySession({
      start: SESSION.start,
      end: SESSION.end,
    }).catch((e) => e);
    expect(err).toBeInstanceOf(ReplayError);
    expect(err.status).toBe(0);
  });
});

describe("deleteReplaySession", () => {
  it("DELETEs the session by id", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(null, { status: 204 }));
    await deleteReplaySession("abc 123");
    const [url, init] = fetchMock.mock.calls[0];
    // The id is URL-encoded so a space/odd char can't break the path.
    expect(url).toBe("/api/v2/replay/sessions/abc%20123");
    expect(init.method).toBe("DELETE");
  });

  it("treats a 404 as success (idempotent teardown)", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ detail: "no replay session" }, { status: 404 }));
    await expect(deleteReplaySession("gone")).resolves.toBeUndefined();
  });

  it("throws on an unexpected status", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ detail: "boom" }, { status: 500 }));
    await expect(deleteReplaySession("x")).rejects.toMatchObject({ status: 500 });
  });
});

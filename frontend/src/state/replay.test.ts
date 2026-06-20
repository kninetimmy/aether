import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  clampCursor,
  emptyReplay,
  replayKey,
  replayVisibleRecords,
  sessionBoundsMs,
} from "./replay";
import type {
  AnyRecord,
  ReplaySessionResponse,
  TrackRecord,
} from "../types/records";

// --- Fixtures --------------------------------------------------------------

/** A track record observed at `observedAt`, otherwise minimal. */
function track(id: string, observedAt: string, over: Partial<TrackRecord> = {}): TrackRecord {
  return {
    schema_version: 2,
    kind: "track",
    id,
    source: "demo",
    observed_at: observedAt,
    received_at: observedAt,
    published_at: observedAt,
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
    ...over,
  };
}

const T0 = "2026-06-15T00:00:00.000Z";
const T0_MS = Date.parse(T0);
const MIN = 60_000;

/** Build a session whose buffer is `records` (ascending observed_at). */
function session(records: AnyRecord[], over: Partial<ReplaySessionResponse> = {}): ReplaySessionResponse {
  return {
    session_id: "sess",
    start: T0,
    end: new Date(T0_MS + 10 * MIN).toISOString(),
    sources: null,
    count: records.length,
    truncated: false,
    records,
    ...over,
  };
}

// --- Pure helpers ----------------------------------------------------------

describe("clampCursor", () => {
  it("clamps below start and above end", () => {
    expect(clampCursor(50, 100, 200)).toBe(100);
    expect(clampCursor(250, 100, 200)).toBe(200);
    expect(clampCursor(150, 100, 200)).toBe(150);
  });
  it("tolerates an inverted range and NaN", () => {
    expect(clampCursor(150, 200, 100)).toBe(150); // [100,200] after min/max
    expect(clampCursor(Number.NaN, 100, 200)).toBe(100);
  });
});

describe("sessionBoundsMs", () => {
  it("returns 0/0 for a null session", () => {
    expect(sessionBoundsMs(null)).toEqual({ startMs: 0, endMs: 0 });
  });
  it("parses ISO bounds to ms", () => {
    const b = sessionBoundsMs(session([]));
    expect(b.startMs).toBe(T0_MS);
    expect(b.endMs).toBe(T0_MS + 10 * MIN);
  });
});

describe("replayKey", () => {
  it("keys non-status records by kind:id and status by source", () => {
    expect(replayKey(track("aircraft:icao:abc", T0))).toBe("track:aircraft:icao:abc");
    const status: AnyRecord = {
      schema_version: 2,
      kind: "source_status",
      id: "status:local_adsb",
      source: "local_adsb",
      observed_at: T0,
      received_at: T0,
      published_at: T0,
      provenance: [],
      tags: [],
      attributes: {},
      status: "connected",
      last_success_at: null,
      last_record_at: null,
      lag_s: null,
      records_received: 0,
      records_rejected: 0,
      error_code: null,
      error_summary: null,
    };
    expect(replayKey(status)).toBe("source_status:local_adsb");
  });
});

describe("replayVisibleRecords (visible-set at cursor T)", () => {
  it("returns [] for a null session", () => {
    expect(replayVisibleRecords(null, T0_MS)).toEqual([]);
  });

  it("picks the LATEST observation per identity at/under the cursor", () => {
    const a1 = track("A", T0); // +0s
    const a2 = track("A", new Date(T0_MS + 30_000).toISOString()); // +30s, newer A
    const a3 = track("A", new Date(T0_MS + 90_000).toISOString()); // +90s, future of cursor
    const buf = session([a1, a2, a3]);
    // Cursor at +45s; A's latest non-future obs is a2 (+30s, age 15s < demo expiry 60s).
    const vis = replayVisibleRecords(buf, T0_MS + 45_000);
    expect(vis).toHaveLength(1);
    expect(vis[0].observed_at).toBe(a2.observed_at);
  });

  it("drops a record older than its source's expiry (demo = 60s)", () => {
    const old = track("A", T0); // +0s
    const fresh = track("B", new Date(T0_MS + 200_000).toISOString()); // +200s
    const buf = session([old, fresh]);
    // Cursor at +210s: A is 210s old (> 60s demo expiry → dropped); B is 10s old (kept).
    const vis = replayVisibleRecords(buf, T0_MS + 210_000);
    expect(vis.map((r) => r.id)).toEqual(["B"]);
  });

  it("keeps a slow-source track far longer than a fast one (per-source expiry)", () => {
    // The whole point of per-source expiry: an APRS station stays visible between
    // beacons (2h window) exactly as it does live, while a demo/ADS-B track at the same
    // age (10min) has long since aged off (60s window) — a flat window can't do both.
    const aprs = track("APRS", T0, { source: "aprs_is" });
    const adsb = track("ADSB", T0, { source: "demo" });
    const buf = session([aprs, adsb], { end: new Date(T0_MS + 3 * 60 * MIN).toISOString() });
    const vis = replayVisibleRecords(buf, T0_MS + 10 * MIN); // both 10min old
    expect(vis.map((r) => r.id)).toEqual(["APRS"]); // ADS-B aged off; APRS still up
  });

  it("excludes records in the future relative to the cursor", () => {
    const past = track("A", new Date(T0_MS + 30_000).toISOString()); // +30s (age 30s at cursor)
    const future = track("B", new Date(T0_MS + 5 * MIN).toISOString());
    const buf = session([past, future]);
    const vis = replayVisibleRecords(buf, T0_MS + MIN);
    expect(vis.map((r) => r.id)).toEqual(["A"]);
  });

  it("skips a record with an unparseable observed_at (never throws)", () => {
    const good = track("A", T0);
    const bad = track("B", "not-a-date");
    const buf = session([good, bad]);
    const vis = replayVisibleRecords(buf, T0_MS + 1000);
    expect(vis.map((r) => r.id)).toEqual(["A"]);
  });

  it("keeps distinct identities at the same cursor", () => {
    const a = track("A", T0);
    const b = track("B", new Date(T0_MS + 1000).toISOString());
    const buf = session([a, b]);
    const vis = replayVisibleRecords(buf, T0_MS + 2000);
    expect(new Set(vis.map((r) => r.id))).toEqual(new Set(["A", "B"]));
  });
});

// --- Store integration (mode toggle, return-to-live, step/seek clamping) ---

async function freshStore() {
  vi.resetModules();
  return (await import("./store")).useStore;
}

describe("store replay slice", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });
  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("enterReplay loads the buffer, switches to replay mode, parks at start paused", async () => {
    const useStore = await freshStore();
    const buf = session([track("A", T0)]);
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        status: 200,
        statusText: "OK",
        json: async () => buf,
      }),
    );
    await useStore.getState().enterReplay({ start: buf.start, end: buf.end });
    const r = useStore.getState().replay;
    expect(r.mode).toBe("replay");
    expect(r.session?.session_id).toBe("sess");
    expect(r.cursorMs).toBe(T0_MS);
    expect(r.playing).toBe(false);
  });

  it("exitReplay restores live rendering without touching live state", async () => {
    const useStore = await freshStore();
    const buf = session([track("A", T0)]);
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 204,
      statusText: "No Content",
      json: async () => buf,
    });
    // First call (POST) returns the buffer; later DELETE resolves the same shape.
    fetchMock.mockResolvedValueOnce({
      ok: true,
      status: 200,
      statusText: "OK",
      json: async () => buf,
    });
    vi.stubGlobal("fetch", fetchMock);
    // Seed some live state and assert replay never mutates it.
    const liveTracks = useStore.getState().live.tracks;
    await useStore.getState().enterReplay({ start: buf.start, end: buf.end });
    expect(useStore.getState().replay.mode).toBe("replay");
    useStore.getState().exitReplay();
    const r = useStore.getState().replay;
    expect(r.mode).toBe("live");
    expect(r.session).toBeNull();
    // Live state object is the very same reference — replay touched nothing live.
    expect(useStore.getState().live.tracks).toBe(liveTracks);
  });

  it("step and seek clamp the cursor to the session window", async () => {
    const useStore = await freshStore();
    const buf = session([track("A", T0)]); // window [T0, T0+10min]
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        status: 200,
        statusText: "OK",
        json: async () => buf,
      }),
    );
    await useStore.getState().enterReplay({ start: buf.start, end: buf.end });

    // step backward below start → clamped to start.
    useStore.getState().step(-5 * MIN);
    expect(useStore.getState().replay.cursorMs).toBe(T0_MS);

    // seek past the end → clamped to end.
    useStore.getState().seek(T0_MS + 999 * MIN);
    expect(useStore.getState().replay.cursorMs).toBe(T0_MS + 10 * MIN);

    // step forward past the end → clamped to end.
    useStore.getState().seek(T0_MS + 5 * MIN);
    useStore.getState().step(99 * MIN);
    expect(useStore.getState().replay.cursorMs).toBe(T0_MS + 10 * MIN);
  });

  it("tick advances while playing and auto-pauses at the window end", async () => {
    const useStore = await freshStore();
    const buf = session([track("A", T0)], { end: new Date(T0_MS + 2000).toISOString() });
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        status: 200,
        statusText: "OK",
        json: async () => buf,
      }),
    );
    await useStore.getState().enterReplay({ start: buf.start, end: buf.end });
    useStore.getState().play();
    expect(useStore.getState().replay.playing).toBe(true);
    useStore.getState().tick(1000); // +1s, still inside
    expect(useStore.getState().replay.cursorMs).toBe(T0_MS + 1000);
    expect(useStore.getState().replay.playing).toBe(true);
    useStore.getState().tick(5000); // overshoots end → park at end, pause
    expect(useStore.getState().replay.cursorMs).toBe(T0_MS + 2000);
    expect(useStore.getState().replay.playing).toBe(false);
  });

  it("play is a no-op when not in a replay session", async () => {
    const useStore = await freshStore();
    useStore.getState().play();
    expect(useStore.getState().replay.mode).toBe("live");
    expect(useStore.getState().replay.playing).toBe(false);
  });

  it("emptyReplay is the live no-op default", () => {
    const e = emptyReplay();
    expect(e.mode).toBe("live");
    expect(e.session).toBeNull();
    expect(e.playing).toBe(false);
  });
});

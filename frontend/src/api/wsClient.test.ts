import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { WsClient } from "./wsClient";
import type { SnapshotFrame, SubscribeFrame } from "../types/records";

// A controllable WebSocket stand-in: capture sent frames and let tests drive the
// lifecycle (open/message/close) deterministically. Mirrors the subset of the DOM
// WebSocket the client touches.
class FakeSocket {
  static instances: FakeSocket[] = [];
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static readonly CLOSING = 2;
  static readonly CLOSED = 3;

  readyState = FakeSocket.CONNECTING;
  sent: string[] = [];
  onopen: (() => void) | null = null;
  onmessage: ((ev: { data: string }) => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;

  constructor(public url: string) {
    FakeSocket.instances.push(this);
  }
  send(data: string): void {
    this.sent.push(data);
  }
  close(): void {
    this.readyState = FakeSocket.CLOSED;
    this.onclose?.();
  }
  // Test drivers:
  fireOpen(): void {
    this.readyState = FakeSocket.OPEN;
    this.onopen?.();
  }
  fireMessage(obj: unknown): void {
    this.onmessage?.({ data: JSON.stringify(obj) });
  }
}

const SUB: SubscribeFrame = {
  type: "subscribe",
  bbox: [-80, 35, -74, 42],
  sources: ["local_adsb"],
  track_types: ["aircraft"],
  include_events: true,
  include_alerts: true,
};

function snapshot(seq: number, cseq: number): SnapshotFrame {
  return { type: "snapshot", seq, cseq, tracks: [], features: [], events: [], alerts: [], source_status: [] };
}

const NOOP = { onState: () => {}, onStatus: () => {} };

beforeEach(() => {
  FakeSocket.instances = [];
  vi.useFakeTimers();
  vi.stubGlobal("WebSocket", FakeSocket);
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

function only(): FakeSocket {
  expect(FakeSocket.instances.length).toBeGreaterThan(0);
  return FakeSocket.instances[FakeSocket.instances.length - 1];
}

describe("WsClient subscribe wiring (M3.6b)", () => {
  it("sends the last subscribe on socket open", () => {
    const c = new WsClient(NOOP, { url: "ws://x/ws/v2", subscribeDebounceMs: 50 });
    c.connect();
    c.subscribe(SUB); // before open: queued, not yet sent (no socket OPEN)
    const sock = only();
    expect(sock.sent).toHaveLength(0);
    sock.fireOpen();
    // On open the latest intent is flushed immediately (no debounce wait).
    expect(sock.sent).toHaveLength(1);
    expect(JSON.parse(sock.sent[0])).toMatchObject({ type: "subscribe", bbox: SUB.bbox });
  });

  it("debounces a viewport/filter change to one frame", () => {
    const c = new WsClient(NOOP, { subscribeDebounceMs: 300 });
    c.connect();
    only().fireOpen();
    only().sent.length = 0; // ignore the on-open send
    c.subscribe({ ...SUB, bbox: [1, 1, 2, 2] });
    c.subscribe({ ...SUB, bbox: [3, 3, 4, 4] }); // supersedes within the window
    expect(only().sent).toHaveLength(0); // nothing sent before the debounce fires
    vi.advanceTimersByTime(300);
    expect(only().sent).toHaveLength(1);
    expect(JSON.parse(only().sent[0]).bbox).toEqual([3, 3, 4, 4]); // last wins
  });

  it("re-sends the last subscribe on reconnect (fresh socket)", () => {
    const c = new WsClient(NOOP, { baseBackoffMs: 10, subscribeDebounceMs: 50 });
    c.connect();
    const first = only();
    first.fireOpen();
    c.subscribe(SUB);
    vi.advanceTimersByTime(50);
    expect(first.sent.length).toBeGreaterThan(0);
    // Drop the connection → the client schedules a reconnect with a NEW socket.
    first.close();
    vi.advanceTimersByTime(10);
    const second = only();
    expect(second).not.toBe(first);
    second.fireOpen();
    // The reconnect re-anchors the same intent so the server re-snapshots.
    expect(second.sent).toHaveLength(1);
    expect(JSON.parse(second.sent[0])).toMatchObject({ type: "subscribe" });
  });

  it("treats a post-subscribe snapshot as a fresh cseq resync baseline", () => {
    const states: number[] = [];
    const c = new WsClient(
      { onState: (s) => states.push(s.cseq), onStatus: () => {} },
      { subscribeDebounceMs: 10 },
    );
    c.connect();
    const sock = only();
    sock.fireOpen();
    // First (default) snapshot then a delta.
    sock.fireMessage(snapshot(100, 0));
    sock.fireMessage({ type: "source_status", seq: 101, cseq: 1, record: status() });
    // A re-subscribe arrives → server replies with a FRESH snapshot at cseq 0.
    sock.fireMessage(snapshot(140, 0));
    // The next delta must be cseq 1 against the re-anchored baseline (no false gap).
    const before = states.length;
    sock.fireMessage({ type: "source_status", seq: 141, cseq: 1, record: status() });
    expect(states[states.length - 1]).toBe(1);
    expect(states.length).toBe(before + 1); // applied, not dropped as a gap
  });
});

function status() {
  const NOW = "2026-06-15T00:00:00Z";
  return {
    schema_version: 2,
    kind: "source_status",
    id: "status:local_adsb",
    source: "local_adsb",
    observed_at: NOW,
    received_at: NOW,
    published_at: NOW,
    provenance: [],
    tags: [],
    attributes: {},
    status: "connected",
    last_success_at: NOW,
    last_record_at: NOW,
    lag_s: 0,
    records_received: 1,
    records_rejected: 0,
    error_code: null,
    error_summary: null,
  };
}

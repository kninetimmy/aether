// Typed /ws/v2 client (PRD §22): connects, parses server frames, and drives the
// snapshot/delta reducer with sequence-gap detection. On a gap (§22.5) it marks
// state stale and reconnects to pull a fresh snapshot; on socket loss it
// reconnects with bounded backoff. The store owns the LiveState; this client
// owns the transport.

import { applyFrame, emptyState, type LiveState } from "../state/liveState";
import type { ServerFrame, SubscribeFrame } from "../types/records";

export type ConnectionStatus = "connecting" | "open" | "closed";

export interface WsClientCallbacks {
  onState(state: LiveState): void;
  onStatus(status: ConnectionStatus): void;
}

export interface WsClientOptions {
  url?: string;
  /** Initial reconnect backoff (ms); doubles up to maxBackoffMs. */
  baseBackoffMs?: number;
  maxBackoffMs?: number;
  /** Debounce for outbound subscribe frames (ms). Mirrors the server guard. */
  subscribeDebounceMs?: number;
}

const DEFAULT_BASE_BACKOFF = 500;
const DEFAULT_MAX_BACKOFF = 10_000;
/** ~300ms viewport/filter debounce (PRD §22.2) — above the server min-interval. */
const DEFAULT_SUBSCRIBE_DEBOUNCE = 300;

/** Resolve the ws:// URL for /ws/v2 from the current page (or an override). */
export function defaultWsUrl(): string {
  if (typeof window === "undefined") return "ws://127.0.0.1:8000/ws/v2";
  const proto = window.location.protocol === "https:" ? "wss" : "ws";
  return `${proto}://${window.location.host}/ws/v2`;
}

export class WsClient {
  private readonly url: string;
  private readonly baseBackoff: number;
  private readonly maxBackoff: number;
  private readonly subscribeDebounce: number;
  private socket: WebSocket | null = null;
  private state: LiveState = emptyState();
  private backoff: number;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private subscribeTimer: ReturnType<typeof setTimeout> | null = null;
  private closedByUser = false;
  /** Latest subscribe intent; re-sent on (re)connect so the server re-anchors. */
  private lastSubscribe: SubscribeFrame | null = null;

  constructor(
    private readonly cb: WsClientCallbacks,
    opts: WsClientOptions = {},
  ) {
    this.url = opts.url ?? defaultWsUrl();
    this.baseBackoff = opts.baseBackoffMs ?? DEFAULT_BASE_BACKOFF;
    this.maxBackoff = opts.maxBackoffMs ?? DEFAULT_MAX_BACKOFF;
    this.subscribeDebounce = opts.subscribeDebounceMs ?? DEFAULT_SUBSCRIBE_DEBOUNCE;
    this.backoff = this.baseBackoff;
  }

  connect(): void {
    this.closedByUser = false;
    this.open();
  }

  close(): void {
    this.closedByUser = true;
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    if (this.subscribeTimer) {
      clearTimeout(this.subscribeTimer);
      this.subscribeTimer = null;
    }
    this.socket?.close();
    this.socket = null;
  }

  /**
   * Record the latest subscribe intent (viewport/filter) and send it debounced.
   * Stored so a (re)connect re-sends it — every subscribe is a server resync
   * point that re-anchors a fresh filtered snapshot + cseq=0 (PRD §22.2/§22.5).
   */
  subscribe(frame: SubscribeFrame): void {
    this.lastSubscribe = frame;
    if (this.subscribeTimer) clearTimeout(this.subscribeTimer);
    this.subscribeTimer = setTimeout(() => {
      this.subscribeTimer = null;
      this.sendSubscribe();
    }, this.subscribeDebounce);
  }

  private sendSubscribe(): void {
    if (!this.lastSubscribe) return;
    if (this.socket?.readyState === WebSocket.OPEN) {
      this.socket.send(JSON.stringify(this.lastSubscribe));
    }
  }

  private open(): void {
    this.cb.onStatus("connecting");
    const socket = new WebSocket(this.url);
    this.socket = socket;

    socket.onopen = () => {
      this.backoff = this.baseBackoff;
      this.cb.onStatus("open");
      // Re-send the last subscribe intent immediately on (re)connect (no debounce):
      // the server replies with a fresh filtered snapshot and resets cseq, which
      // applyFrame treats as the resync baseline. Until the first subscribe the
      // server already serves its default station-scoped snapshot.
      this.sendSubscribe();
    };

    socket.onmessage = (ev: MessageEvent) => this.handleMessage(ev.data);

    socket.onclose = () => {
      this.cb.onStatus("closed");
      this.socket = null;
      if (!this.closedByUser) this.scheduleReconnect();
    };

    socket.onerror = () => {
      // onclose follows; let it drive reconnect. Closing here avoids a
      // half-open socket lingering on some browsers.
      socket.close();
    };
  }

  private handleMessage(data: unknown): void {
    if (typeof data !== "string") return;
    let frame: ServerFrame;
    try {
      frame = JSON.parse(data) as ServerFrame;
    } catch {
      return; // one malformed frame must not kill the stream (PRD §37)
    }
    const { state, outcome } = applyFrame(this.state, frame);
    this.state = state;
    this.cb.onState(state);
    if (outcome === "gap") {
      // §22.5: stop applying, mark stale (done in reducer), resync by
      // reconnecting — the fresh snapshot replaces authoritative state.
      this.resync();
    }
  }

  /** Reset to an empty state and reconnect to obtain a fresh snapshot. */
  private resync(): void {
    this.state = emptyState();
    this.socket?.close(); // triggers onclose → scheduleReconnect
  }

  private scheduleReconnect(): void {
    if (this.reconnectTimer) return;
    const delay = this.backoff;
    this.backoff = Math.min(this.backoff * 2, this.maxBackoff);
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null;
      if (!this.closedByUser) this.open();
    }, delay);
  }
}

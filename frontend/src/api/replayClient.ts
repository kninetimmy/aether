// Typed REST client for record/replay over persisted history (M4.8, PRD §19.6/§21.6).
//
// Replay is REST, NOT the websocket: `createReplaySession` POSTs a bounded window and
// gets back the whole reconstructed buffer once; the browser then plays it locally.
// This module is the ONLY place the replay endpoints are spoken to, and it touches
// nothing on the live path — the hard M4 invariant (replay cannot fire live alerts,
// PRD §19.6/§32) holds structurally here because there is no publish/ws/engine call.
//
// Errors are surfaced as a typed `ReplayError` (carrying the HTTP status) so the
// store can react honestly: a 503 means persistence is disabled (replay unavailable),
// a 400 means a bad/over-long window, anything else is a transport failure. A failed
// request never throws an untyped value, so the caller's catch is exhaustive.

import type {
  ReplaySessionRequest,
  ReplaySessionResponse,
} from "../types/records";

/** Base path for the replay REST API (mounted under /api/v2/replay). */
const REPLAY_BASE = "/api/v2/replay";

/** A typed replay-API failure carrying the HTTP status (0 = transport/parse error). */
export class ReplayError extends Error {
  constructor(
    message: string,
    /** HTTP status, or 0 when the request never completed / response was unparseable. */
    readonly status: number,
  ) {
    super(message);
    this.name = "ReplayError";
  }
}

/** Pull a human detail out of a non-ok JSON body ({detail: ...}); best-effort. */
async function errorDetail(res: Response): Promise<string> {
  try {
    const body: unknown = await res.json();
    if (
      body !== null &&
      typeof body === "object" &&
      "detail" in body &&
      typeof (body as { detail: unknown }).detail === "string"
    ) {
      return (body as { detail: string }).detail;
    }
  } catch {
    // Non-JSON / empty body — fall back to the status text below.
  }
  return res.statusText || `HTTP ${res.status}`;
}

/**
 * Create a replay session for a bounded `[start, end)` window and return the whole
 * reconstructed buffer. Throws {@link ReplayError} on any non-2xx (e.g. 503 when
 * persistence is off, 400 for a bad/over-long window) or on a transport/parse failure.
 */
export async function createReplaySession(
  req: ReplaySessionRequest,
): Promise<ReplaySessionResponse> {
  let res: Response;
  try {
    res = await fetch(`${REPLAY_BASE}/sessions`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(req),
    });
  } catch (err) {
    throw new ReplayError(
      `replay request failed: ${err instanceof Error ? err.message : String(err)}`,
      0,
    );
  }
  if (!res.ok) {
    throw new ReplayError(await errorDetail(res), res.status);
  }
  try {
    return (await res.json()) as ReplaySessionResponse;
  } catch (err) {
    throw new ReplayError(
      `replay response was not valid JSON: ${err instanceof Error ? err.message : String(err)}`,
      0,
    );
  }
}

/**
 * Forget a replay session server-side (DELETE). The browser drops its buffer
 * independently; this is a courtesy teardown so the server's bounded registry can
 * reclaim the slot. A 404 is treated as success (already gone). Throws
 * {@link ReplayError} only on an unexpected status or transport failure.
 */
export async function deleteReplaySession(sessionId: string): Promise<void> {
  let res: Response;
  try {
    res = await fetch(`${REPLAY_BASE}/sessions/${encodeURIComponent(sessionId)}`, {
      method: "DELETE",
    });
  } catch (err) {
    throw new ReplayError(
      `replay delete failed: ${err instanceof Error ? err.message : String(err)}`,
      0,
    );
  }
  // 204 = deleted; 404 = already gone (idempotent teardown) — both are fine.
  if (res.ok || res.status === 404) return;
  throw new ReplayError(await errorDetail(res), res.status);
}

// Pure, framework-free replay helpers (M4.8, PRD §19.6 record/replay).
//
// A replay SESSION is a bounded buffer of historical records (ascending observed_at)
// fetched once over REST and held in the browser; the timeline is played CLIENT-SIDE
// against a cursor time. This module owns the two pure pieces — the replay slice
// shape/defaults and the "what was visible at cursor T" selector — so they are
// unit-testable without React, a store, or a socket (mirrors selectors.ts).
//
// THE HARD M4 INVARIANT (PRD §19.6/§32): replay CANNOT fire live alerts. Nothing
// here publishes, mutates live state, or touches the websocket/engine — it only
// derives a read-only snapshot of an already-fetched buffer. Live data keeps flowing
// underneath untouched; entering/leaving replay never disturbs it.

import type { AnyRecord, ReplaySessionResponse } from "../types/records";

/** Mode flag: the app is showing the live firehose, or a replayed snapshot. */
export type ReplayMode = "live" | "replay";

/** Default playback speeds offered in the timeline (× real time). */
export const REPLAY_SPEEDS = [1, 2, 5, 10] as const;

/**
 * Per-source staleness (seconds) for "visible at T", mirroring the backend's live
 * fusion expiry (`src/aether/fusion/freshness.py` `DEFAULT_FRESHNESS` `expire_s`) so
 * replay shows a track for the SAME span the live map would — by source CADENCE. A
 * single flat window can't be right for both ADS-B (60 s) and APRS (2 h): it would
 * blink slow sources (APRS/AIS) off between reports even though live keeps them up.
 * Unknown sources fall back to the network-grade window, exactly like the backend's
 * fallback. KEEP IN SYNC with freshness.py.
 */
export const SOURCE_EXPIRE_S: Record<string, number> = {
  local_adsb: 60,
  demo: 60,
  network_adsb: 120,
  "demo-net": 120,
  local_aprs: 7200,
  aprs_is: 7200,
  ais: 1800,
};

/** Network-grade fallback for a source not in the table (mirrors the backend). */
export const FALLBACK_EXPIRE_S = 120;

/** Live-consistent staleness for one record, keyed by its contributing source. */
export function sourceExpireS(record: AnyRecord): number {
  return SOURCE_EXPIRE_S[record.source] ?? FALLBACK_EXPIRE_S;
}

/** The replay slice of the app store. `session === null` ⇒ no buffer loaded. */
export interface ReplaySlice {
  mode: ReplayMode;
  session: ReplaySessionResponse | null;
  /** Playback cursor in epoch ms; clamped to [startMs, endMs] of the session. */
  cursorMs: number;
  playing: boolean;
  /** Playback rate (× real time). */
  speed: number;
}

/** Initial replay slice — live mode, no session (an exact no-op over today's app). */
export function emptyReplay(): ReplaySlice {
  return {
    mode: "live",
    session: null,
    cursorMs: 0,
    playing: false,
    speed: 1,
  };
}

/** Parse an ISO instant to epoch ms; NaN when unparseable (caller guards). */
export function isoToMs(iso: string): number {
  return Date.parse(iso);
}

/** Clamp a cursor to [startMs, endMs]; tolerant of an inverted/NaN range. */
export function clampCursor(ms: number, startMs: number, endMs: number): number {
  if (Number.isNaN(ms)) return startMs;
  const lo = Math.min(startMs, endMs);
  const hi = Math.max(startMs, endMs);
  return Math.min(hi, Math.max(lo, ms));
}

/**
 * Session window bounds in epoch ms; NaN-safe. When only ONE bound is unparseable we
 * collapse to the bound that parsed (never let start and end straddle 0, which would
 * snap the cursor to 1970 and blank the map); both unparseable ⇒ [0, 0].
 */
export function sessionBoundsMs(
  session: ReplaySessionResponse | null,
): { startMs: number; endMs: number } {
  if (!session) return { startMs: 0, endMs: 0 };
  const s = isoToMs(session.start);
  const e = isoToMs(session.end);
  if (Number.isNaN(s) && Number.isNaN(e)) return { startMs: 0, endMs: 0 };
  const startMs = Number.isNaN(s) ? e : s;
  const endMs = Number.isNaN(e) ? s : e;
  return { startMs, endMs };
}

/**
 * Stable identity key for a replayed record — the same key liveState uses for its
 * keyed Maps so the replayed snapshot reduces exactly like the live one. Tracks /
 * features / alerts / events are keyed by `id` (stable per identity per source);
 * source_status is keyed by `source` (matching liveState's per-source dedup).
 */
export function replayKey(record: AnyRecord): string {
  if (record.kind === "source_status") return `source_status:${record.source}`;
  return `${record.kind}:${record.id}`;
}

/**
 * The set of records "visible at cursor T": per identity, the LATEST record whose
 * observed_at is ≤ cursor AND no older than that record's PER-SOURCE expiry (so replay
 * keeps a slow APRS/AIS track up between reports exactly as the live map does, and ages
 * a fast ADS-B track off on the same schedule). A record with an unparseable
 * observed_at is skipped (treated as not-visible) so one bad row can't leak onto the
 * replayed map (PRD §37). The session buffer is ascending observed_at, so a single
 * forward pass keeps the last-wins record per key.
 *
 * Pure and read-only: it derives a snapshot from an already-fetched buffer and never
 * touches live state — the structural half of the "replay can't fire live alerts"
 * invariant (PRD §19.6/§32).
 */
export function replayVisibleRecords(
  session: ReplaySessionResponse | null,
  cursorMs: number,
): AnyRecord[] {
  if (!session) return [];
  // Last-wins per key as we scan ascending: a later in-window observation supersedes
  // an earlier one for the same identity.
  const latest = new Map<string, AnyRecord>();
  for (const record of session.records) {
    const t = Date.parse(record.observed_at);
    if (Number.isNaN(t)) continue; // unknown timestamp → not visible (never throws)
    if (t > cursorMs) continue; // future relative to the cursor
    if (cursorMs - t > sourceExpireS(record) * 1000) continue; // past this source's expiry
    latest.set(replayKey(record), record);
  }
  return [...latest.values()];
}

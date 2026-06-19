// Authoritative client-side live state and its pure reducer (PRD §22.5).
//
// Every server frame carries two counters (M3.6b). `seq` is the GLOBAL mutation
// counter (REST/snapshot anchor) — it bumps on every backend mutation, so a
// per-connection-FILTERED client legitimately sees it SKIP. `cseq` is the
// PER-CONNECTION contiguous counter: a snapshot resets it to 0 and each delta the
// server actually sends this connection increments it by exactly 1. Gap detection
// therefore keys off `cseq`, NOT `seq` — a skipped `seq` is "filtered" (expected),
// a skipped `cseq` is "dropped" (real backpressure → resync). `seq` is retained
// only as a display/debug value. Framework-free so this is unit-testable without
// React or a socket.

import type {
  AlertRecord,
  DeltaFrame,
  EventRecord,
  GeoFeatureRecord,
  ServerFrame,
  SnapshotFrame,
  SourceStatusRecord,
  TrackRecord,
} from "../types/records";

/** Cap on the recent-events ring kept client-side (mirrors RECENT_EVENTS_MAX). */
export const RECENT_EVENTS_MAX = 256;

export interface LiveState {
  /**
   * Last GLOBAL seq seen (display/debug only). A filtered connection sees this
   * skip — it is NOT used for gap detection (see `cseq`).
   */
  seq: number;
  /**
   * Last PER-CONNECTION contiguous counter applied. -1 before the first snapshot;
   * a snapshot resets it to the frame's cseq (0). Gap detection keys off THIS.
   */
  cseq: number;
  /** True after a detected gap, until the next snapshot replaces state (§22.5). */
  stale: boolean;
  tracks: Map<string, TrackRecord>;
  features: Map<string, GeoFeatureRecord>;
  alerts: Map<string, AlertRecord>;
  /** Keyed by source name, matching the backend's per-source dedup. */
  sourceStatus: Map<string, SourceStatusRecord>;
  /** Bounded ring of recent events, newest last. */
  events: EventRecord[];
}

export function emptyState(): LiveState {
  return {
    seq: -1,
    cseq: -1,
    stale: false,
    tracks: new Map(),
    features: new Map(),
    alerts: new Map(),
    sourceStatus: new Map(),
    events: [],
  };
}

/**
 * Replace all state from a snapshot frame and clear staleness (§22.3).
 *
 * A snapshot is a resync point: it re-anchors the per-connection `cseq` baseline
 * to the frame's `cseq` (the server resets it to 0 on every subscribe), so the
 * next delta must be `cseq + 1`.
 */
export function applySnapshot(frame: SnapshotFrame): LiveState {
  const events = frame.events.slice(-RECENT_EVENTS_MAX);
  return {
    seq: frame.seq,
    cseq: frame.cseq,
    stale: false,
    tracks: new Map(frame.tracks.map((r) => [r.id, r])),
    features: new Map(frame.features.map((r) => [r.id, r])),
    alerts: new Map(frame.alerts.map((r) => [r.id, r])),
    sourceStatus: new Map(frame.source_status.map((r) => [r.source, r])),
    events,
  };
}

/** What `applyDelta` decided to do — surfaced so the socket can trigger resync. */
export type DeltaOutcome = "applied" | "duplicate" | "gap";

export interface DeltaResult {
  state: LiveState;
  outcome: DeltaOutcome;
}

/**
 * Apply one delta to the current state, enforcing PER-CONNECTION continuity.
 *
 * Continuity is checked on `cseq` (NOT `seq` — a filtered connection's `seq`
 * skips by design; only a `cseq` skip is a real drop):
 *
 * - cseq === current+1 → apply (returns a new state object).
 * - cseq <= current    → duplicate/stale replay; ignore, state unchanged.
 * - cseq  > current+1  → gap; mark stale and DO NOT apply. The caller must
 *   resynchronize (request a fresh snapshot or reconnect) per §22.5.
 */
export function applyDelta(state: LiveState, frame: DeltaFrame): DeltaResult {
  const expected = state.cseq + 1;
  if (frame.cseq <= state.cseq) {
    return { state, outcome: "duplicate" };
  }
  if (frame.cseq !== expected) {
    return { state: { ...state, stale: true }, outcome: "gap" };
  }

  const next: LiveState = {
    ...state,
    seq: frame.seq,
    cseq: frame.cseq,
    tracks: state.tracks,
    features: state.features,
    alerts: state.alerts,
    sourceStatus: state.sourceStatus,
    events: state.events,
  };

  switch (frame.type) {
    case "track_upsert":
      next.tracks = new Map(state.tracks).set(frame.record.id, frame.record);
      break;
    case "feature_upsert":
      next.features = new Map(state.features).set(frame.record.id, frame.record);
      break;
    case "alert_upsert":
      next.alerts = new Map(state.alerts).set(frame.record.id, frame.record);
      break;
    case "source_status":
      next.sourceStatus = new Map(state.sourceStatus).set(
        frame.record.source,
        frame.record,
      );
      break;
    case "event": {
      const events = [...state.events, frame.record];
      next.events =
        events.length > RECENT_EVENTS_MAX
          ? events.slice(-RECENT_EVENTS_MAX)
          : events;
      break;
    }
    case "remove":
      // Only tracks/features/alerts are keyed/removable (PRD §22.4); events and
      // source_status are never removed. Handle each explicitly to stay type-safe.
      if (frame.kind === "track") {
        const m = new Map(state.tracks);
        m.delete(frame.id);
        next.tracks = m;
      } else if (frame.kind === "feature") {
        const m = new Map(state.features);
        m.delete(frame.id);
        next.features = m;
      } else if (frame.kind === "alert") {
        const m = new Map(state.alerts);
        m.delete(frame.id);
        next.alerts = m;
      }
      break;
  }
  return { state: next, outcome: "applied" };
}

/** Apply any server frame; snapshots replace, deltas reduce. */
export function applyFrame(state: LiveState, frame: ServerFrame): DeltaResult {
  if (frame.type === "snapshot") {
    return { state: applySnapshot(frame), outcome: "applied" };
  }
  return applyDelta(state, frame);
}

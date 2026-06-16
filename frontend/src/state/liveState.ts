// Authoritative client-side live state and its pure reducer (PRD §22.5).
//
// The backend bumps a monotonic sequence by exactly 1 per mutation and stamps
// every frame with the new value (see src/aether/state/sequence.py). A snapshot
// carries the current seq; the next delta must be seq+1, and each subsequent
// delta increments by 1. This module is deliberately framework-free so the
// gap-detection/resync logic can be unit-tested without React or a socket.

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
  /** Last sequence number successfully applied. -1 before the first snapshot. */
  seq: number;
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
    stale: false,
    tracks: new Map(),
    features: new Map(),
    alerts: new Map(),
    sourceStatus: new Map(),
    events: [],
  };
}

/** Replace all state from a snapshot frame and clear staleness (§22.3). */
export function applySnapshot(frame: SnapshotFrame): LiveState {
  const events = frame.events.slice(-RECENT_EVENTS_MAX);
  return {
    seq: frame.seq,
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
 * Apply one delta to the current state, enforcing sequence continuity.
 *
 * - seq === current+1 → apply (returns a new state object).
 * - seq <= current    → duplicate/stale replay; ignore, state unchanged.
 * - seq  > current+1   → gap; mark stale and DO NOT apply. The caller must
 *   resynchronize (request a fresh snapshot or reconnect) per §22.5.
 */
export function applyDelta(state: LiveState, frame: DeltaFrame): DeltaResult {
  const expected = state.seq + 1;
  if (frame.seq <= state.seq) {
    return { state, outcome: "duplicate" };
  }
  if (frame.seq !== expected) {
    return { state: { ...state, stale: true }, outcome: "gap" };
  }

  const next: LiveState = {
    ...state,
    seq: frame.seq,
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

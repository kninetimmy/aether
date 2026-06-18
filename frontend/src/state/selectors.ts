// Pure, framework-free display selectors (PRD §16.5, §8.2).
//
// The provenance filter is DISPLAY ONLY — it changes which tracks render, never
// what the backend ingests or fuses. "local" is the flagship collapse-to-
// local-only view: show only what the operator's own antenna is currently hearing.
//
// Crucially this reads `locally_received`, which the backend recomputes on every
// fuse: it is true while a non-expired local-RF contributor exists and flips to
// false once the local radio goes quiet and a network observation continues the
// track (FUSION-FR-004). So "local" naturally includes a stale-but-live local
// contributor, and "network" naturally includes a network-continuation handoff —
// no extra client logic needed.

import type { ProvenanceFilter } from "./store";
import type { TrackRecord } from "../types/records";

/** Whether a track is visible under the given provenance filter. */
export function trackMatchesProvenance(
  track: TrackRecord,
  filter: ProvenanceFilter,
): boolean {
  switch (filter) {
    case "all":
      return true;
    case "local":
      return track.locally_received;
    case "network":
      return !track.locally_received;
  }
}

/**
 * Filter a tracks Map to those matching the provenance filter, as a list.
 * Pure: never mutates its input.
 */
export function visibleTracks(
  tracks: Map<string, TrackRecord>,
  filter: ProvenanceFilter,
): TrackRecord[] {
  const out: TrackRecord[] = [];
  for (const track of tracks.values()) {
    if (trackMatchesProvenance(track, filter)) out.push(track);
  }
  return out;
}

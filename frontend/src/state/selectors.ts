// Pure, framework-free display selectors (PRD §16.5, §8.2).
//
// DISPLAY ONLY — these change which tracks render, never what the backend ingests
// or fuses (the untouched liveState.ts / wsClient.ts ingestion boundary is the
// proof). visibleTracks ANDs a chain of small pure predicates; only predicates
// whose DisplayFilters field is ACTIVE participate, so an all-null/any
// DisplayFilters is an exact no-op and first-load behavior is identical to today.
//
// Predicate contract (PRD §37 isolation):
//   - an INACTIVE criterion returns true (does not participate);
//   - an ACTIVE criterion treats a missing/wrong-typed attribute as "unknown"
//     (no-match) rather than throwing.
//
// The provenance predicate reads `locally_received`, which the backend recomputes
// on every fuse: true while a non-expired local-RF contributor exists, false once
// the local radio goes quiet and a network observation continues the track
// (FUSION-FR-004). So "local" naturally includes a stale-but-live local
// contributor and "network" a network-continuation handoff — no client logic.

import type { DisplayFilters, ProvenanceFilter } from "./store";
import {
  aisAttr,
  aisIntAttr,
  fusionMeta,
  trackAgeS,
  type TrackRecord,
  type TrackType,
} from "../types/records";

/**
 * Context passed into the predicates so visibleTracks stays PURE / clock-free /
 * store-free: the wall clock (`now`) and the runtime station origin are injected
 * by the caller (the store clock tick + runtime config), never read here.
 */
export interface FilterCtx {
  now: number;
  stationCenter: { lon: number; lat: number } | null;
  watchlist: Set<string>;
}

// --- Provenance (existing; kept as ONE predicate so the radiogroup, the "N of M"
// heading, and the existing selectors.test.ts assertions are all preserved) ---

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

export function matchesProvenance(track: TrackRecord, filters: DisplayFilters): boolean {
  return trackMatchesProvenance(track, filters.provenance);
}

// --- live-LOCAL-only (T27) -------------------------------------------------
// "Live on my antenna right now" — locally_received AND a contributor that is
// local_rf with freshness 'live'. Keying off a live local contributor (NOT
// last_local_rf_at alone) means a long-quiet local target is never mislabeled
// 'live'. A None-correlation-key track has no fusion block: fall back to
// locally_received (treat missing fusion as "unknown leg", not no-match).

export function matchesLiveLocal(track: TrackRecord, filters: DisplayFilters): boolean {
  if (!filters.liveLocalOnly) return true;
  if (!track.locally_received) return false;
  const meta = fusionMeta(track);
  if (!meta) return track.locally_received; // unfused local leg: trust top-level flag
  // Belt-and-suspenders: tolerate a malformed contributors array / null elements
  // (PRD §37 — an active criterion treats a bad attribute as unknown, never throws).
  const contributors = Array.isArray(meta.contributors) ? meta.contributors : [];
  return contributors.some((c) => c != null && c.local_rf && c.freshness === "live");
}

// --- Source --------------------------------------------------------------

export function matchesSource(track: TrackRecord, filters: DisplayFilters): boolean {
  if (filters.sources === null) return true;
  return filters.sources.has(track.source);
}

// --- Track type ----------------------------------------------------------

export function matchesTrackType(track: TrackRecord, filters: DisplayFilters): boolean {
  if (filters.trackTypes === null) return true;
  return filters.trackTypes.has(track.track_type);
}

// --- Range from station (haversine) --------------------------------------
// PASS (no-op) when the station origin is unset — the range control degrades to
// a disabled no-op (canonical station config arrives in M3.6b). PASS also when
// the criterion is inactive or the track has no point geometry.

const EARTH_RADIUS_M = 6_371_000;

/** Great-circle distance in metres between two [lon,lat] points. */
export function haversineM(
  a: { lon: number; lat: number },
  b: { lon: number; lat: number },
): number {
  const toRad = (d: number) => (d * Math.PI) / 180;
  const dLat = toRad(b.lat - a.lat);
  const dLon = toRad(b.lon - a.lon);
  const lat1 = toRad(a.lat);
  const lat2 = toRad(b.lat);
  const h =
    Math.sin(dLat / 2) ** 2 +
    Math.cos(lat1) * Math.cos(lat2) * Math.sin(dLon / 2) ** 2;
  return 2 * EARTH_RADIUS_M * Math.asin(Math.min(1, Math.sqrt(h)));
}

const NM_TO_M = 1852;

export function withinRange(
  track: TrackRecord,
  filters: DisplayFilters,
  ctx: FilterCtx,
): boolean {
  if (filters.rangeNmMax === null) return true;
  if (ctx.stationCenter === null) return true; // disabled no-op until station set
  if (!track.geometry) return false; // active criterion, unknown position → no-match
  const [lon, lat] = track.geometry.coordinates;
  const d = haversineM(ctx.stationCenter, { lon, lat });
  return d <= filters.rangeNmMax * NM_TO_M;
}

// --- Altitude / speed band -----------------------------------------------

export function withinAltitude(track: TrackRecord, filters: DisplayFilters): boolean {
  const { altitudeMinM, altitudeMaxM } = filters;
  if (altitudeMinM === null && altitudeMaxM === null) return true;
  if (track.altitude_m === null) return false; // active band, unknown altitude → no-match
  if (altitudeMinM !== null && track.altitude_m < altitudeMinM) return false;
  if (altitudeMaxM !== null && track.altitude_m > altitudeMaxM) return false;
  return true;
}

export function withinSpeed(track: TrackRecord, filters: DisplayFilters): boolean {
  const { speedMinMps, speedMaxMps } = filters;
  if (speedMinMps === null && speedMaxMps === null) return true;
  if (track.speed_mps === null) return false; // active band, unknown speed → no-match
  if (speedMinMps !== null && track.speed_mps < speedMinMps) return false;
  if (speedMaxMps !== null && track.speed_mps > speedMaxMps) return false;
  return true;
}

// --- Age (now − observed_at) ---------------------------------------------
// PASS when observed_at is missing/unparseable (unknown leg, not no-match) so a
// just-acquired track without a clean timestamp isn't silently dropped.

export function withinAge(
  track: TrackRecord,
  filters: DisplayFilters,
  ctx: FilterCtx,
): boolean {
  if (filters.ageMaxS === null) return true;
  const age = trackAgeS(track, ctx.now);
  if (age === undefined) return true; // unknown observed_at → pass, don't hide
  return age <= filters.ageMaxS;
}

// --- Military classification ---------------------------------------------
// Honest labeling (MIL-FR-005): we filter on classification.military and the
// classification.basis, but never imply certainty — the UI surfaces basis +
// confidence via militaryBadge / MIL_BASIS_LABEL. A null classification means
// "unknown": it matches neither "military" nor "civil" when those are active.

export function matchesMilitary(track: TrackRecord, filters: DisplayFilters): boolean {
  if (filters.military === "any") return true;
  const mil = track.classification?.military;
  if (filters.military === "military") return mil === true;
  // "civil": only tracks affirmatively flagged non-military (mil === false);
  // an unknown (null) classification is NOT asserted civil.
  return mil === false;
}

export function matchesMilitaryBasis(
  track: TrackRecord,
  filters: DisplayFilters,
): boolean {
  if (filters.militaryBasis === null) return true;
  const basis = track.classification?.basis ?? "unknown";
  return filters.militaryBasis.has(basis);
}

// --- AIS attribute filters (read defensively from track.attributes) -------
// ship_type / nav_status filter on the raw INT code (stable); *_text is display
// only. name / mmsi / destination are case-insensitive substring matches.

export function matchesAisVesselType(
  track: TrackRecord,
  filters: DisplayFilters,
): boolean {
  if (filters.ais.vesselTypes === null) return true;
  const code = aisIntAttr(track, "ship_type");
  if (code === undefined) return false; // active, no code → unknown → no-match
  return filters.ais.vesselTypes.has(code);
}

export function matchesAisNavStatus(
  track: TrackRecord,
  filters: DisplayFilters,
): boolean {
  if (filters.ais.navStatuses === null) return true;
  const code = aisIntAttr(track, "nav_status");
  if (code === undefined) return false;
  return filters.ais.navStatuses.has(code);
}

function substringMatch(haystack: string | undefined, needle: string): boolean {
  if (haystack === undefined) return false; // active, missing → unknown → no-match
  return haystack.toLowerCase().includes(needle.toLowerCase());
}

export function matchesAisName(track: TrackRecord, filters: DisplayFilters): boolean {
  if (filters.ais.nameLike === null || filters.ais.nameLike === "") return true;
  return substringMatch(aisAttr(track, "vessel_name"), filters.ais.nameLike);
}

export function matchesAisMmsi(track: TrackRecord, filters: DisplayFilters): boolean {
  if (filters.ais.mmsiLike === null || filters.ais.mmsiLike === "") return true;
  return substringMatch(aisAttr(track, "mmsi"), filters.ais.mmsiLike);
}

export function matchesAisDestination(
  track: TrackRecord,
  filters: DisplayFilters,
): boolean {
  if (filters.ais.destinationLike === null || filters.ais.destinationLike === "")
    return true;
  return substringMatch(aisAttr(track, "destination"), filters.ais.destinationLike);
}

// --- APRS callsign (substring on track.label; no adapter change) ----------

export function matchesAprsCallsign(
  track: TrackRecord,
  filters: DisplayFilters,
): boolean {
  if (filters.aprsCallsignLike === null || filters.aprsCallsignLike === "") return true;
  return substringMatch(track.label ?? undefined, filters.aprsCallsignLike);
}

// --- Orbital (M6.6a) ------------------------------------------------------
// CelesTrak GP objects are propagated to predicted orbital_object tracks (M6.5);
// these two predicates let the operator narrow that overlay WITHIN the backend-
// transmitted set. The cardinal rule: BOTH predicates are no-ops for every
// non-orbital track — selecting a category or elevation floor must NEVER hide an
// aircraft/vessel/APRS target. As with every active criterion (PRD §37), an
// orbital track with a missing/wrong-typed attribute reads as "unknown" → no-match.

/** Defensive number attribute read; undefined when absent or not a finite number. */
function numAttr(track: TrackRecord, key: string): number | undefined {
  const raw = track.attributes?.[key];
  return typeof raw === "number" && Number.isFinite(raw) ? raw : undefined;
}

export function matchesOrbitalCategory(
  track: TrackRecord,
  filters: DisplayFilters,
): boolean {
  if (filters.orbitalCategory === null) return true; // inactive → no-op
  if (track.track_type !== "orbital_object") return true; // non-orbital always passes
  const group = strAttr(track, "group");
  if (group === undefined) return false; // active, unknown group → no-match
  return filters.orbitalCategory.has(group);
}

export function matchesOrbitalElevation(
  track: TrackRecord,
  filters: DisplayFilters,
): boolean {
  if (filters.orbitalMinElevationDeg === null) return true; // inactive → no-op
  if (track.track_type !== "orbital_object") return true; // non-orbital always passes
  const elev = numAttr(track, "elevation_deg");
  if (elev === undefined) return false; // active, unknown elevation → no-match
  return elev >= filters.orbitalMinElevationDeg; // inclusive
}

// --- Watchlist (TOI; M3.6c) -----------------------------------------------
// STABLE keying is the whole point: the raw track.id is source-prefixed and
// ephemeral (it changes on a LOCAL→NET fusion handoff and can differ across a
// reconnect), so persisting it would lose the highlight exactly when the target
// matters most. watchlistKey mirrors the backend's fusion identity: prefer the
// correlation_key the FusionEngine already assigns; otherwise derive a domain key
// from the track type (matching the adapters' id/correlation_key conventions —
// readsb `aircraft:icao:<hex>`, ais `ais:vessel:<mmsi>`, aprs the callsign), only
// falling back to the raw id when no stable identity can be formed.

/** Defensive string attribute read (records.ts' aisAttr, inlined for non-AIS keys). */
function strAttr(track: TrackRecord, key: string): string | undefined {
  const raw = track.attributes?.[key];
  return typeof raw === "string" && raw.length > 0 ? raw : undefined;
}

/**
 * A STABLE identity key for a track — what the watchlist persists, NOT the raw
 * ephemeral track.id. Survives reconnect and LOCAL→NET fusion handoff because the
 * backend fuses same-identity observations under one correlation_key.
 */
export function watchlistKey(track: TrackRecord): string {
  if (track.correlation_key) return track.correlation_key;
  switch (track.track_type) {
    case "aircraft": {
      const icao = strAttr(track, "icao") ?? strAttr(track, "hex");
      if (icao) return `aircraft:icao:${icao.toLowerCase()}`;
      break;
    }
    case "vessel": {
      const mmsi = strAttr(track, "mmsi");
      if (mmsi) return `mmsi:${mmsi}`;
      break;
    }
    case "aprs_station":
    case "aprs_object": {
      if (track.label) return `aprs:${track.label}`;
      break;
    }
    default:
      break;
  }
  return track.id;
}

/**
 * Pure membership predicate feeding BOTH the watchlistOnly display filter and the
 * map/list TOI highlight. Keys off the stable watchlistKey, never the raw id.
 */
export function isOnWatchlist(track: TrackRecord, watchlist: Set<string>): boolean {
  return watchlist.has(watchlistKey(track));
}

export function matchesWatchlist(
  track: TrackRecord,
  filters: DisplayFilters,
  ctx: FilterCtx,
): boolean {
  if (!filters.watchlistOnly) return true;
  return isOnWatchlist(track, ctx.watchlist);
}

// --- Composition: AND only the active predicates --------------------------

/**
 * Filter a tracks Map to those passing EVERY active predicate, as a list. Pure:
 * never mutates its input. An all-null/any DisplayFilters is an exact no-op
 * (returns every track), preserving today's first-load behavior.
 */
export function visibleTracks(
  tracks: Map<string, TrackRecord>,
  filters: DisplayFilters,
  ctx: FilterCtx,
): TrackRecord[] {
  const out: TrackRecord[] = [];
  for (const track of tracks.values()) {
    if (
      matchesProvenance(track, filters) &&
      matchesLiveLocal(track, filters) &&
      matchesSource(track, filters) &&
      matchesTrackType(track, filters) &&
      withinRange(track, filters, ctx) &&
      withinAltitude(track, filters) &&
      withinSpeed(track, filters) &&
      withinAge(track, filters, ctx) &&
      matchesMilitary(track, filters) &&
      matchesMilitaryBasis(track, filters) &&
      matchesAisVesselType(track, filters) &&
      matchesAisNavStatus(track, filters) &&
      matchesAisName(track, filters) &&
      matchesAisMmsi(track, filters) &&
      matchesAisDestination(track, filters) &&
      matchesAprsCallsign(track, filters) &&
      matchesOrbitalCategory(track, filters) &&
      matchesOrbitalElevation(track, filters) &&
      matchesWatchlist(track, filters, ctx)
    ) {
      out.push(track);
    }
  }
  return out;
}

/** Track types present in a live tracks map, for filter-control population. */
export function activeTrackTypes(tracks: Map<string, TrackRecord>): TrackType[] {
  const set = new Set<TrackType>();
  for (const t of tracks.values()) set.add(t.track_type);
  return [...set].sort();
}

/** Distinct sources present in a live tracks map, for filter-control population. */
export function activeSources(tracks: Map<string, TrackRecord>): string[] {
  const set = new Set<string>();
  for (const t of tracks.values()) set.add(t.source);
  return [...set].sort();
}

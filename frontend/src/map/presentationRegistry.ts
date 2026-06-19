// Centralized presentation registry (PRD §5, §24, §37 guardrail).
//
// The ONLY place source-/kind-specific visual styling lives. The backend stays
// generic and the map layers stay generic; everything that decides "what does an
// aircraft look like vs. a vessel vs. a TFR" is resolved here. Unknown track or
// feature types fall back to a generic style rather than failing — new sources
// render as a neutral marker until someone gives them an entry.

import type {
  AnyRecord,
  Classification,
  FeatureType,
  GeoFeatureRecord,
  Severity,
  SourceState,
  TrackRecord,
  TrackType,
} from "../types/records";

export interface Presentation {
  /** Stable key used to group records into a map layer. */
  layer: string;
  /** Human label for legends / layer control. */
  label: string;
  /** Fill/stroke color (hex). */
  color: string;
  /** Symbol hint for the renderer (circle today; sprite later). */
  symbol: "aircraft" | "vessel" | "station" | "balloon" | "satellite" | "dot" | "area";
  /** Whether this symbol should rotate to `heading_deg`. */
  rotateByHeading: boolean;
}

const GENERIC_TRACK: Presentation = {
  layer: "tracks-other",
  label: "Other tracks",
  color: "#9aa6b2",
  symbol: "dot",
  rotateByHeading: false,
};

const TRACK_PRESENTATION: Record<TrackType, Presentation> = {
  aircraft: {
    layer: "tracks-aircraft",
    label: "Aircraft",
    color: "#62d0ff",
    symbol: "aircraft",
    rotateByHeading: true,
  },
  vessel: {
    layer: "tracks-vessel",
    label: "Vessels",
    color: "#5ce6b4",
    symbol: "vessel",
    rotateByHeading: true,
  },
  aprs_station: {
    layer: "tracks-aprs",
    label: "APRS stations",
    color: "#ffb347",
    symbol: "station",
    rotateByHeading: false,
  },
  aprs_object: {
    layer: "tracks-aprs",
    label: "APRS objects",
    color: "#ffd27f",
    symbol: "station",
    rotateByHeading: false,
  },
  radiosonde: {
    layer: "tracks-sonde",
    label: "Radiosondes",
    color: "#c79bff",
    symbol: "balloon",
    rotateByHeading: false,
  },
  orbital_object: {
    layer: "tracks-orbital",
    label: "Orbital objects",
    color: "#ff7eb6",
    symbol: "satellite",
    rotateByHeading: false,
  },
  other: GENERIC_TRACK,
};

const GENERIC_FEATURE: Presentation = {
  layer: "features-other",
  label: "Other areas",
  color: "#8893a0",
  symbol: "area",
  rotateByHeading: false,
};

const FEATURE_PRESENTATION: Record<FeatureType, Presentation> = {
  lightning_flash: { layer: "features-lightning", label: "Lightning", color: "#ffe066", symbol: "dot", rotateByHeading: false },
  lightning_cluster: { layer: "features-lightning", label: "Lightning clusters", color: "#ffd11a", symbol: "area", rotateByHeading: false },
  fire_detection: { layer: "features-fire", label: "Fire detections", color: "#ff5a36", symbol: "dot", rotateByHeading: false },
  earthquake: { layer: "features-quake", label: "Earthquakes", color: "#d98c4a", symbol: "dot", rotateByHeading: false },
  tfr: { layer: "features-tfr", label: "TFRs", color: "#ff4d6d", symbol: "area", rotateByHeading: false },
  notam_geometry: { layer: "features-notam", label: "NOTAM areas", color: "#ff8fa3", symbol: "area", rotateByHeading: false },
  predicted_landing: { layer: "features-landing", label: "Predicted landings", color: "#c79bff", symbol: "dot", rotateByHeading: false },
  geofence: { layer: "features-geofence", label: "Geofences", color: "#6ee7ff", symbol: "area", rotateByHeading: false },
  other: GENERIC_FEATURE,
};

export function trackPresentation(track: TrackRecord): Presentation {
  return TRACK_PRESENTATION[track.track_type] ?? GENERIC_TRACK;
}

export function featurePresentation(feature: GeoFeatureRecord): Presentation {
  return FEATURE_PRESENTATION[feature.feature_type] ?? GENERIC_FEATURE;
}

/** Resolve presentation for any track/feature record; null for non-spatial kinds. */
export function presentationFor(record: AnyRecord): Presentation | null {
  if (record.kind === "track") return trackPresentation(record);
  if (record.kind === "feature") return featurePresentation(record);
  return null;
}

// --- Status & severity color ramps (color is never the only channel; §24.9) --

const SOURCE_STATE_COLOR: Record<SourceState, string> = {
  starting: "#9aa6b2",
  connected: "#5ce6b4",
  degraded: "#ffd27f",
  stale: "#ffb347",
  offline: "#ff6b6b",
  disabled: "#6b7280",
};

export function sourceStateColor(state: SourceState): string {
  return SOURCE_STATE_COLOR[state] ?? "#9aa6b2";
}

const SEVERITY_COLOR: Record<Severity, string> = {
  info: "#62d0ff",
  low: "#5ce6b4",
  medium: "#ffd27f",
  high: "#ff9f43",
  critical: "#ff4d6d",
};

export function severityColor(severity: Severity): string {
  return SEVERITY_COLOR[severity] ?? "#9aa6b2";
}

// --- Military classification badge (PRD §11.5) -----------------------------
// Honest labeling: a track is shown as military ONLY on a provider-DB flag or an
// ICAO address-block match (never a movement/callsign heuristic — MIL-FR-004), and
// the language is deliberately hedged because no basis is authoritative
// (MIL-FR-005). The badge is the centralized presentation for that — the list/map
// never builds the string itself.

// Exported so the military-basis filter control (FilterPanel) reuses the SAME
// honest, hedged basis labels the badge tooltip uses — one source of truth, no
// parallel string built in a component (MIL-FR-005).
export const MIL_BASIS_LABEL: Record<Classification["basis"], string> = {
  provider: "provider database flag",
  address_block: "ICAO address-block match",
  both: "provider flag + address-block match",
  unknown: "unspecified basis",
};

export interface MilitaryBadge {
  /** Short, hedged badge text. */
  text: string;
  /** Tooltip naming the basis + confidence, with no certainty language. */
  title: string;
}

/** Honest military badge for a track, or null when it is not flagged military. */
export function militaryBadge(
  classification: Classification | null | undefined,
): MilitaryBadge | null {
  if (!classification || classification.military !== true) return null;
  const basis = MIL_BASIS_LABEL[classification.basis] ?? MIL_BASIS_LABEL.unknown;
  return {
    text: "MIL?",
    title: `Reported military — ${basis} (confidence: ${classification.confidence}). Not authoritative.`,
  };
}

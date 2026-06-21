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

// --- TOI watchlist highlight (PRD §24.6) -----------------------------------
// Centralized so the map ring and the list/panel badge share one source of truth
// — components stay dumb and never hardcode the highlight color/width/glyph.

export interface ToiHighlight {
  /** Ring color (hex) drawn around a watchlisted track. */
  color: string;
  /** Ring stroke width (px) for the dedicated tracks-highlight layer. */
  width: number;
  /** Ring radius (px); sits just outside the tracks-point circle. */
  radius: number;
  /** Star glyph for the list/panel watchlist badge. */
  badge: string;
}

const TOI_HIGHLIGHT: ToiHighlight = {
  color: "#ffd400",
  width: 2,
  radius: 9,
  badge: "★",
};

/** The single TOI highlight style (ring + badge). */
export function toiHighlight(): ToiHighlight {
  return TOI_HIGHLIGHT;
}

// --- Lightning clustering (PRD §24.3, §2061; LIGHTNING-FR-006) --------------
// Dense GLM flash points are clustered client-side so a storm stays legible at
// low zoom (LIGHTNING-FR-006). All lightning styling lives here (centralized
// presentation, §5): the map component owns the MapLibre source/layer/expression
// shapes, but reads every color and size from this one place. A cluster bubble
// encodes its flash count three ways — bigger radius, hotter color, AND a printed
// count — so color is never the only channel (§24.9). Counting flashes is an
// honest aggregate: it never restates GLM's total-lightning caveat (a cluster of
// flashes is still not a count of confirmed cloud-to-ground strikes, LIGHTNING-FR-004).

export interface LightningStyle {
  /** An individual, unclustered flash dot. */
  flashColor: string;
  flashRadius: number;
  /**
   * Cluster bubble color by contained flash count (storm-intensity proxy).
   * `base` is the sub-`steps[0][0]` color; each [count, color] raises the color
   * at/above that count. Feeds a MapLibre `step` expression over `point_count`.
   */
  clusterColor: { base: string; steps: [number, string][] };
  /** Cluster bubble radius (px) by flash count; same `step` shape as the color. */
  clusterRadius: { base: number; steps: [number, number][] };
  /** Color of the count label printed on a cluster bubble. */
  countColor: string;
}

const LIGHTNING_STYLE: LightningStyle = {
  // A lone flash reuses the registry's lightning_flash color so clustered and
  // unclustered lightning read as one layer.
  flashColor: FEATURE_PRESENTATION.lightning_flash.color,
  flashRadius: 3,
  clusterColor: {
    base: FEATURE_PRESENTATION.lightning_flash.color, // few flashes — calm yellow
    steps: [
      [10, FEATURE_PRESENTATION.lightning_cluster.color],
      [50, "#ff9f1a"],
      [200, "#ff5a1a"], // a dense, active cell — hot orange
    ],
  },
  clusterRadius: {
    base: 12,
    steps: [
      [10, 16],
      [50, 22],
      [200, 30],
    ],
  },
  countColor: "#1a1205", // dark ink legible on the yellow→orange bubbles
};

/** The single lightning-clustering style (flash dot + cluster ramps + label). */
export function lightningStyle(): LightningStyle {
  return LIGHTNING_STYLE;
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

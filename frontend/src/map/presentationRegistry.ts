// Centralized presentation registry (PRD §5, §24, §37 guardrail).
//
// The ONLY place source-/kind-specific visual styling lives. The backend stays
// generic and the map layers stay generic; everything that decides "what does an
// aircraft look like vs. a vessel vs. a TFR" is resolved here. Unknown track or
// feature types fall back to a generic style rather than failing — new sources
// render as a neutral marker until someone gives them an entry.

import { numAttr, objAttr, strAttr } from "../types/records";
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

// --- Per-type track detail extraction (PRD §24.6) --------------------------
// "What does THIS record actually contain?" lives here, with the rest of the
// per-type knowledge — the details panel stays dumb and just renders the groups.
// Every field is pulled through a DEFENSIVE reader and emitted only when present,
// so a missing/wrong-typed attribute is silently omitted rather than invented or
// crashing (PRD §37). Labels match the adapter-normalized attribute keys
// (aprs.py weather block, adsb_provider.py `r`/`t`/`category`, sondehub.py,
// celestrak.py) — no field is shown that the adapter didn't actually set.

/** One labelled value in the details panel; `title` is an optional tooltip. */
export interface DetailField {
  label: string;
  value: string;
  title?: string;
}

/** A titled group of detail fields (e.g. "Weather", "Position & motion"). */
export interface DetailGroup {
  heading?: string;
  fields: DetailField[];
}

/** Short packet-kind badge for an APRS track ("Weather"/"Status"/…), or null. */
export interface PacketKind {
  text: string;
  title: string;
}

const M_TO_FT = 3.28084;
const MPS_TO_KT = 1.94384;
const MPS_TO_FPM = 196.8504;

function fmt(n: number, digits = 0): string {
  return n.toLocaleString("en-US", {
    maximumFractionDigits: digits,
    minimumFractionDigits: 0,
  });
}

/** Human element/observation age — seconds → "45 s" / "12 min" / "3.4 h" / "2.1 d". */
function humanAge(s: number): string {
  if (s < 90) return `${Math.round(s)} s`;
  if (s < 5400) return `${Math.round(s / 60)} min`;
  if (s < 172800) return `${(s / 3600).toFixed(1)} h`;
  return `${(s / 86400).toFixed(1)} d`;
}

/** Signed human time-delta from now: future "in 12 min", past "12 min ago", "now". */
function humanRelative(deltaS: number): string {
  if (Math.abs(deltaS) < 1) return "now";
  const mag = humanAge(Math.abs(deltaS));
  return deltaS >= 0 ? `in ${mag}` : `${mag} ago`;
}

function pushStr(out: DetailField[], track: TrackRecord, key: string, label: string): void {
  const v = strAttr(track, key);
  if (v && v.trim()) out.push({ label, value: v });
}

function pushNum(
  out: DetailField[],
  track: TrackRecord,
  key: string,
  label: string,
  unit: string,
  digits: number,
): void {
  const v = numAttr(track, key);
  if (v == null) return;
  const sep = unit.startsWith("°") || unit === "%" || unit === "" ? "" : " ";
  out.push({ label, value: `${fmt(v, digits)}${sep}${unit}` });
}

/** Id-like field that may arrive as a string OR a bare number (mmsi, norad_id). */
function pushId(out: DetailField[], track: TrackRecord, key: string, label: string): void {
  const raw = track.attributes[key];
  if (typeof raw === "string" && raw.trim()) out.push({ label, value: raw });
  else if (typeof raw === "number" && Number.isFinite(raw))
    out.push({ label, value: String(raw) });
}

/** Lat, lon (and altitude folded into the motion group) from the point geometry. */
function positionField(track: TrackRecord): DetailField | null {
  const g = track.geometry;
  if (!g) return null;
  const [lon, lat] = g.coordinates;
  return { label: "Position", value: `${lat.toFixed(4)}, ${lon.toFixed(4)}` };
}

/** Altitude / speed / heading / vertical-rate, in aviation units, when present. */
function motionFields(track: TrackRecord): DetailField[] {
  const out: DetailField[] = [];
  if (track.altitude_m != null)
    out.push({
      label: "Altitude",
      value: `${fmt(track.altitude_m * M_TO_FT)} ft (${fmt(track.altitude_m)} m)`,
    });
  if (track.speed_mps != null)
    out.push({ label: "Ground speed", value: `${fmt(track.speed_mps * MPS_TO_KT)} kt` });
  if (track.heading_deg != null)
    out.push({ label: "Heading", value: `${Math.round(track.heading_deg)}°` });
  if (track.vertical_rate_mps != null) {
    const fpm = track.vertical_rate_mps * MPS_TO_FPM;
    const arrow = fpm > 50 ? " ↑" : fpm < -50 ? " ↓" : "";
    out.push({ label: "Vertical rate", value: `${fmt(Math.abs(fpm))} ft/min${arrow}` });
  }
  return out;
}

// ADS-B emitter category → human size/kind class (DO-260 / adsb.fi `category`).
const AIRCRAFT_CATEGORY: Record<string, string> = {
  A0: "No category info",
  A1: "Light (<15,500 lb)",
  A2: "Small (15,500–75,000 lb)",
  A3: "Large (75,000–300,000 lb)",
  A4: "High-vortex large (e.g. B757)",
  A5: "Heavy (>300,000 lb)",
  A6: "High performance",
  A7: "Rotorcraft",
  B1: "Glider / sailplane",
  B2: "Lighter-than-air",
  B3: "Parachutist",
  B4: "Ultralight / paraglider",
  B6: "UAV",
  B7: "Space / trans-atmospheric",
  C1: "Surface — emergency vehicle",
  C2: "Surface — service vehicle",
  C3: "Point obstacle",
};

// APRS symbol code (2nd char) → human hint, for the common primary-table symbols.
const APRS_SYMBOL: Record<string, string> = {
  _: "Weather station",
  "-": "House / QTH",
  ">": "Car",
  "<": "Motorcycle",
  "[": "Person / walker",
  j: "Jeep",
  k: "Truck",
  u: "Truck (semi)",
  v: "Van",
  "'": "Small aircraft",
  "^": "Large aircraft",
  g: "Glider",
  s: "Ship / boat",
  Y: "Yacht (sail)",
  b: "Bicycle",
  R: "Recreational vehicle",
  O: "Balloon",
  "#": "Digipeater",
  "&": "I-gate / gateway",
  "=": "Train",
};

/** Decode an APRS 2-char symbol to a human hint (with overlay note), or undefined. */
function decodeAprsSymbol(sym: string | undefined): string | undefined {
  if (!sym || sym.length < 2) return undefined;
  const name = APRS_SYMBOL[sym[1]];
  if (!name) return undefined;
  const table = sym[0];
  const overlay = table !== "/" && table !== "\\" ? ` (overlay ${table})` : "";
  return `${name}${overlay}`;
}

/** APRS weather block (aprs.py `_parse_weather` keys) as labelled fields. */
function aprsWeatherFields(track: TrackRecord): DetailField[] {
  const wx = objAttr(track, "weather");
  if (!wx) return [];
  const num = (k: string): number | undefined => {
    const v = wx[k];
    return typeof v === "number" && Number.isFinite(v) ? v : undefined;
  };
  const out: DetailField[] = [];
  const tf = num("temp_f");
  const tc = num("temp_c");
  if (tf != null || tc != null)
    out.push({
      label: "Temperature",
      value: [tf != null ? `${fmt(tf)}°F` : null, tc != null ? `${fmt(tc, 1)}°C` : null]
        .filter((x): x is string => x !== null)
        .join(" / "),
    });
  const wd = num("wind_dir_deg");
  const ws = num("wind_speed_mph");
  const gust = num("gust_mph");
  if (ws != null || wd != null) {
    let v = ws != null ? `${fmt(ws)} mph` : "calm";
    if (wd != null) v = `${Math.round(wd)}° @ ${v}`;
    if (gust != null && gust > 0) v += `, gust ${fmt(gust)} mph`;
    out.push({ label: "Wind", value: v });
  }
  const h = num("humidity_pct");
  if (h != null) out.push({ label: "Humidity", value: `${fmt(h)}%` });
  const p = num("pressure_hpa");
  if (p != null) out.push({ label: "Pressure", value: `${fmt(p, 1)} hPa` });
  const r = num("rain_since_midnight_in");
  if (r != null) out.push({ label: "Rain since midnight", value: `${fmt(r, 2)} in` });
  return out;
}

/**
 * Short packet-kind badge for an APRS track (null for non-APRS). Weather is keyed
 * off the parsed weather block (aprs.py sets it only for `_` symbol reports), so
 * the badge never claims "weather" for a packet that carries none.
 */
export function aprsPacketKind(track: TrackRecord): PacketKind | null {
  if (track.track_type !== "aprs_station" && track.track_type !== "aprs_object")
    return null;
  if (objAttr(track, "weather")) return { text: "Weather", title: "APRS weather report" };
  if (track.track_type === "aprs_object")
    return { text: "Object", title: "APRS object report" };
  if (strAttr(track, "status")) return { text: "Status", title: "APRS status report" };
  return { text: "Position", title: "APRS position report" };
}

/**
 * Resolve the per-type detail groups for a selected track. Centralized so the
 * details panel renders the same vocabulary the map/list use. Only fields the
 * adapter actually set appear (defensive readers); unknown track types fall back
 * to bare position + motion.
 */
export function trackDetails(track: TrackRecord): DetailGroup[] {
  const groups: DetailGroup[] = [];
  const motion = motionFields(track);
  const pos = positionField(track);
  const withPos = (fields: DetailField[]): DetailField[] =>
    pos ? [...fields, pos] : fields;

  switch (track.track_type) {
    case "aircraft": {
      const ident: DetailField[] = [];
      pushStr(ident, track, "r", "Registration");
      pushStr(ident, track, "t", "Type");
      const cat = strAttr(track, "category");
      if (cat)
        ident.push({
          label: "Category",
          value: AIRCRAFT_CATEGORY[cat] ? `${cat} — ${AIRCRAFT_CATEGORY[cat]}` : cat,
        });
      pushStr(ident, track, "squawk", "Squawk");
      const emg = strAttr(track, "emergency");
      if (emg && emg !== "none") ident.push({ label: "Emergency", value: emg });
      if (track.attributes["on_ground"] === true)
        ident.push({ label: "On ground", value: "yes" });
      if (ident.length) groups.push({ heading: "Aircraft", fields: ident });
      const mf = withPos(motion);
      if (mf.length) groups.push({ heading: "Position & motion", fields: mf });
      break;
    }
    case "aprs_station":
    case "aprs_object": {
      const wx = aprsWeatherFields(track);
      if (wx.length) groups.push({ heading: "Weather", fields: wx });
      const info: DetailField[] = [];
      pushStr(info, track, "status", "Status");
      const comment = strAttr(track, "comment");
      if (comment && comment.trim() && comment.toLowerCase() !== "none")
        info.push({ label: "Comment", value: comment });
      const symRaw = strAttr(track, "aprs_symbol");
      if (symRaw) {
        const hint = decodeAprsSymbol(symRaw);
        info.push({ label: "Symbol", value: hint ? `${hint} (${symRaw})` : symRaw });
      }
      const path = track.attributes["aprs_path"];
      if (Array.isArray(path) && path.length)
        info.push({
          label: "Via",
          value: path.filter((x): x is string => typeof x === "string").join(" › "),
        });
      pushStr(info, track, "aprs_dest", "To");
      pushStr(info, track, "reported_by", "Reported by");
      if (info.length) groups.push({ heading: "Packet", fields: info });
      const mf = withPos(motion);
      if (mf.length) groups.push({ heading: "Position & motion", fields: mf });
      break;
    }
    case "radiosonde": {
      const ident: DetailField[] = [];
      pushStr(ident, track, "serial", "Serial");
      const st = strAttr(track, "sonde_type");
      const sub = strAttr(track, "subtype");
      if (st || sub)
        ident.push({
          label: "Sonde type",
          value: [st, sub].filter((x): x is string => !!x).join(" / "),
        });
      pushStr(ident, track, "manufacturer", "Manufacturer");
      pushStr(ident, track, "ascent_state", "Phase");
      pushNum(ident, track, "frequency_mhz", "Frequency", "MHz", 3);
      pushStr(ident, track, "uploader_callsign", "Heard by");
      if (ident.length) groups.push({ heading: "Radiosonde", fields: ident });
      const atmos: DetailField[] = [];
      pushNum(atmos, track, "temp_c", "Temperature", "°C", 1);
      pushNum(atmos, track, "humidity_pct", "Humidity", "%", 0);
      pushNum(atmos, track, "pressure_hpa", "Pressure", "hPa", 1);
      pushNum(atmos, track, "sats", "GPS sats", "", 0);
      pushNum(atmos, track, "batt_v", "Battery", "V", 1);
      if (atmos.length) groups.push({ heading: "Atmospherics", fields: atmos });
      const mf = withPos(motion);
      if (mf.length) groups.push({ heading: "Position & motion", fields: mf });
      break;
    }
    case "orbital_object": {
      const ident: DetailField[] = [];
      pushId(ident, track, "norad_id", "NORAD");
      pushStr(ident, track, "object_id", "Int'l ID");
      pushStr(ident, track, "group", "Catalog group");
      if (ident.length) groups.push({ heading: "Satellite", fields: ident });
      const look: DetailField[] = [];
      pushNum(look, track, "elevation_deg", "Elevation", "°", 1);
      pushNum(look, track, "azimuth_deg", "Azimuth", "°", 1);
      const sr = numAttr(track, "slant_range_m");
      if (sr != null) look.push({ label: "Slant range", value: `${fmt(sr / 1000)} km` });
      if (track.altitude_m != null)
        look.push({ label: "Altitude", value: `${fmt(track.altitude_m / 1000)} km` });
      if (look.length) groups.push({ heading: "Look angles (predicted)", fields: look });
      const age = numAttr(track, "element_age_s");
      if (age != null)
        groups.push({
          heading: "Elements",
          fields: [
            {
              label: "Element age",
              value: humanAge(age),
              title: "Age of the orbital elements this SGP4 prediction was propagated from",
            },
          ],
        });
      const pass: DetailField[] = [];
      const nowMs = Date.now();
      const passTimeField = (key: string, label: string): void => {
        const iso = strAttr(track, key);
        if (!iso) return; // absent → omit (defensive; no fake values)
        const ms = Date.parse(iso);
        if (Number.isNaN(ms)) return;
        pass.push({
          label,
          value: humanRelative((ms - nowMs) / 1000),
          title: new Date(ms).toLocaleTimeString(),
        });
      };
      passTimeField("pass_rise_at", "Rise");
      const culmIso = strAttr(track, "pass_culmination_at");
      if (culmIso) {
        const ms = Date.parse(culmIso);
        if (!Number.isNaN(ms)) {
          const maxEl = numAttr(track, "pass_max_elevation_deg");
          const rel = humanRelative((ms - nowMs) / 1000);
          pass.push({
            label: "Culmination",
            value: maxEl != null ? `${rel} (max ${fmt(maxEl, 0)}°)` : rel,
            title: new Date(ms).toLocaleTimeString(),
          });
        }
      }
      passTimeField("pass_set_at", "Set");
      if (pass.length) groups.push({ heading: "Pass (predicted)", fields: pass });
      break;
    }
    case "vessel": {
      const ident: DetailField[] = [];
      pushStr(ident, track, "vessel_name", "Name");
      pushId(ident, track, "mmsi", "MMSI");
      pushStr(ident, track, "ship_type_text", "Vessel type");
      pushStr(ident, track, "nav_status_text", "Nav status");
      pushStr(ident, track, "destination", "Destination");
      if (ident.length) groups.push({ heading: "Vessel", fields: ident });
      const mf = withPos(motion);
      if (mf.length) groups.push({ heading: "Position & motion", fields: mf });
      break;
    }
    default: {
      const mf = withPos(motion);
      if (mf.length) groups.push({ heading: "Position & motion", fields: mf });
    }
  }
  return groups;
}

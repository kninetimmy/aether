// TypeScript mirror of the schema v2 record union (PRD §14) and the /ws/v2 wire
// protocol (PRD §22). These shapes track src/aether/schema/records.py and
// src/aether/backend/protocol.py — keep them in sync when the schema bumps.

export const SCHEMA_VERSION = 2 as const;

// --- GeoJSON (only what the frontend reads) -------------------------------

export interface GeoJSONPoint {
  type: "Point";
  coordinates: [number, number] | [number, number, number]; // [lon, lat, (alt)]
}

export interface GeoJSONGeometryGeneric {
  type:
    | "Point"
    | "MultiPoint"
    | "LineString"
    | "MultiLineString"
    | "Polygon"
    | "MultiPolygon"
    | "GeometryCollection";
  coordinates?: unknown;
  geometries?: GeoJSONGeometryGeneric[];
}

export type GeoJSONGeometry = GeoJSONGeometryGeneric;

// --- Shared record base ----------------------------------------------------

export interface Provenance {
  source: string;
  observed_at: string;
  received_at?: string | null;
  locally_received?: boolean;
  signal_dbm?: number | null;
  [key: string]: unknown;
}

export interface RecordBase {
  schema_version: 2;
  kind: RecordKind;
  id: string;
  source: string;
  observed_at: string;
  received_at: string;
  published_at: string;
  correlation_key?: string | null;
  provenance: Provenance[];
  tags: string[];
  attributes: Record<string, unknown>;
}

export type RecordKind =
  | "track"
  | "feature"
  | "event"
  | "alert"
  | "source_status";

// --- Track -----------------------------------------------------------------

export type TrackType =
  | "aircraft"
  | "vessel"
  | "aprs_station"
  | "aprs_object"
  | "radiosonde"
  | "orbital_object"
  | "other";

export interface Classification {
  military: boolean | null;
  basis: "provider" | "address_block" | "both" | "unknown";
  confidence: "high" | "medium" | "low" | "unknown";
  note: string | null;
}

export interface TrackRecord extends RecordBase {
  kind: "track";
  track_type: TrackType;
  label: string | null;
  geometry: GeoJSONPoint | null;
  altitude_m: number | null;
  speed_mps: number | null;
  heading_deg: number | null;
  vertical_rate_mps: number | null;
  locally_received: boolean;
  classification: Classification | null;
  valid_until: string | null;
  predicted: boolean;
}

// --- Fusion metadata (M3.1) ------------------------------------------------
// The backend fuses same-identity local-RF + network observations into one
// track and stashes the diagnostics under attributes.fusion (PRD §11.4, §15) —
// no schema bump. These shapes mirror src/aether/fusion/engine.py's fusion block;
// read them with `fusionMeta(track)`, which returns undefined when absent.

export type FreshnessClass = "live" | "stale" | "expired";

export interface FusionContributor {
  source: string;
  local_rf: boolean;
  observed_at: string;
  freshness: FreshnessClass;
}

export interface FusionMeta {
  /** Source supplying the headline geometry — PRD §8.1's "who am I seeing this from". */
  active_source: string;
  contributors: FusionContributor[];
  /** Winning source per dynamic field (or null when no contributor carries it). */
  field_sources: Record<string, string | null>;
  field_freshness: Record<string, FreshnessClass | null>;
  /** When the operator's own antenna last heard this — survives local expiry (PRD §8.1). */
  last_local_rf_at: string | null;
  /** Number of current contributing sources. */
  fused_count: number;
}

/** Read the fusion block off a track; undefined when absent or malformed. */
export function fusionMeta(track: TrackRecord): FusionMeta | undefined {
  const raw = track.attributes?.["fusion"];
  if (raw === null || typeof raw !== "object" || Array.isArray(raw)) return undefined;
  // A present-but-malformed block (no/wrong-typed `contributors`) must read as
  // absent, not throw downstream (consumers call .some/.map on contributors).
  if (!Array.isArray((raw as { contributors?: unknown }).contributors)) return undefined;
  return raw as FusionMeta;
}

// --- Defensive attribute readers (M3.6a) -----------------------------------
// Adapters normalize at the edge (PRD §13.2), but the display filters must never
// assume a key is present: an ACTIVE criterion treats a missing/wrong-typed
// attribute as "unknown" (no-match) rather than throwing (PRD §37 isolation).
// AIS keys mirror src/aether/adapters/ais.py: mmsi, vessel_name, destination,
// ship_type (int code) / ship_type_text, nav_status (int code) / nav_status_text.

/** Read a string attribute defensively; undefined when absent or not a string. */
export function aisAttr(track: TrackRecord, key: string): string | undefined {
  const raw = track.attributes?.[key];
  return typeof raw === "string" ? raw : undefined;
}

/**
 * Read an integer-coded attribute defensively (AIS ship_type / nav_status are
 * raw ITU int codes — the filter keys off the stable code, not the *_text label).
 * Undefined when absent or not an integer.
 */
export function aisIntAttr(track: TrackRecord, key: string): number | undefined {
  const raw = track.attributes?.[key];
  return typeof raw === "number" && Number.isInteger(raw) ? raw : undefined;
}

/**
 * Age of a track in seconds = now − observed_at. undefined when observed_at is
 * missing/unparseable so the caller can treat it as "unknown leg" rather than 0.
 */
export function trackAgeS(track: TrackRecord, now: number): number | undefined {
  const t = Date.parse(track.observed_at);
  if (Number.isNaN(t)) return undefined;
  return Math.max(0, (now - t) / 1000);
}

// --- GeoFeature ------------------------------------------------------------

export type FeatureType =
  | "lightning_flash"
  | "lightning_cluster"
  | "fire_detection"
  | "earthquake"
  | "tfr"
  | "notam_geometry"
  | "predicted_landing"
  | "geofence"
  | "other";

export interface GeoFeatureRecord extends RecordBase {
  kind: "feature";
  feature_type: FeatureType;
  geometry: GeoJSONGeometry;
  valid_from: string | null;
  valid_until: string | null;
  severity: string | null;
  label: string | null;
}

// --- Event -----------------------------------------------------------------

export interface EventRecord extends RecordBase {
  kind: "event";
  event_type: string;
  subject_id: string | null;
  summary: string;
  message: string | null;
  geometry: GeoJSONGeometry | null;
  severity: string | null;
}

// --- Alert -----------------------------------------------------------------

export type AlertState =
  | "open"
  | "acknowledged"
  | "resolved"
  | "suppressed"
  | "delivery_failed";

export type Severity = "info" | "low" | "medium" | "high" | "critical";

export interface AlertRecord extends RecordBase {
  kind: "alert";
  rule_id: string;
  subject_id: string | null;
  state: AlertState;
  severity: Severity;
  title: string;
  summary: string;
  triggered_at: string;
  acknowledged_at: string | null;
  resolved_at: string | null;
  delivery_status: Record<string, string>;
}

// --- SourceStatus ----------------------------------------------------------

export type SourceState =
  | "starting"
  | "connected"
  | "degraded"
  | "stale"
  | "offline"
  | "disabled";

export interface SourceStatusRecord extends RecordBase {
  kind: "source_status";
  status: SourceState;
  last_success_at: string | null;
  last_record_at: string | null;
  lag_s: number | null;
  records_received: number;
  records_rejected: number;
  error_code: string | null;
  error_summary: string | null;
}

export type AnyRecord =
  | TrackRecord
  | GeoFeatureRecord
  | EventRecord
  | AlertRecord
  | SourceStatusRecord;

// --- Wire frames (PRD §22) -------------------------------------------------

export interface SnapshotFrame {
  type: "snapshot";
  seq: number;
  tracks: TrackRecord[];
  features: GeoFeatureRecord[];
  events: EventRecord[];
  alerts: AlertRecord[];
  source_status: SourceStatusRecord[];
}

export interface TrackUpsertFrame {
  type: "track_upsert";
  seq: number;
  record: TrackRecord;
}
export interface FeatureUpsertFrame {
  type: "feature_upsert";
  seq: number;
  record: GeoFeatureRecord;
}
export interface AlertUpsertFrame {
  type: "alert_upsert";
  seq: number;
  record: AlertRecord;
}
export interface SourceStatusFrame {
  type: "source_status";
  seq: number;
  record: SourceStatusRecord;
}
export interface EventFrame {
  type: "event";
  seq: number;
  record: EventRecord;
}
export interface RemoveFrame {
  type: "remove";
  seq: number;
  kind: RecordKind;
  id: string;
}

export type DeltaFrame =
  | TrackUpsertFrame
  | FeatureUpsertFrame
  | AlertUpsertFrame
  | SourceStatusFrame
  | EventFrame
  | RemoveFrame;

export type ServerFrame = SnapshotFrame | DeltaFrame;

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

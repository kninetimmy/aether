// Build GeoJSON sources from live state for the map (PRD §24.3: GeoJSON sources +
// WebGL layers, never one DOM marker per object). Tracks become point features;
// features become whatever geometry they carry. The presentation registry
// supplies color/symbol; the map component owns the MapLibre layer definitions.

import {
  featurePresentation,
  trackPresentation,
} from "../presentationRegistry";
import { fusionMeta } from "../../types/records";
import type {
  GeoFeatureRecord,
  GeoJSONGeometry,
  GeoJSONPoint,
  TrackRecord,
} from "../../types/records";

export interface MapFeatureProps {
  id: string;
  kind: "track" | "feature";
  layer: string;
  label: string;
  color: string;
  symbol: string;
  rotateByHeading: boolean;
  heading: number;
  locallyReceived: boolean;
  predicted: boolean;
  subtype: string;
  /** Fusion headline source (PRD §8.1); empty when the track isn't fused. */
  activeSource: string;
  /** Number of contributing sources (1 when not fused). */
  fusedCount: number;
}

export interface MapFeature {
  type: "Feature";
  geometry: GeoJSONGeometry | GeoJSONPoint;
  properties: MapFeatureProps;
}

export interface FeatureCollection {
  type: "FeatureCollection";
  features: MapFeature[];
}

/** Point features for tracks that currently have a position.
 *
 * Accepts any iterable of tracks, so a caller can pass a provenance-filtered list
 * (see `visibleTracks`) — a hidden track simply isn't in the iterable, so it
 * leaves the GeoJSON source entirely (PRD §16.5). Display only; never changes
 * ingestion.
 */
export function trackFeatureCollection(
  tracks: Iterable<TrackRecord>,
): FeatureCollection {
  const features: MapFeature[] = [];
  for (const track of tracks) {
    if (!track.geometry) continue;
    const p = trackPresentation(track);
    const meta = fusionMeta(track);
    features.push({
      type: "Feature",
      geometry: track.geometry,
      properties: {
        id: track.id,
        kind: "track",
        layer: p.layer,
        label: track.label ?? track.id,
        color: p.color,
        symbol: p.symbol,
        rotateByHeading: p.rotateByHeading,
        heading: track.heading_deg ?? 0,
        locallyReceived: track.locally_received,
        predicted: track.predicted,
        subtype: track.track_type,
        activeSource: meta?.active_source ?? "",
        fusedCount: meta?.fused_count ?? 1,
      },
    });
  }
  return { type: "FeatureCollection", features };
}

/** Features for all geo-features (TFRs, fires, geofences, …). */
export function featureFeatureCollection(
  geoFeatures: Map<string, GeoFeatureRecord>,
): FeatureCollection {
  const features: MapFeature[] = [];
  for (const feat of geoFeatures.values()) {
    const p = featurePresentation(feat);
    features.push({
      type: "Feature",
      geometry: feat.geometry,
      properties: {
        id: feat.id,
        kind: "feature",
        layer: p.layer,
        label: feat.label ?? feat.id,
        color: p.color,
        symbol: p.symbol,
        rotateByHeading: false,
        heading: 0,
        locallyReceived: false,
        predicted: false,
        subtype: feat.feature_type,
        activeSource: "",
        fusedCount: 1,
      },
    });
  }
  return { type: "FeatureCollection", features };
}

/** Distinct presentation layer ids present in current state, for layer control. */
export function activeLayers(
  tracks: Map<string, TrackRecord>,
  geoFeatures: Map<string, GeoFeatureRecord>,
): string[] {
  const layers = new Set<string>();
  for (const t of tracks.values()) layers.add(trackPresentation(t).layer);
  for (const f of geoFeatures.values()) layers.add(featurePresentation(f).layer);
  return [...layers].sort();
}

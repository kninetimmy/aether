import { describe, expect, it } from "vitest";
import {
  featureFeatureCollection,
  isLightningFeature,
  lightningFeatureCollection,
  trackFeatureCollection,
} from "./recordLayers";
import type {
  FeatureType,
  GeoFeatureRecord,
  GeoJSONGeometry,
  TrackRecord,
} from "../../types/records";

const NOW = "2026-06-15T00:00:00Z";

function track(
  id: string,
  over: Partial<TrackRecord> = {},
): TrackRecord {
  return {
    schema_version: 2,
    kind: "track",
    id,
    source: "demo",
    observed_at: NOW,
    received_at: NOW,
    published_at: NOW,
    correlation_key: id,
    provenance: [],
    tags: [],
    attributes: {},
    track_type: "aircraft",
    label: id,
    geometry: { type: "Point", coordinates: [-95, 40] },
    altitude_m: null,
    speed_mps: null,
    heading_deg: null,
    vertical_rate_mps: null,
    locally_received: true,
    classification: null,
    valid_until: null,
    predicted: false,
    ...over,
  };
}

describe("trackFeatureCollection isToi", () => {
  const onList = track("aircraft:icao:aaa", { correlation_key: "aircraft:icao:aaa" });
  const offList = track("aircraft:icao:bbb", { correlation_key: "aircraft:icao:bbb" });

  it("sets isToi=true only for watchlisted members (by stable key)", () => {
    const fc = trackFeatureCollection(
      [onList, offList],
      new Set(["aircraft:icao:aaa"]),
    );
    const props = Object.fromEntries(
      fc.features.map((f) => [f.properties.id, f.properties.isToi]),
    );
    expect(props["aircraft:icao:aaa"]).toBe(true);
    expect(props["aircraft:icao:bbb"]).toBe(false);
  });

  it("defaults isToi=false with no watchlist argument", () => {
    const fc = trackFeatureCollection([onList]);
    expect(fc.features[0]?.properties.isToi).toBe(false);
  });

  it("a TOI hidden by an upstream filter (absent from the iterable) yields no feature, so it has no highlight", () => {
    // The highlight ring is built from the SAME already-filtered collection by
    // filtering on isToi. If a watchlisted track was filtered out upstream
    // (visibleTracks), it is simply not in the iterable passed here — so there is
    // no feature at all and therefore nothing for the highlight layer to draw.
    const filteredIterable = [offList]; // onList was hidden by a layer/provenance filter
    const fc = trackFeatureCollection(filteredIterable, new Set(["aircraft:icao:aaa"]));
    const ids = fc.features.map((f) => f.properties.id);
    expect(ids).not.toContain("aircraft:icao:aaa");
    // And none of the rendered features are flagged isToi (the only TOI is hidden).
    expect(fc.features.some((f) => f.properties.isToi)).toBe(false);
  });

  it("skips tracks without geometry", () => {
    const noGeo = track("aircraft:icao:ccc", {
      correlation_key: "aircraft:icao:ccc",
      geometry: null,
    });
    const fc = trackFeatureCollection([noGeo], new Set(["aircraft:icao:ccc"]));
    expect(fc.features).toHaveLength(0);
  });
});

function feature(
  id: string,
  featureType: FeatureType,
  geometry: GeoJSONGeometry = { type: "Point", coordinates: [-95, 40] },
): GeoFeatureRecord {
  return {
    schema_version: 2,
    kind: "feature",
    id,
    source: "demo",
    observed_at: NOW,
    received_at: NOW,
    published_at: NOW,
    provenance: [],
    tags: [],
    attributes: {},
    feature_type: featureType,
    geometry,
    valid_from: null,
    valid_until: null,
    severity: null,
    label: id,
  };
}

function featureMap(...feats: GeoFeatureRecord[]): Map<string, GeoFeatureRecord> {
  return new Map(feats.map((f) => [f.id, f]));
}

describe("lightning clustering split (LIGHTNING-FR-006)", () => {
  const flash = feature("glm:flash:1", "lightning_flash");
  const fire = feature("firms:fire:1", "fire_detection");
  const tfr = feature("faa:tfr:1", "tfr", { type: "Polygon", coordinates: [] });

  it("routes lightning points to the lightning collection, not the generic one", () => {
    const feats = featureMap(flash, fire, tfr);
    const generic = featureFeatureCollection(feats).features.map((f) => f.properties.id);
    const lightning = lightningFeatureCollection(feats).features.map((f) => f.properties.id);

    expect(lightning).toEqual(["glm:flash:1"]);
    // No double-draw: the flash is absent from the generic source...
    expect(generic).not.toContain("glm:flash:1");
    // ...while non-lightning features stay on the generic path.
    expect(generic).toEqual(expect.arrayContaining(["firms:fire:1", "faa:tfr:1"]));
    expect(lightning).not.toContain("firms:fire:1");
  });

  it("keeps non-point lightning on the generic path (clustering needs points)", () => {
    // A future lightning_cluster could arrive as a polygon; MapLibre clustering
    // requires points, so it must NOT enter the clustered source.
    const polyCluster = feature("glm:cluster:poly", "lightning_cluster", {
      type: "Polygon",
      coordinates: [],
    });
    expect(isLightningFeature(polyCluster)).toBe(false);

    const feats = featureMap(polyCluster);
    expect(lightningFeatureCollection(feats).features).toHaveLength(0);
    expect(featureFeatureCollection(feats).features.map((f) => f.properties.id)).toEqual([
      "glm:cluster:poly",
    ]);
  });

  it("treats a point lightning_cluster as clustered lightning", () => {
    expect(isLightningFeature(feature("glm:cluster:pt", "lightning_cluster"))).toBe(true);
    expect(isLightningFeature(flash)).toBe(true);
    expect(isLightningFeature(fire)).toBe(false);
  });
});

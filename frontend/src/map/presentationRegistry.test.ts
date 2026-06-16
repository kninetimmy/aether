import { describe, expect, it } from "vitest";
import {
  featurePresentation,
  presentationFor,
  severityColor,
  sourceStateColor,
  trackPresentation,
} from "./presentationRegistry";
import type {
  FeatureType,
  GeoFeatureRecord,
  TrackRecord,
} from "../types/records";

const NOW = "2026-06-15T00:00:00Z";

function baseTrack(over: Partial<TrackRecord>): TrackRecord {
  return {
    schema_version: 2,
    kind: "track",
    id: "t",
    source: "demo",
    observed_at: NOW,
    received_at: NOW,
    published_at: NOW,
    provenance: [],
    tags: [],
    attributes: {},
    track_type: "aircraft",
    label: null,
    geometry: null,
    altitude_m: null,
    speed_mps: null,
    heading_deg: null,
    vertical_rate_mps: null,
    locally_received: false,
    classification: null,
    valid_until: null,
    predicted: false,
    ...over,
  };
}

describe("trackPresentation", () => {
  it("maps known track types to distinct layers", () => {
    expect(trackPresentation(baseTrack({ track_type: "aircraft" })).layer).toBe(
      "tracks-aircraft",
    );
    expect(trackPresentation(baseTrack({ track_type: "vessel" })).layer).toBe(
      "tracks-vessel",
    );
    expect(trackPresentation(baseTrack({ track_type: "aircraft" })).rotateByHeading).toBe(
      true,
    );
  });

  it("falls back to a generic style for unknown types", () => {
    // Force an out-of-union value to simulate a new/unknown source type.
    const unknown = baseTrack({ track_type: "quantum_blip" as never });
    const p = trackPresentation(unknown);
    expect(p.layer).toBe("tracks-other");
    expect(p.symbol).toBe("dot");
  });
});

function baseFeature(featureType: FeatureType): GeoFeatureRecord {
  return {
    schema_version: 2,
    kind: "feature",
    id: "f",
    source: "demo",
    observed_at: NOW,
    received_at: NOW,
    published_at: NOW,
    provenance: [],
    tags: [],
    attributes: {},
    feature_type: featureType,
    geometry: { type: "Polygon", coordinates: [] },
    valid_from: null,
    valid_until: null,
    severity: null,
    label: null,
  };
}

describe("featurePresentation", () => {
  it("maps tfr to its own layer", () => {
    expect(featurePresentation(baseFeature("tfr")).layer).toBe("features-tfr");
  });

  it("falls back for unknown feature types", () => {
    expect(featurePresentation(baseFeature("ufo" as never)).layer).toBe(
      "features-other",
    );
  });
});

describe("presentationFor", () => {
  it("returns null for non-spatial kinds", () => {
    expect(presentationFor({ kind: "alert" } as never)).toBeNull();
    expect(presentationFor({ kind: "source_status" } as never)).toBeNull();
  });
});

describe("status & severity ramps", () => {
  it("gives each source state a color", () => {
    expect(sourceStateColor("offline")).toBeTruthy();
    expect(sourceStateColor("connected")).not.toBe(sourceStateColor("offline"));
  });
  it("gives each severity a color", () => {
    expect(severityColor("critical")).not.toBe(severityColor("info"));
  });
});

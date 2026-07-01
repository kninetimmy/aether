import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  aprsPacketKind,
  featurePresentation,
  lightningStyle,
  militaryBadge,
  presentationFor,
  severityColor,
  sourceStateColor,
  trackDetails,
  trackPresentation,
} from "./presentationRegistry";
import type {
  Classification,
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

describe("militaryBadge", () => {
  const cls = (over: Partial<Classification>): Classification => ({
    military: true,
    basis: "provider",
    confidence: "medium",
    note: null,
    ...over,
  });

  it("is null when there is no military classification", () => {
    expect(militaryBadge(null)).toBeNull();
    expect(militaryBadge(undefined)).toBeNull();
    expect(militaryBadge(cls({ military: null }))).toBeNull();
    expect(militaryBadge(cls({ military: false }))).toBeNull();
  });

  it("renders a hedged badge naming the basis, never claiming certainty", () => {
    const badge = militaryBadge(cls({ basis: "address_block", confidence: "low" }));
    expect(badge).not.toBeNull();
    expect(badge!.text).toBe("MIL?"); // hedged, not "MIL"/"CONFIRMED"
    expect(badge!.title).toContain("address-block");
    expect(badge!.title).toContain("low");
    expect(badge!.title.toLowerCase()).toContain("not authoritative");
  });
});

describe("lightningStyle (LIGHTNING-FR-006)", () => {
  it("ramps cluster color and radius UP with flash count (density is multi-channel)", () => {
    const ls = lightningStyle();
    // Both ramps are ascending in their stop thresholds, so a denser cluster is
    // always drawn bigger AND hotter — count is never the only channel.
    const radii = ls.clusterRadius.steps;
    const colors = ls.clusterColor.steps;
    expect(radii.length).toBeGreaterThan(0);
    expect(colors.length).toBe(radii.length);
    for (let i = 1; i < radii.length; i++) {
      expect(radii[i]![0]).toBeGreaterThan(radii[i - 1]![0]); // ascending count stops
      expect(radii[i]![1]).toBeGreaterThan(radii[i - 1]![1]); // ascending radius
    }
    expect(radii[0]![1]).toBeGreaterThan(ls.clusterRadius.base); // first step grows the base
  });

  it("gives unclustered flashes a smaller dot than the smallest cluster bubble", () => {
    const ls = lightningStyle();
    expect(ls.flashRadius).toBeLessThan(ls.clusterRadius.base);
    expect(ls.flashColor).toBeTruthy();
    expect(ls.countColor).toBeTruthy();
  });
});

/** Flatten a track's detail groups into one {label: value} map for assertions. */
function fieldMap(track: TrackRecord): Record<string, string> {
  const m: Record<string, string> = {};
  for (const g of trackDetails(track)) for (const f of g.fields) m[f.label] = f.value;
  return m;
}

describe("aprsPacketKind", () => {
  it("classifies a weather packet off the parsed weather block", () => {
    const k = aprsPacketKind(
      baseTrack({ track_type: "aprs_station", attributes: { weather: { temp_f: 70 } } }),
    );
    expect(k?.text).toBe("Weather");
  });

  it("classifies status, plain position, and objects distinctly", () => {
    expect(
      aprsPacketKind(
        baseTrack({ track_type: "aprs_station", attributes: { status: "QTH" } }),
      )?.text,
    ).toBe("Status");
    expect(aprsPacketKind(baseTrack({ track_type: "aprs_station" }))?.text).toBe(
      "Position",
    );
    expect(aprsPacketKind(baseTrack({ track_type: "aprs_object" }))?.text).toBe("Object");
  });

  it("is null for non-APRS tracks", () => {
    expect(aprsPacketKind(baseTrack({ track_type: "aircraft" }))).toBeNull();
  });
});

describe("trackDetails", () => {
  it("renders APRS weather contents under a Weather group", () => {
    const t = baseTrack({
      track_type: "aprs_station",
      attributes: {
        weather: {
          temp_f: 67,
          temp_c: 19.44,
          wind_dir_deg: 90,
          wind_speed_mph: 5,
          gust_mph: 9,
          humidity_pct: 90,
          pressure_hpa: 1014.9,
        },
      },
    });
    expect(trackDetails(t).some((g) => g.heading === "Weather")).toBe(true);
    const f = fieldMap(t);
    expect(f["Temperature"]).toContain("67°F");
    expect(f["Wind"]).toContain("mph");
    expect(f["Humidity"]).toBe("90%");
  });

  it("decodes the aircraft size class from the emitter category", () => {
    const f = fieldMap(
      baseTrack({
        track_type: "aircraft",
        attributes: { category: "A5", r: "N473MC", t: "B744" },
      }),
    );
    expect(f["Registration"]).toBe("N473MC");
    expect(f["Type"]).toBe("B744");
    expect(f["Category"]).toContain("Heavy");
  });

  it("shows orbital look-angles + element age and never invents absent fields", () => {
    const t = baseTrack({
      track_type: "orbital_object",
      altitude_m: 500000,
      attributes: {
        norad_id: 25544,
        elevation_deg: 43.5,
        azimuth_deg: 117.6,
        slant_range_m: 3514544,
        element_age_s: 46216,
      },
    });
    const f = fieldMap(t);
    expect(f["NORAD"]).toBe("25544");
    expect(f["Elevation"]).toContain("°");
    expect(f["Element age"]).toBeTruthy();
    expect(f["Int'l ID"]).toBeUndefined(); // object_id absent → omitted, not invented
  });

  it("omits motion/position fields that are absent (defensive)", () => {
    const keys = Object.keys(fieldMap(baseTrack({ track_type: "aircraft" })));
    expect(keys).not.toContain("Altitude");
    expect(keys).not.toContain("Position");
  });

  describe("orbital pass prediction (PRD §32 #18/#19)", () => {
    const FROZEN_NOW = new Date("2026-06-15T12:00:00Z");

    beforeEach(() => {
      vi.useFakeTimers();
      vi.setSystemTime(FROZEN_NOW);
    });

    afterEach(() => {
      vi.useRealTimers();
    });

    it("renders rise (past)/culmination/set (future) as signed-relative strings", () => {
      const riseAt = new Date(FROZEN_NOW.getTime() - 5 * 60_000); // 5 min ago
      const culminationAt = new Date(FROZEN_NOW.getTime() + 2 * 60_000); // in 2 min
      const setAt = new Date(FROZEN_NOW.getTime() + 9 * 60_000); // in 9 min
      const t = baseTrack({
        track_type: "orbital_object",
        attributes: {
          norad_id: 25544,
          elevation_deg: 12.0,
          pass_rise_at: riseAt.toISOString(),
          pass_culmination_at: culminationAt.toISOString(),
          pass_max_elevation_deg: 67.3,
          pass_set_at: setAt.toISOString(),
        },
      });
      const groups = trackDetails(t);
      const passGroup = groups.find((g) => g.heading === "Pass (predicted)");
      expect(passGroup).toBeTruthy();
      const f: Record<string, string> = {};
      for (const field of passGroup!.fields) f[field.label] = field.value;
      expect(f["Rise"]).toContain("ago"); // in the past
      expect(f["Culmination"]).toMatch(/^in /); // in the future
      expect(f["Culmination"]).toContain("max");
      expect(f["Culmination"]).toContain("°");
      expect(f["Set"]).toMatch(/^in /); // in the future
    });

    it("renders no Pass group when no pass_* attributes are present (GEO/no-pass)", () => {
      const t = baseTrack({
        track_type: "orbital_object",
        attributes: { norad_id: 99002, elevation_deg: 4.0 },
      });
      const groups = trackDetails(t);
      expect(groups.some((g) => g.heading === "Pass (predicted)")).toBe(false);
      // Existing groups are unaffected by the absence of pass data.
      expect(groups.some((g) => g.heading === "Satellite")).toBe(true);
    });
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

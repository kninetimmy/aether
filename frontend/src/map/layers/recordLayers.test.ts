import { describe, expect, it } from "vitest";
import { trackFeatureCollection } from "./recordLayers";
import type { TrackRecord } from "../../types/records";

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

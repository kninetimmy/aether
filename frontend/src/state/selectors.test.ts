import { describe, expect, it } from "vitest";
import { trackMatchesProvenance, visibleTracks } from "./selectors";
import { fusionMeta, type TrackRecord } from "../types/records";

const NOW = "2026-06-15T00:00:00Z";

function track(
  id: string,
  locally_received: boolean,
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
    locally_received,
    classification: null,
    valid_until: null,
    predicted: false,
    ...over,
  };
}

describe("trackMatchesProvenance", () => {
  const local = track("local", true);
  const net = track("net", false);

  it("all matches everything", () => {
    expect(trackMatchesProvenance(local, "all")).toBe(true);
    expect(trackMatchesProvenance(net, "all")).toBe(true);
  });

  it("local matches only locally received", () => {
    expect(trackMatchesProvenance(local, "local")).toBe(true);
    expect(trackMatchesProvenance(net, "local")).toBe(false);
  });

  it("network matches only non-local", () => {
    expect(trackMatchesProvenance(local, "network")).toBe(false);
    expect(trackMatchesProvenance(net, "network")).toBe(true);
  });
});

describe("visibleTracks", () => {
  const tracks = new Map<string, TrackRecord>([
    ["a", track("a", true)],
    ["b", track("b", false)],
    ["c", track("c", true)],
  ]);

  it("returns the correct subset per mode", () => {
    expect(visibleTracks(tracks, "all").map((t) => t.id).sort()).toEqual(["a", "b", "c"]);
    expect(visibleTracks(tracks, "local").map((t) => t.id).sort()).toEqual(["a", "c"]);
    expect(visibleTracks(tracks, "network").map((t) => t.id)).toEqual(["b"]);
  });

  it("empty map gives empty list", () => {
    expect(visibleTracks(new Map(), "all")).toEqual([]);
  });

  it("never mutates the input map", () => {
    const before = tracks.size;
    visibleTracks(tracks, "local");
    expect(tracks.size).toBe(before);
  });
});

describe("fusionMeta", () => {
  it("returns the typed block when present", () => {
    const fused = track("fused", true, {
      attributes: {
        fusion: {
          active_source: "local_adsb",
          contributors: [
            { source: "local_adsb", local_rf: true, observed_at: NOW, freshness: "live" },
            { source: "demo-net", local_rf: false, observed_at: NOW, freshness: "live" },
          ],
          field_sources: { geometry: "local_adsb", speed_mps: "demo-net" },
          field_freshness: { geometry: "live", speed_mps: "live" },
          last_local_rf_at: NOW,
          fused_count: 2,
        },
      },
    });
    const meta = fusionMeta(fused);
    expect(meta?.fused_count).toBe(2);
    expect(meta?.active_source).toBe("local_adsb");
    expect(meta?.contributors).toHaveLength(2);
  });

  it("returns undefined when absent", () => {
    expect(fusionMeta(track("plain", true))).toBeUndefined();
  });

  it("returns undefined when malformed (non-object)", () => {
    expect(fusionMeta(track("bad", true, { attributes: { fusion: "nope" } }))).toBeUndefined();
    expect(fusionMeta(track("arr", true, { attributes: { fusion: [1, 2] } }))).toBeUndefined();
  });
});

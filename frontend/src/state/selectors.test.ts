import { describe, expect, it } from "vitest";
import {
  haversineM,
  matchesAisDestination,
  matchesAisMmsi,
  matchesAisName,
  matchesAisNavStatus,
  matchesAisVesselType,
  matchesAprsCallsign,
  matchesLiveLocal,
  matchesMilitary,
  matchesMilitaryBasis,
  matchesProvenance,
  matchesSource,
  matchesTrackType,
  trackMatchesProvenance,
  visibleTracks,
  withinAge,
  withinAltitude,
  withinRange,
  withinSpeed,
  type FilterCtx,
} from "./selectors";
import { defaultFilters, type DisplayFilters } from "./store";
import { fusionMeta, type TrackRecord } from "../types/records";

const NOW = "2026-06-15T00:00:00Z";
const NOW_MS = Date.parse(NOW);

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

/** A DisplayFilters with one field overridden, otherwise the no-op default. */
function f(over: Partial<DisplayFilters> = {}): DisplayFilters {
  return { ...defaultFilters(), ...over };
}

function ctx(over: Partial<FilterCtx> = {}): FilterCtx {
  return { now: NOW_MS, stationCenter: null, watchlist: new Set(), ...over };
}

describe("trackMatchesProvenance (existing behavior preserved)", () => {
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

describe("matchesProvenance predicate", () => {
  it("reads filters.provenance", () => {
    expect(matchesProvenance(track("a", true), f({ provenance: "local" }))).toBe(true);
    expect(matchesProvenance(track("a", false), f({ provenance: "local" }))).toBe(false);
    expect(matchesProvenance(track("a", false), f({ provenance: "all" }))).toBe(true);
  });
});

describe("matchesLiveLocal (T27)", () => {
  it("inactive returns true regardless", () => {
    expect(matchesLiveLocal(track("a", false), f())).toBe(true);
  });

  it("true for locally_received with a live local contributor", () => {
    const t = track("live", true, {
      attributes: {
        fusion: {
          active_source: "local_adsb",
          contributors: [
            { source: "local_adsb", local_rf: true, observed_at: NOW, freshness: "live" },
          ],
          field_sources: {},
          field_freshness: {},
          last_local_rf_at: NOW,
          fused_count: 1,
        },
      },
    });
    expect(matchesLiveLocal(t, f({ liveLocalOnly: true }))).toBe(true);
  });

  it("false for a long-quiet local target: locally_received but no live local contributor", () => {
    const quiet = track("quiet", true, {
      attributes: {
        fusion: {
          active_source: "demo-net",
          contributors: [
            // local contributor exists but is no longer live (expired) ...
            { source: "local_adsb", local_rf: true, observed_at: NOW, freshness: "expired" },
            // ... a network contributor is the only live leg now
            { source: "demo-net", local_rf: false, observed_at: NOW, freshness: "live" },
          ],
          field_sources: {},
          field_freshness: {},
          last_local_rf_at: NOW, // last heard locally survives — but NOT "live"
          fused_count: 2,
        },
      },
    });
    expect(matchesLiveLocal(quiet, f({ liveLocalOnly: true }))).toBe(false);
  });

  it("false when not locally received at all", () => {
    expect(matchesLiveLocal(track("net", false), f({ liveLocalOnly: true }))).toBe(false);
  });

  it("unfused local leg (no fusion block) falls back to locally_received", () => {
    expect(matchesLiveLocal(track("loc", true), f({ liveLocalOnly: true }))).toBe(true);
  });

  it("does not throw on a malformed fusion block (PRD §37): bad/null contributors", () => {
    // A present-but-malformed fusion block (wrong-typed or null-element
    // contributors) must read as "unknown leg" and fall back to locally_received,
    // never throw out of the render memo and blank the COP.
    const bad = (fusion: unknown) =>
      track("bad", true, { attributes: { fusion } });
    const active = f({ liveLocalOnly: true });
    // Each of these previously threw a TypeError in .some(); now they fall back.
    expect(() => matchesLiveLocal(bad({}), active)).not.toThrow();
    expect(matchesLiveLocal(bad({}), active)).toBe(true);
    expect(() => matchesLiveLocal(bad({ contributors: "x" }), active)).not.toThrow();
    expect(matchesLiveLocal(bad({ contributors: "x" }), active)).toBe(true);
    expect(() => matchesLiveLocal(bad({ contributors: null }), active)).not.toThrow();
    expect(matchesLiveLocal(bad({ contributors: null }), active)).toBe(true);
    // A well-formed array with a null contributor element survives fusionMeta;
    // the consumer must skip the null leg (no live local contributor) → false,
    // never throw on `null.local_rf`.
    expect(() =>
      matchesLiveLocal(bad({ contributors: [null] }), active),
    ).not.toThrow();
    expect(matchesLiveLocal(bad({ contributors: [null] }), active)).toBe(false);
  });
});

describe("matchesSource", () => {
  it("inactive (null) passes; active gates on membership", () => {
    const t = track("a", true, { source: "ais" });
    expect(matchesSource(t, f())).toBe(true);
    expect(matchesSource(t, f({ sources: new Set(["ais"]) }))).toBe(true);
    expect(matchesSource(t, f({ sources: new Set(["demo"]) }))).toBe(false);
  });
});

describe("matchesTrackType", () => {
  it("inactive passes; active gates", () => {
    const t = track("a", true, { track_type: "vessel" });
    expect(matchesTrackType(t, f())).toBe(true);
    expect(matchesTrackType(t, f({ trackTypes: new Set(["vessel"]) }))).toBe(true);
    expect(matchesTrackType(t, f({ trackTypes: new Set(["aircraft"]) }))).toBe(false);
  });
});

describe("withinRange (haversine; no-op when station unset)", () => {
  it("returns true (no-op) when stationCenter is null even with a max set", () => {
    expect(withinRange(track("a", true), f({ rangeNmMax: 1 }), ctx())).toBe(true);
  });

  it("haversine: 1 deg of latitude ~= 60 NM", () => {
    // ~111.2 km between (0,0) and (0,1).
    const d = haversineM({ lon: 0, lat: 0 }, { lon: 0, lat: 1 });
    expect(d).toBeGreaterThan(111_000);
    expect(d).toBeLessThan(111_400);
  });

  it("includes inside, excludes outside the radius when station set", () => {
    const station = { lon: 0, lat: 0 };
    const near = track("near", true, {
      geometry: { type: "Point", coordinates: [0, 0.5] }, // ~30 NM
    });
    const far = track("far", true, {
      geometry: { type: "Point", coordinates: [0, 2] }, // ~120 NM
    });
    const filt = f({ rangeNmMax: 60 });
    expect(withinRange(near, filt, ctx({ stationCenter: station }))).toBe(true);
    expect(withinRange(far, filt, ctx({ stationCenter: station }))).toBe(false);
  });

  it("active range with no geometry is a no-match", () => {
    const t = track("nogeo", true, { geometry: null });
    expect(
      withinRange(t, f({ rangeNmMax: 60 }), ctx({ stationCenter: { lon: 0, lat: 0 } })),
    ).toBe(false);
  });
});

describe("withinAltitude / withinSpeed", () => {
  it("altitude band, missing altitude is a no-match when active", () => {
    expect(withinAltitude(track("a", true, { altitude_m: 5000 }), f())).toBe(true);
    expect(
      withinAltitude(track("a", true, { altitude_m: 5000 }), f({ altitudeMinM: 1000 })),
    ).toBe(true);
    expect(
      withinAltitude(track("a", true, { altitude_m: 500 }), f({ altitudeMinM: 1000 })),
    ).toBe(false);
    expect(
      withinAltitude(track("a", true, { altitude_m: 5000 }), f({ altitudeMaxM: 1000 })),
    ).toBe(false);
    expect(
      withinAltitude(track("a", true, { altitude_m: null }), f({ altitudeMaxM: 1000 })),
    ).toBe(false);
  });

  it("speed band, missing speed is a no-match when active", () => {
    expect(withinSpeed(track("a", true, { speed_mps: 100 }), f())).toBe(true);
    expect(
      withinSpeed(track("a", true, { speed_mps: 100 }), f({ speedMinMps: 50 })),
    ).toBe(true);
    expect(
      withinSpeed(track("a", true, { speed_mps: 10 }), f({ speedMinMps: 50 })),
    ).toBe(false);
    expect(
      withinSpeed(track("a", true, { speed_mps: null }), f({ speedMaxMps: 200 })),
    ).toBe(false);
  });
});

describe("withinAge", () => {
  it("inactive passes", () => {
    expect(withinAge(track("a", true), f(), ctx())).toBe(true);
  });

  it("gates on now - observed_at", () => {
    const old = track("old", true, { observed_at: "2026-06-14T23:00:00Z" }); // 1h old
    expect(withinAge(old, f({ ageMaxS: 60 }), ctx())).toBe(false);
    expect(withinAge(old, f({ ageMaxS: 7200 }), ctx())).toBe(true);
  });

  it("missing/unparseable observed_at passes (unknown leg, not hidden)", () => {
    const bad = track("bad", true, { observed_at: "not-a-date" });
    expect(withinAge(bad, f({ ageMaxS: 1 }), ctx())).toBe(true);
  });
});

describe("matchesMilitary / matchesMilitaryBasis", () => {
  const milTrack = track("mil", true, {
    classification: { military: true, basis: "address_block", confidence: "low", note: null },
  });
  const civTrack = track("civ", true, {
    classification: { military: false, basis: "provider", confidence: "high", note: null },
  });
  const unknownTrack = track("unk", true); // classification null

  it("military filter", () => {
    expect(matchesMilitary(milTrack, f())).toBe(true); // any
    expect(matchesMilitary(milTrack, f({ military: "military" }))).toBe(true);
    expect(matchesMilitary(civTrack, f({ military: "military" }))).toBe(false);
    expect(matchesMilitary(civTrack, f({ military: "civil" }))).toBe(true);
    expect(matchesMilitary(milTrack, f({ military: "civil" }))).toBe(false);
    // unknown classification is asserted neither military nor civil
    expect(matchesMilitary(unknownTrack, f({ military: "military" }))).toBe(false);
    expect(matchesMilitary(unknownTrack, f({ military: "civil" }))).toBe(false);
  });

  it("military basis filter; null classification does not throw and maps to 'unknown'", () => {
    expect(matchesMilitaryBasis(milTrack, f())).toBe(true); // inactive
    expect(
      matchesMilitaryBasis(milTrack, f({ militaryBasis: new Set(["address_block"]) })),
    ).toBe(true);
    expect(
      matchesMilitaryBasis(milTrack, f({ militaryBasis: new Set(["provider"]) })),
    ).toBe(false);
    expect(() =>
      matchesMilitaryBasis(unknownTrack, f({ militaryBasis: new Set(["provider"]) })),
    ).not.toThrow();
    expect(
      matchesMilitaryBasis(unknownTrack, f({ militaryBasis: new Set(["unknown"]) })),
    ).toBe(true);
  });
});

describe("AIS predicates (defensive attribute reads)", () => {
  const vessel = track("v", false, {
    track_type: "vessel",
    source: "ais",
    label: "ais:vessel:123456789",
    attributes: {
      mmsi: "123456789",
      vessel_name: "EVER GIVEN",
      destination: "ROTTERDAM",
      ship_type: 70,
      ship_type_text: "Cargo",
      nav_status: 0,
      nav_status_text: "Under way using engine",
    },
  });
  const bare = track("b", false, { track_type: "vessel", source: "ais", attributes: {} });

  it("vessel type / nav status gate on the raw int code", () => {
    expect(matchesAisVesselType(vessel, f({ ais: { ...defaultFilters().ais, vesselTypes: new Set([70]) } }))).toBe(true);
    expect(matchesAisVesselType(vessel, f({ ais: { ...defaultFilters().ais, vesselTypes: new Set([80]) } }))).toBe(false);
    expect(matchesAisNavStatus(vessel, f({ ais: { ...defaultFilters().ais, navStatuses: new Set([0]) } }))).toBe(true);
    expect(matchesAisNavStatus(vessel, f({ ais: { ...defaultFilters().ais, navStatuses: new Set([1]) } }))).toBe(false);
  });

  it("name / mmsi / destination are case-insensitive substrings", () => {
    expect(matchesAisName(vessel, f({ ais: { ...defaultFilters().ais, nameLike: "ever" } }))).toBe(true);
    expect(matchesAisMmsi(vessel, f({ ais: { ...defaultFilters().ais, mmsiLike: "4567" } }))).toBe(true);
    expect(matchesAisDestination(vessel, f({ ais: { ...defaultFilters().ais, destinationLike: "rott" } }))).toBe(true);
    expect(matchesAisName(vessel, f({ ais: { ...defaultFilters().ais, nameLike: "nope" } }))).toBe(false);
  });

  it("missing attribute is a no-match (when active) and never throws", () => {
    const nameActive = f({ ais: { ...defaultFilters().ais, nameLike: "x" } });
    const mmsiActive = f({ ais: { ...defaultFilters().ais, mmsiLike: "x" } });
    const destActive = f({ ais: { ...defaultFilters().ais, destinationLike: "x" } });
    const typeActive = f({ ais: { ...defaultFilters().ais, vesselTypes: new Set([70]) } });
    expect(() => matchesAisName(bare, nameActive)).not.toThrow();
    expect(matchesAisName(bare, nameActive)).toBe(false);
    expect(matchesAisMmsi(bare, mmsiActive)).toBe(false);
    expect(matchesAisDestination(bare, destActive)).toBe(false);
    expect(matchesAisVesselType(bare, typeActive)).toBe(false);
  });
});

describe("matchesAprsCallsign (substring on track.label)", () => {
  const aprs = track("a", true, { track_type: "aprs_station", label: "N0CALL-9" });
  it("inactive passes; active substring matches the label", () => {
    expect(matchesAprsCallsign(aprs, f())).toBe(true);
    expect(matchesAprsCallsign(aprs, f({ aprsCallsignLike: "n0call" }))).toBe(true);
    expect(matchesAprsCallsign(aprs, f({ aprsCallsignLike: "w1aw" }))).toBe(false);
  });
  it("null label is a no-match when active, never throws", () => {
    const noLabel = track("x", true, { label: null });
    expect(() => matchesAprsCallsign(noLabel, f({ aprsCallsignLike: "x" }))).not.toThrow();
    expect(matchesAprsCallsign(noLabel, f({ aprsCallsignLike: "x" }))).toBe(false);
  });
});

describe("visibleTracks composition", () => {
  const tracks = new Map<string, TrackRecord>([
    ["a", track("a", true)],
    ["b", track("b", false)],
    ["c", track("c", true)],
  ]);

  it("an untouched DisplayFilters is an exact no-op (returns all tracks)", () => {
    expect(visibleTracks(tracks, defaultFilters(), ctx()).map((t) => t.id).sort()).toEqual([
      "a",
      "b",
      "c",
    ]);
  });

  it("preserves the existing provenance subsets (all/local/network)", () => {
    expect(
      visibleTracks(tracks, f({ provenance: "all" }), ctx())
        .map((t) => t.id)
        .sort(),
    ).toEqual(["a", "b", "c"]);
    expect(
      visibleTracks(tracks, f({ provenance: "local" }), ctx())
        .map((t) => t.id)
        .sort(),
    ).toEqual(["a", "c"]);
    expect(
      visibleTracks(tracks, f({ provenance: "network" }), ctx()).map((t) => t.id),
    ).toEqual(["b"]);
  });

  it("ANDs multiple active predicates", () => {
    const mixed = new Map<string, TrackRecord>([
      ["hi", track("hi", true, { altitude_m: 10000, track_type: "aircraft" })],
      ["lo", track("lo", true, { altitude_m: 100, track_type: "aircraft" })],
      ["ship", track("ship", true, { altitude_m: 10000, track_type: "vessel" })],
    ]);
    // aircraft AND altitude >= 5000 → only "hi"
    const out = visibleTracks(
      mixed,
      f({ trackTypes: new Set(["aircraft"]), altitudeMinM: 5000 }),
      ctx(),
    );
    expect(out.map((t) => t.id)).toEqual(["hi"]);
  });

  it("empty map gives empty list", () => {
    expect(visibleTracks(new Map(), defaultFilters(), ctx())).toEqual([]);
  });

  it("never mutates the input map", () => {
    const before = tracks.size;
    visibleTracks(tracks, f({ provenance: "local" }), ctx());
    expect(tracks.size).toBe(before);
  });

  it("watchlistOnly with empty watchlist yields no tracks", () => {
    expect(visibleTracks(tracks, f({ watchlistOnly: true }), ctx())).toEqual([]);
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

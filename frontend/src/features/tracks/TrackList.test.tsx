import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { TrackList } from "./TrackList";
import { emptyState } from "../../state/liveState";
import { defaultFilters, useStore, type ProvenanceFilter } from "../../state/store";
import type { TrackRecord } from "../../types/records";

// jsdom client render: zustand's server snapshot is memoized at store creation,
// so renderToStaticMarkup would read stale empty state. The test environment is
// jsdom, so we render on a real DOM node and read innerHTML before unmounting.
(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT =
  true;

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

function setTracks(tracks: TrackRecord[], filter: ProvenanceFilter = "all") {
  const live = emptyState();
  live.tracks = new Map(tracks.map((t) => [t.id, t]));
  // provenance is now one field of the DisplayFilters object (M3.6a).
  useStore.setState({ live, filters: { ...defaultFilters(), provenance: filter } });
}

/** Render TrackList against current store state and return its HTML. */
function render(): string {
  const el = document.createElement("div");
  const root = createRoot(el);
  act(() => {
    root.render(<TrackList />);
  });
  const html = el.innerHTML;
  act(() => {
    root.unmount();
  });
  return html;
}

const FUSED = track("aircraft:icao:demo01", true, {
  label: "DEMO-FUSE",
  attributes: {
    fusion: {
      active_source: "local_adsb",
      contributors: [
        { source: "demo", local_rf: true, observed_at: NOW, freshness: "live" },
        { source: "demo-net", local_rf: false, observed_at: NOW, freshness: "live" },
      ],
      field_sources: { geometry: "demo", speed_mps: "demo-net" },
      field_freshness: { geometry: "live", speed_mps: "live" },
      last_local_rf_at: NOW,
      fused_count: 2,
    },
  },
});

describe("TrackList", () => {
  beforeEach(() => {
    useStore.setState({ live: emptyState(), filters: defaultFilters() });
  });
  afterEach(() => {
    useStore.setState({ live: emptyState(), filters: defaultFilters() });
  });

  it("shows a contributor badge and LOCAL provenance for a fused track", () => {
    setTracks([FUSED]);
    const html = render();
    expect(html).toContain("×2"); // contributor badge for fused_count > 1
    expect(html).toContain("LOCAL"); // locally_received drives the prov badge
    expect(html).toContain("DEMO-FUSE");
  });

  it("changes the N of M count when the provenance filter changes", () => {
    const tracks = [track("a", true), track("b", false)];
    setTracks(tracks, "all");
    expect(render()).toContain("Tracks (2)");

    setTracks(tracks, "local");
    const localHtml = render();
    expect(localHtml).toContain("Tracks (1 of 2)");
  });

  it("renders a track with no fusion attributes without crashing", () => {
    setTracks([track("plain", false)]);
    const html = render();
    expect(html).toContain("NET");
    expect(html).not.toContain("×"); // no contributor badge
  });
});

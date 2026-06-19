import { describe, expect, it } from "vitest";
import {
  applyDelta,
  applyFrame,
  applySnapshot,
  emptyState,
  RECENT_EVENTS_MAX,
} from "./liveState";
import type {
  AlertRecord,
  EventRecord,
  GeoFeatureRecord,
  SnapshotFrame,
  SourceStatusRecord,
  TrackRecord,
} from "../types/records";

const NOW = "2026-06-15T00:00:00Z";

function track(id: string, over: Partial<TrackRecord> = {}): TrackRecord {
  return {
    schema_version: 2,
    kind: "track",
    id,
    source: "demo",
    observed_at: NOW,
    received_at: NOW,
    published_at: NOW,
    provenance: [],
    tags: [],
    attributes: {},
    track_type: "aircraft",
    label: id,
    geometry: { type: "Point", coordinates: [-80, 35] },
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

function status(source: string, over: Partial<SourceStatusRecord> = {}): SourceStatusRecord {
  return {
    schema_version: 2,
    kind: "source_status",
    id: `status:${source}`,
    source,
    observed_at: NOW,
    received_at: NOW,
    published_at: NOW,
    provenance: [],
    tags: [],
    attributes: {},
    status: "connected",
    last_success_at: NOW,
    last_record_at: NOW,
    lag_s: 0,
    records_received: 1,
    records_rejected: 0,
    error_code: null,
    error_summary: null,
    ...over,
  };
}

function event(id: string): EventRecord {
  return {
    schema_version: 2,
    kind: "event",
    id,
    source: "demo",
    observed_at: NOW,
    received_at: NOW,
    published_at: NOW,
    provenance: [],
    tags: [],
    attributes: {},
    event_type: "test",
    subject_id: null,
    summary: id,
    message: null,
    geometry: null,
    severity: null,
  };
}

function snapshot(seq: number, tracks: TrackRecord[] = [], cseq = 0): SnapshotFrame {
  return {
    type: "snapshot",
    seq,
    cseq,
    tracks,
    features: [],
    events: [],
    alerts: [],
    source_status: [],
  };
}

describe("applySnapshot", () => {
  it("replaces state and clears staleness", () => {
    const s = applySnapshot(snapshot(10, [track("a"), track("b")]));
    expect(s.seq).toBe(10);
    expect(s.stale).toBe(false);
    expect(s.tracks.size).toBe(2);
  });

  it("caps the events ring to RECENT_EVENTS_MAX", () => {
    const events = Array.from({ length: RECENT_EVENTS_MAX + 50 }, (_, i) =>
      event(`e${i}`),
    );
    const s = applySnapshot({ ...snapshot(1), events });
    expect(s.events.length).toBe(RECENT_EVENTS_MAX);
    expect(s.events[s.events.length - 1].id).toBe(`e${RECENT_EVENTS_MAX + 49}`);
  });
});

describe("applyDelta per-connection cseq continuity (PRD §22.5)", () => {
  it("applies a delta with cseq === current+1", () => {
    const base = applySnapshot(snapshot(5, [], 0));
    const { state, outcome } = applyDelta(base, {
      type: "track_upsert",
      seq: 6,
      cseq: 1,
      record: track("a"),
    });
    expect(outcome).toBe("applied");
    expect(state.cseq).toBe(1);
    expect(state.seq).toBe(6);
    expect(state.tracks.has("a")).toBe(true);
  });

  it("applies even when the GLOBAL seq skips, as long as cseq is contiguous", () => {
    // A filtered connection: seq jumps 5→20 (intervening frames filtered out) but
    // cseq is still contiguous 0→1, so the delta applies (no false resync).
    const base = applySnapshot(snapshot(5, [], 0));
    const { state, outcome } = applyDelta(base, {
      type: "track_upsert",
      seq: 20,
      cseq: 1,
      record: track("a"),
    });
    expect(outcome).toBe("applied");
    expect(state.cseq).toBe(1);
    expect(state.tracks.has("a")).toBe(true);
  });

  it("ignores a duplicate/stale delta (cseq <= current)", () => {
    const base = applySnapshot(snapshot(5, [track("a")], 3));
    const { state, outcome } = applyDelta(base, {
      type: "track_upsert",
      seq: 6,
      cseq: 3,
      record: track("a", { label: "stale" }),
    });
    expect(outcome).toBe("duplicate");
    expect(state).toBe(base); // unchanged reference
  });

  it("marks stale and does not apply on a cseq gap (real drop)", () => {
    const base = applySnapshot(snapshot(5, [], 0));
    const { state, outcome } = applyDelta(base, {
      type: "track_upsert",
      seq: 8,
      cseq: 3, // expected 1 — a true per-connection drop
      record: track("a"),
    });
    expect(outcome).toBe("gap");
    expect(state.stale).toBe(true);
    expect(state.cseq).toBe(0); // not advanced
    expect(state.tracks.has("a")).toBe(false); // not applied
  });

  it("a fresh snapshot recovers from stale and re-anchors cseq", () => {
    const base = { ...applySnapshot(snapshot(5, [], 7)), stale: true };
    const recovered = applyFrame(base, snapshot(9, [track("z")], 0)).state;
    expect(recovered.stale).toBe(false);
    expect(recovered.seq).toBe(9);
    expect(recovered.cseq).toBe(0); // resync point
  });
});

describe("delta kinds", () => {
  it("removes a track", () => {
    const base = applySnapshot(snapshot(1, [track("a")], 0));
    const { state } = applyDelta(base, {
      type: "remove",
      seq: 2,
      cseq: 1,
      kind: "track",
      id: "a",
    });
    expect(state.tracks.has("a")).toBe(false);
  });

  it("keys source_status by source name", () => {
    const base = applySnapshot(snapshot(1));
    const { state } = applyDelta(base, {
      type: "source_status",
      seq: 2,
      cseq: 1,
      record: status("local_adsb"),
    });
    expect(state.sourceStatus.get("local_adsb")?.status).toBe("connected");
  });

  it("appends events with a bounded ring", () => {
    let state = applySnapshot(snapshot(0));
    for (let i = 1; i <= RECENT_EVENTS_MAX + 10; i++) {
      state = applyDelta(state, {
        type: "event",
        seq: i,
        cseq: i,
        record: event(`e${i}`),
      }).state;
    }
    expect(state.events.length).toBe(RECENT_EVENTS_MAX);
  });

  it("upserts an alert", () => {
    const base = applySnapshot(snapshot(1));
    const alert: AlertRecord = {
      schema_version: 2,
      kind: "alert",
      id: "al1",
      source: "engine",
      observed_at: NOW,
      received_at: NOW,
      published_at: NOW,
      provenance: [],
      tags: [],
      attributes: {},
      rule_id: "r1",
      subject_id: null,
      state: "open",
      severity: "high",
      title: "T",
      summary: "S",
      triggered_at: NOW,
      acknowledged_at: null,
      resolved_at: null,
      delivery_status: {},
    };
    const { state } = applyDelta(base, {
      type: "alert_upsert",
      seq: 2,
      cseq: 1,
      record: alert,
    });
    expect(state.alerts.get("al1")?.severity).toBe("high");
  });

  it("upserts a geo-feature", () => {
    const base = applySnapshot(snapshot(1));
    const feat: GeoFeatureRecord = {
      schema_version: 2,
      kind: "feature",
      id: "f1",
      source: "faa_tfr",
      observed_at: NOW,
      received_at: NOW,
      published_at: NOW,
      provenance: [],
      tags: [],
      attributes: {},
      feature_type: "tfr",
      geometry: { type: "Polygon", coordinates: [] },
      valid_from: null,
      valid_until: null,
      severity: null,
      label: "TFR 1",
    };
    const { state } = applyDelta(base, {
      type: "feature_upsert",
      seq: 2,
      cseq: 1,
      record: feat,
    });
    expect(state.features.get("f1")?.feature_type).toBe("tfr");
  });
});

describe("emptyState", () => {
  it("starts at seq/cseq -1 so the first snapshot (cseq 0) applies", () => {
    expect(emptyState().seq).toBe(-1);
    expect(emptyState().cseq).toBe(-1);
  });
});

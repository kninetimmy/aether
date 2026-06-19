// React-facing live-state store (Zustand). Wraps the framework-free LiveState +
// WsClient: a single client instance feeds snapshots/deltas in, components select
// slices out. Keeping the reducer pure (liveState.ts) means this layer is just
// glue — no business logic lives here.

import { create } from "zustand";
import { WsClient, type ConnectionStatus } from "../api/wsClient";
import { emptyState, type LiveState } from "./liveState";
import type { TrackType } from "../types/records";

/**
 * Client-side provenance display filter (PRD §16.5 + the flagship "collapse to
 * local-only" principle, PRD §8.2). DISPLAY ONLY — it never affects ingestion;
 * the backend still fuses every source.
 */
export type ProvenanceFilter = "all" | "local" | "network";

export type MilitaryFilter = "any" | "military" | "civil";

export type MilitaryBasis = "provider" | "address_block" | "both" | "unknown";

/** AIS sub-filters; ship_type / nav_status key off the raw ITU int code. */
export interface AisFilters {
  vesselTypes: Set<number> | null;
  navStatuses: Set<number> | null;
  nameLike: string | null;
  mmsiLike: string | null;
  destinationLike: string | null;
}

/**
 * The full client-side display-filter object (PRD §16.5 + COP-FR-009 +
 * AIS-FR-005 + APRSIS-FR-006). DISPLAY ONLY — post-ingestion, AND-chained as
 * pure predicates in selectors.ts. The default (all-null / "all" / "any") is an
 * exact no-op so first-load behavior is identical to today.
 */
export interface DisplayFilters {
  provenance: ProvenanceFilter;
  liveLocalOnly: boolean;
  sources: Set<string> | null;
  trackTypes: Set<TrackType> | null;
  rangeNmMax: number | null;
  altitudeMinM: number | null;
  altitudeMaxM: number | null;
  speedMinMps: number | null;
  speedMaxMps: number | null;
  ageMaxS: number | null;
  military: MilitaryFilter;
  militaryBasis: Set<MilitaryBasis> | null;
  ais: AisFilters;
  aprsCallsignLike: string | null;
  watchlistOnly: boolean;
}

/** Default = all-null / any: an exact no-op (first load behaves like today). */
export function defaultFilters(): DisplayFilters {
  return {
    provenance: "all",
    liveLocalOnly: false,
    sources: null,
    trackTypes: null,
    rangeNmMax: null,
    altitudeMinM: null,
    altitudeMaxM: null,
    speedMinMps: null,
    speedMaxMps: null,
    ageMaxS: null,
    military: "any",
    militaryBasis: null,
    ais: {
      vesselTypes: null,
      navStatuses: null,
      nameLike: null,
      mmsiLike: null,
      destinationLike: null,
    },
    aprsCallsignLike: null,
    watchlistOnly: false,
  };
}

export interface AppState {
  live: LiveState;
  connection: ConnectionStatus;
  /** Layer visibility toggles, keyed by presentation layer id. */
  layerVisible: Record<string, boolean>;
  /** Full client-side display filters (provenance is one field within). */
  filters: DisplayFilters;
  /**
   * Runtime-injected station origin for the range-from-station filter; null when
   * unconfigured, which degrades the range control to a disabled no-op. NEVER
   * hardcoded / committed — canonical station config arrives in M3.6b (PRD §5).
   */
  stationCenter: { lon: number; lat: number } | null;
  /**
   * Wall clock (ms) bumped by a 1s tick so the age + live-LOCAL filters
   * re-evaluate and the filtered set doesn't silently drift between frames.
   */
  clock: number;
  client: WsClient | null;
  connect: (url?: string) => void;
  disconnect: () => void;
  setLayerVisible: (layer: string, visible: boolean) => void;
  setProvenanceFilter: (filter: ProvenanceFilter) => void;
  setFilters: (patch: Partial<DisplayFilters>) => void;
  resetFilters: () => void;
  setStationCenter: (center: { lon: number; lat: number } | null) => void;
  tickClock: () => void;
}

export const useStore = create<AppState>((set, get) => ({
  live: emptyState(),
  connection: "closed",
  layerVisible: {},
  filters: defaultFilters(),
  stationCenter: null,
  clock: Date.now(),
  client: null,

  connect: (url?: string) => {
    if (get().client) return; // idempotent — one socket per app
    const client = new WsClient(
      {
        onState: (live) => set({ live }),
        onStatus: (connection) => set({ connection }),
      },
      url ? { url } : {},
    );
    set({ client });
    client.connect();
  },

  disconnect: () => {
    get().client?.close();
    set({ client: null, connection: "closed" });
  },

  setLayerVisible: (layer, visible) =>
    set((s) => ({ layerVisible: { ...s.layerVisible, [layer]: visible } })),

  // Thin wrapper over filters.provenance so the existing radiogroup + tests keep
  // working while provenance is just one field of the DisplayFilters object.
  setProvenanceFilter: (provenance) =>
    set((s) => ({ filters: { ...s.filters, provenance } })),

  // Shallow-merge patch into the filters object (DISPLAY ONLY; never ingestion).
  setFilters: (patch) => set((s) => ({ filters: { ...s.filters, ...patch } })),

  resetFilters: () => set({ filters: defaultFilters() }),

  setStationCenter: (stationCenter) => set({ stationCenter }),

  tickClock: () => set({ clock: Date.now() }),
}));

/** A layer is visible unless explicitly toggled off (default-on). */
export function isLayerVisible(state: AppState, layer: string): boolean {
  return state.layerVisible[layer] !== false;
}

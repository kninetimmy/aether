// React-facing live-state store (Zustand). Wraps the framework-free LiveState +
// WsClient: a single client instance feeds snapshots/deltas in, components select
// slices out. Keeping the reducer pure (liveState.ts) means this layer is just
// glue — no business logic lives here.

import { create } from "zustand";
import { WsClient, type ConnectionStatus } from "../api/wsClient";
import { createReplaySession, deleteReplaySession } from "../api/replayClient";
import { emptyState, type LiveState } from "./liveState";
import {
  clampCursor,
  emptyReplay,
  sessionBoundsMs,
  type ReplaySlice,
} from "./replay";
import type { ReplaySessionRequest, TrackType } from "../types/records";

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
  /**
   * Orbital category (CelesTrak GP group, e.g. "stations"/"starlink"). null =
   * no-op; a Set keeps only orbital_object tracks whose attributes.group is a
   * member. NON-orbital tracks always pass (see matchesOrbitalCategory).
   */
  orbitalCategory: Set<string> | null;
  /**
   * Minimum elevation (deg above the horizon) for orbital_object tracks. null =
   * no-op. NARROWS within the backend-transmitted set (>= inclusive); it can
   * never reveal objects below the station's configured emission floor, since
   * those are not sent. Non-orbital tracks always pass.
   */
  orbitalMinElevationDeg: number | null;
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
    orbitalCategory: null,
    orbitalMinElevationDeg: null,
  };
}

/**
 * Runtime orbital config from /api/config (M6.6a). `enabled` gates whether the
 * UI renders the orbital filter controls at all; `groups` populates the category
 * chips; `minElevationDeg` is the BACKEND emission floor (display-only helper
 * text — the client narrows within, never below, it). null until fetched.
 */
export interface OrbitalConfig {
  enabled: boolean;
  groups: string[];
  minElevationDeg: number;
}

/** Raw `/api/config` orbital block (snake_case wire shape). */
export interface OrbitalConfigApi {
  enabled: boolean;
  groups: string[];
  min_elevation_deg: number;
}

/**
 * Map the raw `/api/config` orbital block to {@link OrbitalConfig}, or null when
 * the block is absent (older backend / failed fetch) so the controls stay off
 * (PRD §37 graceful degradation). Pure — kept here so the snake_case→camelCase
 * contract is unit-testable without mounting the (MapLibre) App.
 */
export function orbitalConfigFromApi(
  orbital: OrbitalConfigApi | null | undefined,
): OrbitalConfig | null {
  if (!orbital) return null;
  return {
    enabled: orbital.enabled,
    groups: orbital.groups,
    minElevationDeg: orbital.min_elevation_deg,
  };
}

/**
 * Per-target operator annotation for a watchlisted TOI (PRD §24.6 label/priority).
 * localStorage-only in M3.6 — server CRUD + the SQLite `watchlist` table are
 * deferred to M4.
 */
export interface ToiMeta {
  label?: string;
  priority?: number;
}

// localStorage key for the persisted watchlist (stable keys, JSON array). The
// `.v1` suffix lets a future schema change migrate without colliding (PRD §24.6).
const WATCHLIST_KEY = "aether.toi.watchlist.v1";

/** Hydrate the watchlist Set from localStorage; tolerant of any malformed blob. */
function loadWatchlist(): Set<string> {
  try {
    const raw = globalThis.localStorage?.getItem(WATCHLIST_KEY);
    if (!raw) return new Set();
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) return new Set();
    return new Set(parsed.filter((k): k is string => typeof k === "string"));
  } catch {
    // A corrupt/inaccessible store must never crash store creation (PRD §37).
    return new Set();
  }
}

/** Write-through the watchlist to localStorage; failures are non-fatal. */
function saveWatchlist(watchlist: Set<string>): void {
  try {
    globalThis.localStorage?.setItem(WATCHLIST_KEY, JSON.stringify([...watchlist]));
  } catch {
    // Private-mode / quota errors must not break a toggle.
  }
}

export interface AppState {
  live: LiveState;
  connection: ConnectionStatus;
  /** Layer visibility toggles, keyed by presentation layer id. */
  layerVisible: Record<string, boolean>;
  /** Full client-side display filters (provenance is one field within). */
  filters: DisplayFilters;
  /**
   * TOI watchlist: STABLE keys (watchlistKey, NOT the ephemeral source-prefixed
   * track.id) so a highlight survives reconnect and LOCAL→NET fusion handoff.
   * Hydrated from localStorage on store creation; write-through on every mutate.
   */
  watchlist: Set<string>;
  /** Optional per-TOI label/priority (PRD §24.6), keyed by the same stable key. */
  toiMeta: Map<string, ToiMeta>;
  /** Currently-selected track id (for the TOI details panel); null when none. */
  selectedTrackId: string | null;
  /**
   * Runtime-injected station origin for the range-from-station filter; null when
   * unconfigured, which degrades the range control to a disabled no-op. NEVER
   * hardcoded / committed — canonical station config arrives in M3.6b (PRD §5).
   */
  stationCenter: { lon: number; lat: number } | null;
  /**
   * Runtime orbital config from /api/config (M6.6a); null until fetched. When
   * unset or `enabled:false` the FilterPanel omits the orbital controls entirely.
   * Carries no secrets / no coordinates (the contract is groups + emission floor).
   */
  orbitalConfig: OrbitalConfig | null;
  /**
   * Wall clock (ms) bumped by a 1s tick so the age + live-LOCAL filters
   * re-evaluate and the filtered set doesn't silently drift between frames.
   */
  clock: number;
  /**
   * Record/replay slice (M4.8, PRD §19.6). `mode` gates whether the app shows the
   * LIVE firehose or a REPLAYED snapshot; the buffer lives entirely client-side and
   * is played against `cursorMs`. Live state above is NEVER touched by replay — the
   * structural half of "replay can't fire live alerts" (PRD §19.6/§32).
   */
  replay: ReplaySlice;
  client: WsClient | null;
  connect: (url?: string) => void;
  disconnect: () => void;
  setLayerVisible: (layer: string, visible: boolean) => void;
  setProvenanceFilter: (filter: ProvenanceFilter) => void;
  setFilters: (patch: Partial<DisplayFilters>) => void;
  resetFilters: () => void;
  setStationCenter: (center: { lon: number; lat: number } | null) => void;
  setOrbitalConfig: (config: OrbitalConfig | null) => void;
  tickClock: () => void;
  /** Add/remove a stable watchlist key (write-through to localStorage). */
  toggleWatchlist: (key: string) => void;
  /** Remove a stable watchlist key (write-through to localStorage). */
  removeFromWatchlist: (key: string) => void;
  /** Shallow-merge label/priority annotation for a TOI key (PRD §24.6). */
  setToiMeta: (key: string, patch: ToiMeta) => void;
  /** Select a track for the details panel (null clears the selection). */
  selectTrack: (id: string | null) => void;

  // --- Replay actions (M4.8, PRD §19.6/§21.6) ------------------------------
  /**
   * Enter replay: fetch a bounded session over REST, load the buffer, switch to
   * REPLAY mode, park the cursor at the window start, PAUSED. Rejects (the caller
   * shows the error) on a 503/400/transport failure — mode stays LIVE on failure.
   * Live state is untouched throughout.
   */
  enterReplay: (req: ReplaySessionRequest) => Promise<void>;
  /**
   * Return to LIVE: drop the session/buffer and switch back. The live firehose was
   * never interrupted, so this is an instant restore. Best-effort server teardown.
   */
  exitReplay: () => void;
  /** Start playback (no-op outside replay or with no session). */
  play: () => void;
  /** Pause playback. */
  pause: () => void;
  /** Set the playback rate (× real time). */
  setSpeed: (speed: number) => void;
  /** Advance/rewind the cursor by deltaMs, clamped to the session window. */
  step: (deltaMs: number) => void;
  /** Jump the cursor to an absolute epoch-ms time, clamped to the window. */
  seek: (tMs: number) => void;
  /**
   * Playback tick: advance the cursor by `advanceMs` while playing; auto-pause at
   * the window end. Driven by an App setInterval effect (mirrors tickClock).
   */
  tick: (advanceMs: number) => void;
}

export const useStore = create<AppState>((set, get) => ({
  live: emptyState(),
  connection: "closed",
  layerVisible: {},
  filters: defaultFilters(),
  watchlist: loadWatchlist(),
  toiMeta: new Map(),
  selectedTrackId: null,
  stationCenter: null,
  orbitalConfig: null,
  clock: Date.now(),
  replay: emptyReplay(),
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

  setOrbitalConfig: (orbitalConfig) => set({ orbitalConfig }),

  tickClock: () => set({ clock: Date.now() }),

  // Watchlist mutators write a NEW Set (Zustand identity change → re-render) and
  // write-through to localStorage. Keys are stable watchlistKeys, never raw ids.
  toggleWatchlist: (key) =>
    set((s) => {
      const watchlist = new Set(s.watchlist);
      if (watchlist.has(key)) watchlist.delete(key);
      else watchlist.add(key);
      saveWatchlist(watchlist);
      return { watchlist };
    }),

  removeFromWatchlist: (key) =>
    set((s) => {
      if (!s.watchlist.has(key)) return {};
      const watchlist = new Set(s.watchlist);
      watchlist.delete(key);
      saveWatchlist(watchlist);
      return { watchlist };
    }),

  setToiMeta: (key, patch) =>
    set((s) => {
      const toiMeta = new Map(s.toiMeta);
      toiMeta.set(key, { ...toiMeta.get(key), ...patch });
      return { toiMeta };
    }),

  selectTrack: (selectedTrackId) => set({ selectedTrackId }),

  // --- Replay actions (M4.8) -------------------------------------------------
  // Entering replay does NOT close the websocket: live ingestion keeps running
  // underneath so a one-click return-to-live is instant and never loses data. The
  // replay buffer is read-only and played client-side; no action here publishes,
  // mutates live state, or touches the engine — replay can't fire live alerts.

  enterReplay: async (req) => {
    const session = await createReplaySession(req); // throws → caller surfaces it
    const { startMs } = sessionBoundsMs(session);
    set((s) => ({
      replay: {
        ...s.replay,
        mode: "replay",
        session,
        cursorMs: startMs,
        playing: false,
      },
    }));
  },

  exitReplay: () => {
    const prior = get().replay.session;
    // Best-effort server teardown so the bounded registry reclaims the slot; a
    // failure is irrelevant to the client, which has already dropped its buffer.
    if (prior) void deleteReplaySession(prior.session_id).catch(() => {});
    set({ replay: emptyReplay() });
  },

  play: () =>
    set((s) =>
      s.replay.mode === "replay" && s.replay.session
        ? { replay: { ...s.replay, playing: true } }
        : {},
    ),

  pause: () => set((s) => ({ replay: { ...s.replay, playing: false } })),

  setSpeed: (speed) => set((s) => ({ replay: { ...s.replay, speed } })),

  step: (deltaMs) =>
    set((s) => {
      if (!s.replay.session) return {};
      const { startMs, endMs } = sessionBoundsMs(s.replay.session);
      const cursorMs = clampCursor(s.replay.cursorMs + deltaMs, startMs, endMs);
      return { replay: { ...s.replay, cursorMs } };
    }),

  seek: (tMs) =>
    set((s) => {
      if (!s.replay.session) return {};
      const { startMs, endMs } = sessionBoundsMs(s.replay.session);
      return { replay: { ...s.replay, cursorMs: clampCursor(tMs, startMs, endMs) } };
    }),

  tick: (advanceMs) =>
    set((s) => {
      if (!s.replay.playing || !s.replay.session) return {};
      const { startMs, endMs } = sessionBoundsMs(s.replay.session);
      const next = s.replay.cursorMs + advanceMs;
      if (next >= endMs) {
        // Hit the end of the window: park at the end and auto-pause.
        return { replay: { ...s.replay, cursorMs: endMs, playing: false } };
      }
      return { replay: { ...s.replay, cursorMs: clampCursor(next, startMs, endMs) } };
    }),
}));

/** A layer is visible unless explicitly toggled off (default-on). */
export function isLayerVisible(state: AppState, layer: string): boolean {
  return state.layerVisible[layer] !== false;
}

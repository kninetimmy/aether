// React-facing live-state store (Zustand). Wraps the framework-free LiveState +
// WsClient: a single client instance feeds snapshots/deltas in, components select
// slices out. Keeping the reducer pure (liveState.ts) means this layer is just
// glue — no business logic lives here.

import { create } from "zustand";
import { WsClient, type ConnectionStatus } from "../api/wsClient";
import { emptyState, type LiveState } from "./liveState";

export interface AppState {
  live: LiveState;
  connection: ConnectionStatus;
  /** Layer visibility toggles, keyed by presentation layer id. */
  layerVisible: Record<string, boolean>;
  client: WsClient | null;
  connect: (url?: string) => void;
  disconnect: () => void;
  setLayerVisible: (layer: string, visible: boolean) => void;
}

export const useStore = create<AppState>((set, get) => ({
  live: emptyState(),
  connection: "closed",
  layerVisible: {},
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
}));

/** A layer is visible unless explicitly toggled off (default-on). */
export function isLayerVisible(state: AppState, layer: string): boolean {
  return state.layerVisible[layer] !== false;
}

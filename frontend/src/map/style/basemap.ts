// Basemap style selection (PRD §24.1; config knob "Preferred basemap provider/style").
//
// Default: a hosted dark vector basemap (CARTO "Dark Matter") so the COP renders real
// geography (coastlines, borders, roads, labels) beneath the record overlays.
//
// This is a DELIBERATE, operator-authorized relaxation of the "no external tile fetch /
// fully self-contained" default (CLAUDE.md §5 / PRD §6): with a hosted basemap the
// BROWSER fetches tiles from the provider, which exposes the operator's current viewport
// to that provider. It is kept honest by three properties:
//   - key-free: no secret/API key is committed to the repo (CLAUDE.md §5).
//   - operator-overridable: set VITE_AETHER_BASEMAP to a different style-JSON URL, or to
//     "offline" to force the fully self-contained dark canvas (zero network).
//   - graceful: if the hosted style can't load, MapView falls back to OFFLINE_DARK_STYLE
//     so a blocked/absent network degrades visibly instead of breaking (PRD §37).

import type { StyleSpecification } from "maplibre-gl";
import { darkStyle } from "./darkStyle";

/** Fully self-contained dark canvas — no external fetch. The graceful-degradation target. */
export const OFFLINE_DARK_STYLE: StyleSpecification = darkStyle;

// No API key required. Attribution: © OpenStreetMap contributors © CARTO.
const DEFAULT_HOSTED_STYLE =
  "https://basemaps.cartocdn.com/gl/dark-matter-gl-style/style.json";

/** Shown via the attribution control whenever a hosted basemap is active (provider terms). */
export const BASEMAP_ATTRIBUTION =
  '© <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noreferrer">OpenStreetMap</a> contributors © <a href="https://carto.com/attributions" target="_blank" rel="noreferrer">CARTO</a>';

// VITE_AETHER_BASEMAP: "offline" forces the self-contained canvas; any other non-empty
// value is treated as a replacement style-JSON URL (e.g. OpenFreeMap:
// https://tiles.openfreemap.org/styles/dark). Empty/unset → the default hosted style.
const override = (
  import.meta as unknown as { env?: Record<string, string | undefined> }
).env?.["VITE_AETHER_BASEMAP"]?.trim();

/** False only when the operator forced the offline canvas — drives attribution + fallback wiring. */
export const usingHostedBasemap = override !== "offline";

/** The style passed to `new maplibregl.Map({ style })`: a URL (hosted) or the offline object. */
export const basemapStyle: string | StyleSpecification =
  override === "offline"
    ? OFFLINE_DARK_STYLE
    : override && override.length > 0
      ? override
      : DEFAULT_HOSTED_STYLE;

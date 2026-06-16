// Minimal dark MapLibre style with NO external tile fetch (PRD §24.1 dark/
// tactical; local-first per §6/§33). The shell renders our own GeoJSON record
// layers on top of a flat dark background. A real basemap (local vector tiles /
// protomaps) is a later milestone — kept out here so the COP stays fully
// self-contained and private by default.

import type { StyleSpecification } from "maplibre-gl";

export const DARK_BACKGROUND = "#0a0e14";

export const darkStyle: StyleSpecification = {
  version: 8,
  // No glyphs/sprite URLs: we render circles, not text/sprites, in the shell.
  sources: {},
  layers: [
    {
      id: "background",
      type: "background",
      paint: { "background-color": DARK_BACKGROUND },
    },
  ],
};

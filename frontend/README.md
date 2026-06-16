# aether frontend (M1.3 — COP shell)

React + Vite + TypeScript + MapLibre GL. Renders the schema-v2 record union
(tracks / geo-features / events / alerts / source-status) live from the backend
`/ws/v2` websocket (PRD §22), through a centralized presentation registry
(PRD §5, §24, §37).

## Layout (PRD §30)

```
src/
  api/wsClient.ts            typed /ws/v2 client: snapshot + deltas, seq-gap
                             detection, resync-on-gap, reconnect backoff
  state/liveState.ts         framework-free authoritative state + pure reducer
  state/store.ts             Zustand store wiring the client into React
  map/presentationRegistry.ts  the ONLY source-/kind-specific styling
  map/layers/recordLayers.ts   live state -> GeoJSON feature collections
  map/style/darkStyle.ts       blank dark MapLibre style (no external tiles)
  features/map|sources|tracks/ panels: map, layer control, source health,
                             track list, event/alert feed
  app/App.tsx                collapsible shell (header / left / map / right / bottom)
  types/records.ts           TS mirror of src/aether/schema/records.py
```

## Develop

```bash
cd frontend
npm install
npm run dev          # http://127.0.0.1:5173  (proxies /api + /ws to :8000)
```

Run the backend demo source in another terminal (repo root):

```bash
docker compose up -d
uvicorn aether.backend.main:app --host 127.0.0.1 --port 8000
```

The shell connects on load, draws the demo tracks/features/alerts on the map,
and shows source health + counts. A sequence gap flips the header to **STALE**
and the client reconnects to pull a fresh snapshot.

## Verify

```bash
npm run typecheck    # tsc -b --noEmit
npm run lint         # eslint
npm test             # vitest: reducer seq/resync + presentation registry
npm run build        # tsc -b && vite build
```

## Notes

- **Local-first:** the shell fetches no external map tiles (blank dark basemap).
  A real local/vector basemap is a later milestone.
- **No secrets, callsign, or coordinates** in this tree (PRD §5). The initial
  map view is a generic CONUS extent, not the station.

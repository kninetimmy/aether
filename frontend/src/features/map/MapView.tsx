// MapLibre map view (PRD §24.3). Initializes one map with the dark style, keeps
// two GeoJSON sources (tracks + features) in sync with live state, and applies
// layer-visibility toggles. Circle/area layers only for the shell — heading
// rotation and sprite symbols arrive with a real basemap in a later milestone.

import maplibregl, { type Map as MlMap } from "maplibre-gl";
import { useEffect, useMemo, useRef } from "react";
import {
  featureFeatureCollection,
  trackFeatureCollection,
} from "../../map/layers/recordLayers";
import { darkStyle } from "../../map/style/darkStyle";
import { visibleTracks } from "../../state/selectors";
import { isLayerVisible, useStore } from "../../state/store";

const TRACK_SOURCE = "aether-tracks";
const FEATURE_SOURCE = "aether-features";

// Watchlist membership feeds the watchlistOnly filter; the watchlist slice lands
// in M3.6c, so until then the chokepoint receives an empty (stable) set.
const EMPTY_WATCHLIST: Set<string> = new Set();

// Default view: continental US-ish. No station coordinates baked in (PRD §5).
const INITIAL_CENTER: [number, number] = [-98.5, 39.8];
const INITIAL_ZOOM = 3.2;

export function MapView() {
  const containerRef = useRef<HTMLDivElement>(null);
  const mapRef = useRef<MlMap | null>(null);
  const readyRef = useRef(false);

  // Select the sub-collections, not the whole live object: the reducer keeps a
  // stable Map reference for anything that didn't change in a frame, so these
  // memos only rebuild when tracks/features actually move — not on every alert,
  // event, or source-status tick.
  const tracks = useStore((s) => s.live.tracks);
  const features = useStore((s) => s.live.features);
  const layerVisible = useStore((s) => s.layerVisible);
  const filters = useStore((s) => s.filters);
  const stationCenter = useStore((s) => s.stationCenter);
  const clock = useStore((s) => s.clock);

  // The display filters (provenance, live-LOCAL, range, age, AIS, …) are applied
  // through the single visibleTracks chokepoint, so a filtered-out track leaves
  // the GeoJSON source entirely — it vanishes from the map exactly as it does
  // from the list (PRD §16.5). Display only; never changes ingestion.
  const trackFc = useMemo(
    () =>
      trackFeatureCollection(
        visibleTracks(tracks, filters, {
          now: clock,
          stationCenter,
          watchlist: EMPTY_WATCHLIST,
        }),
      ),
    [tracks, filters, stationCenter, clock],
  );
  const featureFc = useMemo(() => featureFeatureCollection(features), [features]);

  // Initialize the map once.
  useEffect(() => {
    if (!containerRef.current || mapRef.current) return;
    const map = new maplibregl.Map({
      container: containerRef.current,
      style: darkStyle,
      center: INITIAL_CENTER,
      zoom: INITIAL_ZOOM,
      attributionControl: false,
    });
    mapRef.current = map;

    map.on("load", () => {
      // Bail if the component unmounted before the style finished loading —
      // the cleanup has already removed this map, so touching it would throw.
      if (mapRef.current !== map) return;
      map.addSource(TRACK_SOURCE, { type: "geojson", data: emptyFc() });
      map.addSource(FEATURE_SOURCE, { type: "geojson", data: emptyFc() });

      // Geo-feature areas underneath tracks.
      map.addLayer({
        id: "features-fill",
        type: "fill",
        source: FEATURE_SOURCE,
        filter: ["match", ["geometry-type"], ["Polygon", "MultiPolygon"], true, false],
        paint: { "fill-color": ["get", "color"], "fill-opacity": 0.18 },
      });
      map.addLayer({
        id: "features-outline",
        type: "line",
        source: FEATURE_SOURCE,
        filter: ["match", ["geometry-type"], ["Polygon", "MultiPolygon"], true, false],
        paint: { "line-color": ["get", "color"], "line-width": 1.5 },
      });
      map.addLayer({
        id: "features-point",
        type: "circle",
        source: FEATURE_SOURCE,
        filter: ["==", ["geometry-type"], "Point"],
        paint: {
          "circle-radius": 4,
          "circle-color": ["get", "color"],
          "circle-opacity": 0.85,
        },
      });

      // Tracks: outline encodes provenance — solid for local RF, dashed-feel
      // (lighter stroke) for network/predicted. Color is not the only channel.
      map.addLayer({
        id: "tracks-point",
        type: "circle",
        source: TRACK_SOURCE,
        paint: {
          "circle-radius": 5,
          "circle-color": ["get", "color"],
          "circle-stroke-color": [
            "case",
            ["get", "locallyReceived"],
            "#ffffff",
            "#33414f",
          ],
          "circle-stroke-width": ["case", ["get", "locallyReceived"], 2, 1],
          "circle-opacity": ["case", ["get", "predicted"], 0.5, 0.95],
        },
      });

      readyRef.current = true;
      pushData();
    });

    return () => {
      map.remove();
      mapRef.current = null;
      readyRef.current = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Push fresh GeoJSON whenever live state changes.
  function pushData() {
    const map = mapRef.current;
    if (!map || !readyRef.current) return;
    (map.getSource(TRACK_SOURCE) as maplibregl.GeoJSONSource | undefined)?.setData(
      trackFc as never,
    );
    (map.getSource(FEATURE_SOURCE) as maplibregl.GeoJSONSource | undefined)?.setData(
      featureFc as never,
    );
  }

  useEffect(() => {
    pushData();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [trackFc, featureFc]);

  // Apply layer-visibility toggles via per-feature filters.
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !readyRef.current) return;
    const state = useStore.getState();
    const hidden = Object.keys(layerVisible).filter(
      (l) => !isLayerVisible(state, l),
    );
    const trackFilter =
      hidden.length === 0
        ? null
        : (["!", ["in", ["get", "layer"], ["literal", hidden]]] as never);
    for (const id of ["tracks-point"]) {
      if (map.getLayer(id)) map.setFilter(id, trackFilter);
    }
  }, [layerVisible]);

  return <div ref={containerRef} className="map-container" />;
}

function emptyFc() {
  return { type: "FeatureCollection", features: [] } as never;
}

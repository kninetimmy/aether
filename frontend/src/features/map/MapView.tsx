// MapLibre map view (PRD §24.3). Initializes one map with the dark style, keeps
// two GeoJSON sources (tracks + features) in sync with live state, and applies
// layer-visibility toggles. Circle/area layers only for the shell — heading
// rotation and sprite symbols arrive with a real basemap in a later milestone.

import maplibregl, { type Map as MlMap } from "maplibre-gl";
import { useCallback, useEffect, useMemo, useRef } from "react";
import {
  featureFeatureCollection,
  trackFeatureCollection,
} from "../../map/layers/recordLayers";
import { darkStyle } from "../../map/style/darkStyle";
import { toiHighlight } from "../../map/presentationRegistry";
import { visibleTracks } from "../../state/selectors";
import { isLayerVisible, useStore } from "../../state/store";

const TRACK_SOURCE = "aether-tracks";
const FEATURE_SOURCE = "aether-features";

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
  const watchlist = useStore((s) => s.watchlist);
  const selectTrack = useStore((s) => s.selectTrack);
  const client = useStore((s) => s.client);

  // The server-side display-stream subscription (M3.6b): the viewport bbox plus
  // the source/track-type display filters become a debounced `subscribe` frame so
  // the backend trims the snapshot+delta firehose per-connection (PRD §16.3,
  // §22.2). The bbox is the map's current bounds; sources/track_types come from
  // the SAME DisplayFilters the client-side chokepoint reads, so server and client
  // filtering agree. include_events/alerts stay on (no UI toggle yet).
  const sendSubscribe = useCallback(() => {
    const map = mapRef.current;
    if (!map || !client) return;
    const b = map.getBounds();
    const bbox: [number, number, number, number] = [
      b.getWest(),
      b.getSouth(),
      b.getEast(),
      b.getNorth(),
    ];
    client.subscribe({
      type: "subscribe",
      bbox,
      sources: filters.sources ? [...filters.sources] : null,
      track_types: filters.trackTypes ? [...filters.trackTypes] : null,
      include_events: true,
      include_alerts: true,
    });
  }, [client, filters.sources, filters.trackTypes]);

  // The map's moveend (registered once) must always call the FRESHEST subscribe
  // closure, so route it through a ref rather than re-binding listeners.
  const sendSubscribeRef = useRef(sendSubscribe);
  sendSubscribeRef.current = sendSubscribe;

  // Re-subscribe whenever the source/track-type filters or the client change (the
  // viewport path fires from moveend below). Debounced inside WsClient.
  useEffect(() => {
    sendSubscribe();
  }, [sendSubscribe]);

  // The display filters (provenance, live-LOCAL, range, age, AIS, …) are applied
  // through the single visibleTracks chokepoint, so a filtered-out track leaves
  // the GeoJSON source entirely — it vanishes from the map exactly as it does
  // from the list (PRD §16.5). Display only; never changes ingestion. The TOI
  // highlight ring reads the SAME already-filtered features (filtering on isToi),
  // so a TOI hidden by any filter cannot reappear as a highlight.
  const trackFc = useMemo(
    () =>
      trackFeatureCollection(
        visibleTracks(tracks, filters, {
          now: clock,
          stationCenter,
          watchlist,
        }),
        watchlist,
      ),
    [tracks, filters, stationCenter, clock, watchlist],
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

      // TOI highlight ring — ordered ABOVE tracks-point, reads the SAME (already
      // filtered) track source and only renders the isToi members, so a TOI
      // hidden by a layer/provenance/display filter has no feature and cannot
      // reappear. Styling comes from the centralized presentation registry.
      const toi = toiHighlight();
      map.addLayer({
        id: "tracks-highlight",
        type: "circle",
        source: TRACK_SOURCE,
        filter: ["==", ["get", "isToi"], true],
        paint: {
          "circle-radius": toi.radius,
          "circle-color": "rgba(0,0,0,0)",
          "circle-stroke-color": toi.color,
          "circle-stroke-width": toi.width,
        },
      });

      // Click a track point → select it for the TOI details panel.
      map.on("click", "tracks-point", (e) => {
        const f = e.features?.[0];
        const id = f?.properties?.["id"];
        if (typeof id === "string") selectTrack(id);
      });
      map.on("mouseenter", "tracks-point", () => {
        map.getCanvas().style.cursor = "pointer";
      });
      map.on("mouseleave", "tracks-point", () => {
        map.getCanvas().style.cursor = "";
      });

      // Viewport change → debounced server re-subscribe (M3.6b). Routed through a
      // ref so this once-registered handler always uses the freshest filters.
      map.on("moveend", () => sendSubscribeRef.current());

      readyRef.current = true;
      pushData();
      // Initial subscribe now that bounds exist (also re-sent on socket open).
      sendSubscribeRef.current();
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
    const layerHidden =
      hidden.length === 0
        ? null
        : (["!", ["in", ["get", "layer"], ["literal", hidden]]] as const);
    // tracks-point: hide whole layers toggled off.
    if (map.getLayer("tracks-point")) {
      map.setFilter("tracks-point", (layerHidden as never) ?? null);
    }
    // tracks-highlight must keep its isToi gate AND honor the SAME layer-visibility
    // filter, so a TOI on a hidden layer shows no ring (it can't reappear).
    if (map.getLayer("tracks-highlight")) {
      const isToi = ["==", ["get", "isToi"], true] as const;
      const combined = layerHidden ? ["all", isToi, layerHidden] : isToi;
      map.setFilter("tracks-highlight", combined as never);
    }
  }, [layerVisible]);

  return <div ref={containerRef} className="map-container" />;
}

function emptyFc() {
  return { type: "FeatureCollection", features: [] } as never;
}

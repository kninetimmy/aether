// MapLibre map view (PRD §24.3). Initializes one map with the configured basemap
// (a hosted dark vector style by default, with an offline fallback — see
// basemap.ts), keeps two GeoJSON sources (tracks + features) in sync with live
// state, and applies layer-visibility toggles. Our record overlays render ABOVE
// the basemap. Circle/area layers only for now — heading rotation and sprite
// symbols are a later refinement.

import maplibregl, { type Map as MlMap } from "maplibre-gl";
import { useCallback, useEffect, useMemo, useRef } from "react";
import {
  featureFeatureCollection,
  lightningFeatureCollection,
  trackFeatureCollection,
} from "../../map/layers/recordLayers";
import {
  BASEMAP_ATTRIBUTION,
  OFFLINE_DARK_STYLE,
  basemapStyle,
  usingHostedBasemap,
} from "../../map/style/basemap";
import { lightningStyle, toiHighlight } from "../../map/presentationRegistry";
import { visibleTracks } from "../../state/selectors";
import { replayVisibleRecords } from "../../state/replay";
import { isLayerVisible, useStore } from "../../state/store";
import type {
  GeoFeatureRecord,
  GeoJSONPoint,
  TrackRecord,
} from "../../types/records";

const TRACK_SOURCE = "aether-tracks";
const FEATURE_SOURCE = "aether-features";
// Clustered lightning lives in its own source (clustering is a per-source flag;
// the shared feature source also carries polygons). LIGHTNING-FR-006 / PRD §24.3.
const LIGHTNING_SOURCE = "aether-lightning";
// The lightning layers all derive from the one presentation layer key, so the
// existing "Lightning" toggle in LayerControl gates every one of them at once.
const LIGHTNING_LAYER_KEY = "features-lightning";
const LIGHTNING_LAYER_IDS = [
  "lightning-clusters",
  "lightning-cluster-count",
  "lightning-flash",
] as const;

// Build a MapLibre `step` expression from a {base, steps:[stop,output]} ramp:
// step(input, base, stop0, out0, stop1, out1, …). Lets the centralized lightning
// style own the numbers while the map owns the expression shape.
function stepExpr(
  input: maplibregl.ExpressionSpecification,
  ramp: { base: number | string; steps: [number, number | string][] },
): maplibregl.ExpressionSpecification {
  return [
    "step",
    input,
    ramp.base,
    ...ramp.steps.flatMap(([stop, out]) => [stop, out]),
  ] as unknown as maplibregl.ExpressionSpecification;
}

/** Stable empty feature map for replay (track-only persistence hides live overlays). */
const EMPTY_FEATURES = new Map<string, GeoFeatureRecord>();

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

  // Replay slice (M4.8): when mode==='replay' the map renders the REPLAYED snapshot
  // at the cursor instead of the live set. Live data keeps flowing into the store
  // underneath — we just stop SHOWING it — so return-to-live is instant and replay
  // never disturbs ingestion (PRD §19.6 invariant).
  const replay = useStore((s) => s.replay);
  const inReplay = replay.mode === "replay";

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
  // In REPLAY mode, derive the on-map record set from the session buffer at the cursor
  // (visible-at-T) and feed it through the SAME presentation path the live set uses — so
  // a replayed aircraft/vessel looks exactly like its live counterpart, only frozen at
  // time T. The display filters still apply to tracks (so a provenance/range/type filter
  // narrows replay just as it does live). M4 persists ONLY track observations, so a
  // replay buffer is track-only: overlay features (geofences/TFRs) are not historical
  // and are hidden in replay rather than mixing live overlays into a past snapshot.
  const replayTracks = useMemo(() => {
    const m = new Map<string, TrackRecord>();
    if (!inReplay) return m;
    for (const r of replayVisibleRecords(replay.session, replay.cursorMs)) {
      if (r.kind === "track") m.set(r.id, r);
    }
    return m;
  }, [inReplay, replay.session, replay.cursorMs]);

  const trackFc = useMemo(() => {
    const src = inReplay ? replayTracks : tracks;
    return trackFeatureCollection(
      visibleTracks(src, filters, { now: clock, stationCenter, watchlist }),
      watchlist,
    );
  }, [inReplay, replayTracks, tracks, filters, stationCenter, clock, watchlist]);
  const featureFc = useMemo(
    // Replay has no historical features to show (track-only persistence, M4), so the
    // overlay source is emptied rather than showing LIVE features over a past snapshot.
    () => featureFeatureCollection(inReplay ? EMPTY_FEATURES : features),
    [inReplay, features],
  );
  // Lightning is split into its own clustered source (LIGHTNING-FR-006). Like the
  // generic feature source it carries no historical data, so replay shows none.
  const lightningFc = useMemo(
    () => lightningFeatureCollection(inReplay ? EMPTY_FEATURES : features),
    [inReplay, features],
  );

  // Initialize the map once.
  useEffect(() => {
    if (!containerRef.current || mapRef.current) return;
    const map = new maplibregl.Map({
      container: containerRef.current,
      style: basemapStyle,
      center: INITIAL_CENTER,
      zoom: INITIAL_ZOOM,
      // Hosted basemaps must show provider attribution (CARTO/OSM terms); the
      // offline canvas has no external data to attribute.
      attributionControl: usingHostedBasemap
        ? { compact: true, customAttribution: BASEMAP_ATTRIBUTION }
        : false,
    });
    mapRef.current = map;

    // Add our record sources + layers ABOVE the basemap. Idempotent (every add is
    // existence-guarded) so it runs both on the initial style load AND again after
    // a fallback setStyle(), which discards every source/layer the prior style held.
    const installOverlay = () => {
      if (mapRef.current !== map) return;
      if (!map.getSource(TRACK_SOURCE))
        map.addSource(TRACK_SOURCE, { type: "geojson", data: emptyFc() });
      if (!map.getSource(FEATURE_SOURCE))
        map.addSource(FEATURE_SOURCE, { type: "geojson", data: emptyFc() });
      // Lightning gets a clustered source: GLM emits one point per flash, which
      // is illegible at low zoom during a storm (LIGHTNING-FR-006). MapLibre
      // aggregates nearby flashes into a single bubble below clusterMaxZoom and
      // splits them back into individual flashes above it.
      if (!map.getSource(LIGHTNING_SOURCE))
        map.addSource(LIGHTNING_SOURCE, {
          type: "geojson",
          data: emptyFc(),
          cluster: true,
          clusterRadius: 50,
          clusterMaxZoom: 9,
        });

      // Geo-feature areas underneath tracks.
      if (!map.getLayer("features-fill"))
        map.addLayer({
          id: "features-fill",
          type: "fill",
          source: FEATURE_SOURCE,
          filter: ["match", ["geometry-type"], ["Polygon", "MultiPolygon"], true, false],
          paint: { "fill-color": ["get", "color"], "fill-opacity": 0.18 },
        });
      if (!map.getLayer("features-outline"))
        map.addLayer({
          id: "features-outline",
          type: "line",
          source: FEATURE_SOURCE,
          filter: ["match", ["geometry-type"], ["Polygon", "MultiPolygon"], true, false],
          paint: { "line-color": ["get", "color"], "line-width": 1.5 },
        });
      if (!map.getLayer("features-point"))
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

      // Lightning (clustered). Three layers off the one clustered source: cluster
      // bubbles (only features with `point_count`), the count label on each bubble,
      // and individual unclustered flashes. Bigger+hotter bubble AND a printed
      // count both encode density, so color is never the only channel (§24.9). All
      // sizes/colors come from the centralized lightning style.
      const ls = lightningStyle();
      if (!map.getLayer("lightning-clusters"))
        map.addLayer({
          id: "lightning-clusters",
          type: "circle",
          source: LIGHTNING_SOURCE,
          filter: ["has", "point_count"],
          paint: {
            "circle-color": stepExpr(["get", "point_count"], ls.clusterColor),
            "circle-radius": stepExpr(["get", "point_count"], ls.clusterRadius),
            "circle-opacity": 0.85,
            "circle-stroke-color": "#1a1205",
            "circle-stroke-width": 1,
          },
        });
      if (!map.getLayer("lightning-cluster-count"))
        map.addLayer({
          id: "lightning-cluster-count",
          type: "symbol",
          source: LIGHTNING_SOURCE,
          filter: ["has", "point_count"],
          layout: {
            "text-field": ["get", "point_count_abbreviated"],
            "text-size": 11,
            "text-allow-overlap": true,
          },
          paint: { "text-color": ls.countColor },
        });
      if (!map.getLayer("lightning-flash"))
        map.addLayer({
          id: "lightning-flash",
          type: "circle",
          source: LIGHTNING_SOURCE,
          filter: ["!", ["has", "point_count"]],
          paint: {
            "circle-radius": ls.flashRadius,
            "circle-color": ls.flashColor,
            "circle-opacity": 0.9,
          },
        });

      // Tracks: outline encodes provenance — solid for local RF, dashed-feel
      // (lighter stroke) for network/predicted. Color is not the only channel.
      if (!map.getLayer("tracks-point"))
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
      if (!map.getLayer("tracks-highlight")) {
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
      }
    };

    // Map/layer event handlers — bound ONCE here (not inside the style-load
    // callback): they key off layer ids by string, tolerate being registered
    // before the layer exists, and survive a fallback setStyle().
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
    // Click a lightning cluster to zoom to the level where it breaks apart — the
    // standard cluster drill-down. Best-effort: any query/source miss is a no-op.
    map.on("click", "lightning-clusters", (e) => {
      const f = e.features?.[0];
      const clusterId = f?.properties?.["cluster_id"];
      const src = map.getSource(LIGHTNING_SOURCE) as
        | maplibregl.GeoJSONSource
        | undefined;
      if (typeof clusterId !== "number" || !src) return;
      void src
        .getClusterExpansionZoom(clusterId)
        .then((zoom) => {
          const coords = (f?.geometry as GeoJSONPoint | undefined)?.coordinates;
          if (coords) map.easeTo({ center: [coords[0], coords[1]], zoom });
        })
        .catch(() => {});
    });
    map.on("mouseenter", "lightning-clusters", () => {
      map.getCanvas().style.cursor = "pointer";
    });
    map.on("mouseleave", "lightning-clusters", () => {
      map.getCanvas().style.cursor = "";
    });
    // Viewport change → debounced server re-subscribe (M3.6b). Routed through a
    // ref so this once-registered handler always uses the freshest filters.
    map.on("moveend", () => sendSubscribeRef.current());

    // Bring the overlay + live data online once a style (basemap or fallback) is
    // ready. Idempotent, so the initial load and the fallback path share it.
    const activate = () => {
      if (mapRef.current !== map) return;
      installOverlay();
      readyRef.current = true;
      pushData();
      // Initial subscribe now that bounds exist (also re-sent on socket open).
      sendSubscribeRef.current();
    };

    map.on("load", activate);

    // Graceful degradation (PRD §37): a hosted basemap style that can't load
    // (network blocked/offline) never fires `load`. After a grace period with no
    // load, drop to the self-contained offline canvas and re-activate on its
    // styledata. A style OBJECT loads without a network round-trip, so this always
    // resolves. Transient single-tile errors are ignored (the style still loaded).
    let fellBack = false;
    const fallbackTimer = usingHostedBasemap
      ? window.setTimeout(() => {
          if (fellBack || mapRef.current !== map || readyRef.current) return;
          fellBack = true;
          // eslint-disable-next-line no-console
          console.warn(
            "aether: hosted basemap unavailable; falling back to offline dark canvas",
          );
          map.setStyle(OFFLINE_DARK_STYLE);
          map.once("styledata", activate);
        }, 8000)
      : undefined;

    return () => {
      if (fallbackTimer !== undefined) window.clearTimeout(fallbackTimer);
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
    (
      map.getSource(LIGHTNING_SOURCE) as maplibregl.GeoJSONSource | undefined
    )?.setData(lightningFc as never);
  }

  useEffect(() => {
    pushData();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [trackFc, featureFc, lightningFc]);

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
    // The lightning layers come from a separate clustered source, so a per-feature
    // `layer`-property filter can't reach them (clusters MapLibre synthesizes carry
    // no `layer` prop). Gate all three by layout visibility off the same
    // `features-lightning` toggle the LayerControl drives — so hiding "Lightning"
    // clears the flashes AND the cluster bubbles together.
    const lightningVisible = isLayerVisible(state, LIGHTNING_LAYER_KEY);
    for (const id of LIGHTNING_LAYER_IDS) {
      if (map.getLayer(id))
        map.setLayoutProperty(id, "visibility", lightningVisible ? "visible" : "none");
    }
  }, [layerVisible]);

  return <div ref={containerRef} className="map-container" />;
}

function emptyFc() {
  return { type: "FeatureCollection", features: [] } as never;
}

// App shell (PRD §24.2): header bar, left layers/filters panel, center map,
// right selection/source panel, bottom timeline/feed. Panels are collapsible.
// Connects the websocket on mount and tears it down on unmount.

import { useEffect, useState } from "react";
import { MapView } from "../features/map/MapView";
import { EventFeed } from "../features/map/EventFeed";
import { FilterPanel } from "../features/filters/FilterPanel";
import { AlertsPanel } from "../features/alerts/AlertsPanel";
import { AlertRuleEditor } from "../features/alerts/AlertRuleEditor";
import { LayerControl } from "../features/sources/LayerControl";
import { SourceHealthPanel } from "../features/sources/SourceHealthPanel";
import { TOIDetailsPanel } from "../features/toi/TOIDetailsPanel";
import { TrackList } from "../features/tracks/TrackList";
import { ReplayLauncher } from "../features/replay/ReplayLauncher";
import { ReplayTimeline } from "../features/replay/ReplayTimeline";
import { useStore, orbitalConfigFromApi, type OrbitalConfigApi } from "../state/store";

export function App() {
  const connect = useStore((s) => s.connect);
  const disconnect = useStore((s) => s.disconnect);
  const tickClock = useStore((s) => s.tickClock);
  const setStationCenter = useStore((s) => s.setStationCenter);
  const setOrbitalConfig = useStore((s) => s.setOrbitalConfig);
  const hydrateWatchlist = useStore((s) => s.hydrateWatchlist);
  const ageMaxS = useStore((s) => s.filters.ageMaxS);
  const stale = useStore((s) => s.live.stale);
  const seq = useStore((s) => s.live.seq);
  const replayMode = useStore((s) => s.replay.mode);
  const replayPlaying = useStore((s) => s.replay.playing);
  const replaySpeed = useStore((s) => s.replay.speed);
  const tick = useStore((s) => s.tick);

  const [leftOpen, setLeftOpen] = useState(true);
  const [rightOpen, setRightOpen] = useState(true);
  const [bottomOpen, setBottomOpen] = useState(true);

  const inReplay = replayMode === "replay";

  useEffect(() => {
    connect();
    return () => disconnect();
  }, [connect, disconnect]);

  // Pull the runtime station center (PRD §5: coordinates are never committed) so
  // the M3.6a range-from-station filter has an origin. A 0,0 / unconfigured station
  // comes back as null → the range control stays a disabled no-op.
  useEffect(() => {
    let cancelled = false;
    fetch("/api/config")
      .then((r) => (r.ok ? r.json() : null))
      .then(
        (
          cfg:
            | {
                station: { lon: number; lat: number } | null;
                orbital?: OrbitalConfigApi;
              }
            | null,
        ) => {
          if (cancelled || !cfg) return;
          setStationCenter(
            cfg.station ? { lon: cfg.station.lon, lat: cfg.station.lat } : null,
          );
          // The orbital block is always present per the /api/config contract, but
          // tolerate an older backend (block absent) by leaving the controls off.
          setOrbitalConfig(orbitalConfigFromApi(cfg.orbital));
        },
      )
      .catch(() => {
        // A missing/failed config endpoint must not break the app: leave the
        // range filter disabled (PRD §37 graceful degradation).
      });
    return () => {
      cancelled = true;
    };
  }, [setStationCenter, setOrbitalConfig]);

  // Reconcile the watchlist with the backend-authoritative store once on mount
  // (M6.6b, PRD §21.5). The store starts from the localStorage cache for instant
  // paint; this replaces it with server state. A failure keeps the cache (PRD §37).
  useEffect(() => {
    void hydrateWatchlist();
  }, [hydrateWatchlist]);

  // Drive a 1s store clock tick ONLY while the age filter is active, so the
  // now−observed_at cutoff doesn't silently drift between WS frames. The
  // live-LOCAL filter keys off server-computed contributor freshness frozen into
  // the record, so the client clock can't change its answer — no tick needed for
  // it, and idle (no age filter) avoids a pointless 1Hz re-filter + map rebuild.
  useEffect(() => {
    if (ageMaxS === null) return;
    const id = setInterval(() => tickClock(), 1000);
    return () => clearInterval(id);
  }, [tickClock, ageMaxS]);

  // Drive replay playback: while playing, advance the cursor every TICK_MS by
  // TICK_MS×speed of REPLAY time, so 1× tracks wall-clock and 10× is ten times
  // faster. The store's tick() auto-pauses at the window end. Mirrors the tickClock
  // effect; the interval is torn down whenever playback stops or mode leaves replay.
  // Replay time only — nothing here touches live ingestion (PRD §19.6 invariant).
  useEffect(() => {
    if (!inReplay || !replayPlaying) return;
    const TICK_MS = 250;
    const id = setInterval(() => tick(TICK_MS * replaySpeed), TICK_MS);
    return () => clearInterval(id);
  }, [inReplay, replayPlaying, replaySpeed, tick]);

  return (
    <div className={`app${inReplay ? " replaying" : ""}`}>
      <header className="topbar">
        <span className="brand">aether</span>
        {/* Mode pill: LIVE vs a VISUALLY DISTINCT amber REPLAY pill so the two are
            never confusable (HISTORY-FR-006). The amber .replaying app class also
            tints the map border, doubling the cue beyond color alone (§24.9). */}
        <span className={`mode${inReplay ? " replay" : ""}`}>
          {inReplay ? "REPLAY" : "LIVE"}
        </span>
        <ReplayLauncher />
        {inReplay && (
          <span className="replay-banner" role="status">
            REPLAY — historical data, not live
          </span>
        )}
        {!inReplay && stale && (
          <span className="stale-banner">STALE — resyncing…</span>
        )}
        <span className="seq" title="last applied sequence">
          seq {seq}
        </span>
      </header>

      <div className="body">
        <aside className={`left ${leftOpen ? "" : "collapsed"}`}>
          <button className="collapse" onClick={() => setLeftOpen((v) => !v)}>
            {leftOpen ? "‹" : "›"}
          </button>
          {leftOpen && (
            <div className="panel-scroll">
              <LayerControl />
              <FilterPanel />
              <AlertRuleEditor />
            </div>
          )}
        </aside>

        <main className="center">
          <MapView />
        </main>

        <aside className={`right ${rightOpen ? "" : "collapsed"}`}>
          <button className="collapse" onClick={() => setRightOpen((v) => !v)}>
            {rightOpen ? "›" : "‹"}
          </button>
          {rightOpen && (
            <div className="panel-scroll">
              <TOIDetailsPanel />
              <AlertsPanel />
              <SourceHealthPanel />
              <TrackList />
            </div>
          )}
        </aside>
      </div>

      <footer className={`bottom ${bottomOpen ? "" : "collapsed"}`}>
        <button className="collapse" onClick={() => setBottomOpen((v) => !v)}>
          {bottomOpen ? "▾" : "▴"}
        </button>
        {bottomOpen && inReplay && <ReplayTimeline />}
        {bottomOpen && !inReplay && <EventFeed />}
      </footer>
    </div>
  );
}

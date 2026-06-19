// App shell (PRD §24.2): header bar, left layers/filters panel, center map,
// right selection/source panel, bottom timeline/feed. Panels are collapsible.
// Connects the websocket on mount and tears it down on unmount.

import { useEffect, useState } from "react";
import { MapView } from "../features/map/MapView";
import { EventFeed } from "../features/map/EventFeed";
import { FilterPanel } from "../features/filters/FilterPanel";
import { LayerControl } from "../features/sources/LayerControl";
import { SourceHealthPanel } from "../features/sources/SourceHealthPanel";
import { TrackList } from "../features/tracks/TrackList";
import { useStore } from "../state/store";

export function App() {
  const connect = useStore((s) => s.connect);
  const disconnect = useStore((s) => s.disconnect);
  const tickClock = useStore((s) => s.tickClock);
  const ageMaxS = useStore((s) => s.filters.ageMaxS);
  const stale = useStore((s) => s.live.stale);
  const seq = useStore((s) => s.live.seq);

  const [leftOpen, setLeftOpen] = useState(true);
  const [rightOpen, setRightOpen] = useState(true);
  const [bottomOpen, setBottomOpen] = useState(true);

  useEffect(() => {
    connect();
    return () => disconnect();
  }, [connect, disconnect]);

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

  return (
    <div className="app">
      <header className="topbar">
        <span className="brand">aether</span>
        <span className="mode">LIVE</span>
        {stale && <span className="stale-banner">STALE — resyncing…</span>}
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
        {bottomOpen && <EventFeed />}
      </footer>
    </div>
  );
}

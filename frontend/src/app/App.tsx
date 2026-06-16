// App shell (PRD §24.2): header bar, left layers/filters panel, center map,
// right selection/source panel, bottom timeline/feed. Panels are collapsible.
// Connects the websocket on mount and tears it down on unmount.

import { useEffect, useState } from "react";
import { MapView } from "../features/map/MapView";
import { EventFeed } from "../features/map/EventFeed";
import { LayerControl } from "../features/sources/LayerControl";
import { SourceHealthPanel } from "../features/sources/SourceHealthPanel";
import { TrackList } from "../features/tracks/TrackList";
import { useStore } from "../state/store";

export function App() {
  const connect = useStore((s) => s.connect);
  const disconnect = useStore((s) => s.disconnect);
  const stale = useStore((s) => s.live.stale);
  const seq = useStore((s) => s.live.seq);

  const [leftOpen, setLeftOpen] = useState(true);
  const [rightOpen, setRightOpen] = useState(true);
  const [bottomOpen, setBottomOpen] = useState(true);

  useEffect(() => {
    connect();
    return () => disconnect();
  }, [connect, disconnect]);

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

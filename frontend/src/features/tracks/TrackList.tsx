// Track list (condensed form of PRD §24.4). Shows current tracks with provenance
// badge (local RF vs network) and type. Full detail panel comes with selection
// in a later slice; this proves mixed track types render from live state.

import { useMemo } from "react";
import { trackPresentation } from "../../map/presentationRegistry";
import { useStore } from "../../state/store";

export function TrackList() {
  // Key on the tracks Map, not the whole live object, so the list only re-sorts
  // when tracks change — not on every alert/event/status frame.
  const trackMap = useStore((s) => s.live.tracks);

  const tracks = useMemo(
    () =>
      [...trackMap.values()].sort((a, b) =>
        (a.label ?? a.id).localeCompare(b.label ?? b.id),
      ),
    [trackMap],
  );

  return (
    <section className="panel-section" aria-label="Tracks">
      <h2>Tracks ({tracks.length})</h2>
      {tracks.length === 0 && <p className="muted">No tracks yet.</p>}
      <ul className="track-list">
        {tracks.map((t) => {
          const p = trackPresentation(t);
          return (
            <li key={t.id} className="track-row">
              <span className="swatch" style={{ background: p.color }} aria-hidden />
              <span className="track-label">{t.label ?? t.id}</span>
              <span className="track-type">{t.track_type}</span>
              <span
                className={`prov ${t.locally_received ? "prov-local" : "prov-net"}`}
                title={t.locally_received ? "Local RF" : "Network feed"}
              >
                {t.locally_received ? "LOCAL" : "NET"}
              </span>
              {t.predicted && <span className="predicted">pred</span>}
            </li>
          );
        })}
      </ul>
    </section>
  );
}

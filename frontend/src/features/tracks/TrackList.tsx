// Track list (condensed form of PRD §24.4). Shows current tracks with provenance
// badge (local RF vs network) and type. When the provenance filter is active the
// header reads "Tracks N of M" so the operator sees what's hidden. A fused track
// (more than one contributing source) gets a ×N contributor badge whose tooltip
// names the sources, the active source, and when the operator's own antenna last
// heard it (PRD §8.1, §11.4). Full detail panel comes with selection later.

import { useMemo } from "react";
import { trackPresentation } from "../../map/presentationRegistry";
import { visibleTracks } from "../../state/selectors";
import { useStore } from "../../state/store";
import { fusionMeta, type TrackRecord } from "../../types/records";

function fusionTooltip(track: TrackRecord): string | undefined {
  const meta = fusionMeta(track);
  if (!meta) return undefined;
  const sources = meta.contributors.map((c) => c.source).join(", ");
  const lastLocal = meta.last_local_rf_at ?? "never";
  return `Sources: ${sources}\nActive: ${meta.active_source}\nLast local RF: ${lastLocal}`;
}

export function TrackList() {
  // Key on the tracks Map + filter, not the whole live object, so the list only
  // re-sorts when tracks change — not on every alert/event/status frame.
  const trackMap = useStore((s) => s.live.tracks);
  const filter = useStore((s) => s.provenanceFilter);

  const total = trackMap.size;
  const tracks = useMemo(
    () =>
      visibleTracks(trackMap, filter).sort((a, b) =>
        (a.label ?? a.id).localeCompare(b.label ?? b.id),
      ),
    [trackMap, filter],
  );

  const heading =
    filter === "all" ? `Tracks (${total})` : `Tracks (${tracks.length} of ${total})`;

  return (
    <section className="panel-section" aria-label="Tracks">
      <h2>{heading}</h2>
      {tracks.length === 0 && <p className="muted">No tracks yet.</p>}
      <ul className="track-list">
        {tracks.map((t) => {
          const p = trackPresentation(t);
          const meta = fusionMeta(t);
          const fused = meta !== undefined && meta.fused_count > 1;
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
              {fused && (
                <span className="fused" title={fusionTooltip(t)}>
                  ×{meta.fused_count}
                </span>
              )}
              {t.predicted && <span className="predicted">pred</span>}
            </li>
          );
        })}
      </ul>
    </section>
  );
}

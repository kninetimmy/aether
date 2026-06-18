// Layer control (PRD §24.5): each active presentation layer with its live count
// and a visibility toggle. Counts come straight from live state; styling/labels
// come from the presentation registry via recordLayers.

import { useMemo } from "react";
import { activeLayers } from "../../map/layers/recordLayers";
import {
  featurePresentation,
  trackPresentation,
} from "../../map/presentationRegistry";
import { useStore, type ProvenanceFilter } from "../../state/store";

// The flagship "collapse to local-only" control (PRD §8.2, §16.5). Display only —
// it filters which tracks render, never what the backend ingests or fuses.
const PROVENANCE_OPTIONS: { value: ProvenanceFilter; label: string }[] = [
  { value: "all", label: "All" },
  { value: "local", label: "Local RF" },
  { value: "network", label: "Network" },
];

export function LayerControl() {
  // Counts only depend on tracks + features; key the memo on those so a status
  // or event tick doesn't recompute every layer count (the reducer keeps stable
  // Map references for unchanged collections).
  const tracks = useStore((s) => s.live.tracks);
  const features = useStore((s) => s.live.features);
  const setLayerVisible = useStore((s) => s.setLayerVisible);

  const rows = useMemo(() => {
    const counts = new Map<string, { label: string; color: string; count: number }>();
    for (const t of tracks.values()) {
      const p = trackPresentation(t);
      const row = counts.get(p.layer) ?? { label: p.label, color: p.color, count: 0 };
      row.count += 1;
      counts.set(p.layer, row);
    }
    for (const f of features.values()) {
      const p = featurePresentation(f);
      const row = counts.get(p.layer) ?? { label: p.label, color: p.color, count: 0 };
      row.count += 1;
      counts.set(p.layer, row);
    }
    return activeLayers(tracks, features).map((layer) => ({
      layer,
      ...(counts.get(layer) ?? { label: layer, color: "#9aa6b2", count: 0 }),
    }));
  }, [tracks, features]);

  const visibility = useStore((s) => s.layerVisible);
  const provenanceFilter = useStore((s) => s.provenanceFilter);
  const setProvenanceFilter = useStore((s) => s.setProvenanceFilter);

  return (
    <section className="panel-section" aria-label="Layers and filters">
      <h2>Layers</h2>

      <div
        className="provenance-filter"
        role="radiogroup"
        aria-label="Provenance filter"
      >
        {PROVENANCE_OPTIONS.map((opt) => (
          <button
            key={opt.value}
            type="button"
            role="radio"
            aria-checked={provenanceFilter === opt.value}
            className={provenanceFilter === opt.value ? "active" : ""}
            onClick={() => setProvenanceFilter(opt.value)}
          >
            {opt.label}
          </button>
        ))}
      </div>

      {rows.length === 0 && <p className="muted">No layers active yet.</p>}
      <ul className="layer-list">
        {rows.map((r) => {
          const visible = visibility[r.layer] !== false; // default-on
          return (
            <li key={r.layer} className="layer-row">
              <label>
                <input
                  type="checkbox"
                  checked={visible}
                  onChange={(e) => setLayerVisible(r.layer, e.target.checked)}
                />
                <span className="swatch" style={{ background: r.color }} aria-hidden />
                <span className="layer-name">{r.label}</span>
              </label>
              <span className="count" aria-label={`${r.count} live`}>
                {r.count}
              </span>
            </li>
          );
        })}
      </ul>
    </section>
  );
}

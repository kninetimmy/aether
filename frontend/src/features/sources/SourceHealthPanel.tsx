// Source health (PRD §24.5, §28.3): one row per source with state, last record
// age, and reject count. Color + text label (never color alone, §24.9).

import { useEffect, useState } from "react";
import { sourceStateColor } from "../../map/presentationRegistry";
import { useStore } from "../../state/store";
import type { SourceStatusRecord } from "../../types/records";

function ageSeconds(iso: string | null): number | null {
  if (!iso) return null;
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return null;
  return Math.max(0, Math.round((Date.now() - t) / 1000));
}

function fmtAge(iso: string | null): string {
  const s = ageSeconds(iso);
  if (s === null) return "—";
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.round(s / 60)}m`;
  return `${Math.round(s / 3600)}h`;
}

export function SourceHealthPanel() {
  const statuses = useStore((s) => [...s.live.sourceStatus.values()]) as SourceStatusRecord[];
  const connection = useStore((s) => s.connection);

  // "Last record age" is derived from Date.now() at render. Without a periodic
  // re-render it would freeze whenever no frames arrive; tick once a second so a
  // silent/stalled source visibly ages instead of looking fresh (PRD §28.3).
  const [, setTick] = useState(0);
  useEffect(() => {
    const id = setInterval(() => setTick((n) => n + 1), 1000);
    return () => clearInterval(id);
  }, []);

  return (
    <section className="panel-section" aria-label="Source health">
      <h2>
        Sources
        <span className={`conn conn-${connection}`}>{connection}</span>
      </h2>
      {statuses.length === 0 && <p className="muted">No source status yet.</p>}
      <ul className="source-list">
        {statuses
          .sort((a, b) => a.source.localeCompare(b.source))
          .map((st) => (
            <li key={st.source} className="source-row">
              <span
                className="dot"
                style={{ background: sourceStateColor(st.status) }}
                aria-hidden
              />
              <span className="source-name">{st.source}</span>
              <span className={`source-state state-${st.status}`}>{st.status}</span>
              <span className="source-age" title="last record age">
                {fmtAge(st.last_record_at)}
              </span>
            </li>
          ))}
      </ul>
    </section>
  );
}

// Event + alert feed (PRD §24.2 bottom strip). Newest first; alerts carry their
// severity color and state. Proves events and alerts flow from live state.

import { useMemo } from "react";
import { severityColor } from "../../map/presentationRegistry";
import { useStore } from "../../state/store";

export function EventFeed() {
  const live = useStore((s) => s.live);

  const alerts = useMemo(
    () =>
      [...live.alerts.values()].sort((a, b) =>
        b.triggered_at.localeCompare(a.triggered_at),
      ),
    [live],
  );
  const events = useMemo(() => [...live.events].reverse(), [live]);

  return (
    <section className="feed" aria-label="Timeline and events">
      <div className="feed-col">
        <h2>Alerts ({alerts.length})</h2>
        {alerts.length === 0 && <p className="muted">No alerts.</p>}
        <ul>
          {alerts.map((a) => (
            <li key={a.id} className="feed-row">
              <span
                className="sev-dot"
                style={{ background: severityColor(a.severity) }}
                aria-hidden
              />
              <span className="feed-title">{a.title}</span>
              <span className={`alert-state state-${a.state}`}>{a.state}</span>
            </li>
          ))}
        </ul>
      </div>
      <div className="feed-col">
        <h2>Events ({events.length})</h2>
        {events.length === 0 && <p className="muted">No events.</p>}
        <ul>
          {events.slice(0, 50).map((e) => (
            <li key={`${e.id}:${e.published_at}`} className="feed-row">
              <span className="feed-type">{e.event_type}</span>
              <span className="feed-title">{e.summary}</span>
            </li>
          ))}
        </ul>
      </div>
    </section>
  );
}

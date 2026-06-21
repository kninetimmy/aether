// Firing-alerts panel (PRD §21.4, §24.6): the live alerts the engine raised, with
// per-alert acknowledge / resolve. Reads live.alerts straight from the store (alerts
// arrive over /ws/v2 as alert_upsert deltas). Ack/resolve POST to the lifecycle
// endpoints; the server transitions the alert and rebroadcasts it, so this panel
// does NOT mutate the store — the ws delta updates the row. Color is paired with a
// text state label (never color alone, §24.9).

import { useEffect, useState } from "react";
import { severityColor } from "../../map/presentationRegistry";
import { useStore } from "../../state/store";
import { AlertApiError, acknowledgeAlert, resolveAlert } from "../../api/alertsClient";
import type { AlertRecord, Severity } from "../../types/records";

/** Severity ordering for the active list (most urgent first). */
const SEVERITY_RANK: Record<Severity, number> = {
  critical: 0,
  high: 1,
  medium: 2,
  low: 3,
  info: 4,
};

function ageSeconds(iso: string): number | null {
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return null;
  return Math.max(0, Math.round((Date.now() - t) / 1000));
}

function fmtAge(iso: string): string {
  const s = ageSeconds(iso);
  if (s === null) return "—";
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.round(s / 60)}m`;
  return `${Math.round(s / 3600)}h`;
}

/** Active = not yet resolved; resolved alerts drop out of the actionable list. */
function isActive(a: AlertRecord): boolean {
  return a.state !== "resolved";
}

function sortAlerts(a: AlertRecord, b: AlertRecord): number {
  const r = SEVERITY_RANK[a.severity] - SEVERITY_RANK[b.severity];
  if (r !== 0) return r;
  // Newer first within a severity.
  return Date.parse(b.triggered_at) - Date.parse(a.triggered_at);
}

export function AlertsPanel() {
  const alerts = useStore((s) => [...s.live.alerts.values()]) as AlertRecord[];
  const [busyId, setBusyId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  // Ages are derived from Date.now() at render; tick so an alert visibly ages even
  // when no frames arrive (mirrors SourceHealthPanel).
  const [, setTick] = useState(0);
  useEffect(() => {
    const id = setInterval(() => setTick((n) => n + 1), 1000);
    return () => clearInterval(id);
  }, []);

  const active = alerts.filter(isActive).sort(sortAlerts);
  const resolvedCount = alerts.length - active.length;

  async function act(
    id: string,
    fn: (id: string) => Promise<AlertRecord>,
    verb: string,
  ): Promise<void> {
    setBusyId(id);
    setError(null);
    try {
      // Success: the server rebroadcasts the transitioned alert over /ws/v2, which
      // updates the row. No local store mutation here (server is authoritative).
      await fn(id);
    } catch (err) {
      // 404 = the alert is no longer live (already resolved/expired); anything else
      // is transport. Surface it inline; the COP never crashes on a failed action.
      const detail =
        err instanceof AlertApiError && err.status === 404
          ? "alert is no longer live"
          : err instanceof Error
            ? err.message
            : `${verb} failed`;
      setError(`${verb} failed: ${detail}`);
    } finally {
      setBusyId(null);
    }
  }

  return (
    <section className="panel-section" aria-label="Alerts">
      <h2>
        Alerts
        {active.length > 0 && <span className="count">{active.length}</span>}
      </h2>
      {active.length === 0 && <p className="muted">No active alerts.</p>}
      {error && (
        <p className="alert-error" role="alert">
          {error}
        </p>
      )}
      <ul className="alert-list">
        {active.map((a) => {
          const busy = busyId === a.id;
          return (
            <li key={a.id} className={`alert-row sev-${a.severity}`}>
              <span
                className="sev-dot"
                style={{ background: severityColor(a.severity) }}
                aria-hidden
              />
              <div className="alert-main">
                <div className="alert-title-row">
                  <span className="alert-title" title={a.title}>
                    {a.title}
                  </span>
                  <span className={`alert-state state-${a.state}`}>{a.state}</span>
                  <span className="alert-age" title="time since triggered">
                    {fmtAge(a.triggered_at)}
                  </span>
                </div>
                {a.summary && <div className="alert-summary">{a.summary}</div>}
                <div className="alert-actions">
                  {(a.state === "open" || a.state === "delivery_failed") && (
                    <button
                      type="button"
                      disabled={busy}
                      onClick={() => void act(a.id, acknowledgeAlert, "Acknowledge")}
                    >
                      Acknowledge
                    </button>
                  )}
                  <button
                    type="button"
                    className="alert-resolve"
                    disabled={busy}
                    onClick={() => void act(a.id, resolveAlert, "Resolve")}
                  >
                    Resolve
                  </button>
                </div>
              </div>
            </li>
          );
        })}
      </ul>
      {resolvedCount > 0 && (
        <p className="muted">
          {resolvedCount} resolved {resolvedCount === 1 ? "alert" : "alerts"} hidden.
        </p>
      )}
    </section>
  );
}

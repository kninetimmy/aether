// Header LIVE/REPLAY toggle + a tiny window-picker (M4.8, PRD §19.6/§24.2).
//
// In LIVE mode this is a single "REPLAY" button that opens a small popover to pick a
// window (default: the last N minutes ending now) and starts a session. In REPLAY
// mode it collapses to a "LIVE" button that returns to the live firehose. The picker
// is deliberately minimal but functional: a minutes field + source-less default, so
// an operator can scrub recent history in two clicks. Errors from the bounded server
// (503 persistence-off, 400 over-long window) are shown inline, never thrown into the
// app — replay degrades visibly and never crashes the COP (PRD §37).

import { useState } from "react";
import { ReplayError } from "../../api/replayClient";
import { useStore } from "../../state/store";

/** Default look-back for the quick picker (minutes). */
const DEFAULT_LOOKBACK_MIN = 15;

//: Upper bound on the quick "last N minutes" picker (3 days). The server permits wider
//: windows anywhere in the 30-day retention; an absolute start/end range picker (and the
//: §21.6 export endpoint) is a deferred follow-up — this control is the recent-history
//: scrubber, not the full archival range.
const MAX_LOOKBACK_MIN = 4320;

export function ReplayLauncher() {
  const mode = useStore((s) => s.replay.mode);
  const enterReplay = useStore((s) => s.enterReplay);
  const exitReplay = useStore((s) => s.exitReplay);

  const [open, setOpen] = useState(false);
  const [minutes, setMinutes] = useState(DEFAULT_LOOKBACK_MIN);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (mode === "replay") {
    return (
      <button
        type="button"
        className="replay-toggle replay-toggle-live"
        title="Return to live"
        onClick={() => exitReplay()}
      >
        ◉ LIVE
      </button>
    );
  }

  async function start() {
    setBusy(true);
    setError(null);
    const end = new Date();
    const start = new Date(end.getTime() - Math.max(1, minutes) * 60_000);
    try {
      await enterReplay({ start: start.toISOString(), end: end.toISOString() });
      setOpen(false);
    } catch (err) {
      // Honest, specific messaging: 503 = persistence disabled; 400 = bad/over-long
      // window; anything else = transport. The picker stays open so the operator can
      // adjust and retry (PRD §37 graceful degradation).
      if (err instanceof ReplayError && err.status === 503) {
        setError("Replay needs persistence enabled (AETHER_PERSIST).");
      } else if (err instanceof ReplayError && err.status === 400) {
        setError(err.message);
      } else {
        setError(err instanceof Error ? err.message : "Replay failed.");
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <span className="replay-launcher">
      <button
        type="button"
        className="replay-toggle"
        title="Replay persisted history"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
      >
        {"⏴"} REPLAY
      </button>
      {open && (
        <div className="replay-picker" role="dialog" aria-label="Start replay">
          <label className="replay-picker-row">
            <span>Last</span>
            <input
              type="number"
              min={1}
              max={MAX_LOOKBACK_MIN}
              value={minutes}
              aria-label="Look-back minutes"
              onChange={(e) => setMinutes(Number(e.target.value))}
            />
            <span>min, ending now</span>
          </label>
          {error && (
            <p className="replay-picker-error" role="alert">
              {error}
            </p>
          )}
          <div className="replay-picker-actions">
            <button type="button" onClick={() => setOpen(false)} disabled={busy}>
              Cancel
            </button>
            <button
              type="button"
              className="replay-picker-go"
              onClick={() => void start()}
              disabled={busy}
            >
              {busy ? "Loading…" : "Start replay"}
            </button>
          </div>
        </div>
      )}
    </span>
  );
}

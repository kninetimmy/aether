// Replay timeline UI (HISTORY-FR-007, PRD §19.6). Renders in the bottom footer when
// the app is in REPLAY mode: a scrubber over the session window plus transport
// controls (play/pause, speed, step ±, jump-to-time) and a one-click RETURN-TO-LIVE.
// It shows the current cursor time in UTC and warns when the buffer was truncated.
//
// Read-only by construction: every control drives a store action that mutates ONLY
// the replay slice (cursor/playing/speed) — never live state, the websocket, or any
// alert path. Replay cannot fire live alerts (PRD §19.6/§32); this component is part
// of that guarantee because it has no path to publish.

import { useMemo } from "react";
import { useStore } from "../../state/store";
import { REPLAY_SPEEDS, sessionBoundsMs } from "../../state/replay";

/** Step granularity for the ± buttons (seconds of replay time). */
const STEP_S = 10;

/** Format an epoch-ms instant as a compact UTC HH:MM:SS (timeline is UTC, PRD §24). */
function fmtUtc(ms: number): string {
  if (!Number.isFinite(ms)) return "—";
  return new Date(ms).toISOString().replace("T", " ").replace(".000Z", "Z");
}

export function ReplayTimeline() {
  const replay = useStore((s) => s.replay);
  const play = useStore((s) => s.play);
  const pause = useStore((s) => s.pause);
  const setSpeed = useStore((s) => s.setSpeed);
  const step = useStore((s) => s.step);
  const seek = useStore((s) => s.seek);
  const exitReplay = useStore((s) => s.exitReplay);

  const { session, cursorMs, playing, speed } = replay;
  const { startMs, endMs } = useMemo(() => sessionBoundsMs(session), [session]);

  // Defensive: the timeline only renders in replay mode, but guard anyway so a
  // transient null session can't throw (PRD §37).
  if (!session) {
    return (
      <section className="replay-timeline" aria-label="Replay timeline">
        <p className="muted">No replay session loaded.</p>
      </section>
    );
  }

  const spanMs = Math.max(0, endMs - startMs);
  const elapsedMs = Math.max(0, cursorMs - startMs);
  const atEnd = cursorMs >= endMs;

  return (
    <section className="replay-timeline" aria-label="Replay timeline">
      <div className="replay-controls">
        <button
          type="button"
          className="replay-btn replay-step"
          title={`Back ${STEP_S}s`}
          aria-label={`Step back ${STEP_S} seconds`}
          onClick={() => step(-STEP_S * 1000)}
        >
          ⏮
        </button>

        {playing ? (
          <button
            type="button"
            className="replay-btn replay-play"
            title="Pause"
            aria-label="Pause"
            onClick={() => pause()}
          >
            ⏸
          </button>
        ) : (
          <button
            type="button"
            className="replay-btn replay-play"
            title="Play"
            aria-label="Play"
            disabled={atEnd}
            onClick={() => play()}
          >
            ▶
          </button>
        )}

        <button
          type="button"
          className="replay-btn replay-step"
          title={`Forward ${STEP_S}s`}
          aria-label={`Step forward ${STEP_S} seconds`}
          onClick={() => step(STEP_S * 1000)}
        >
          ⏭
        </button>

        <label className="replay-speed">
          <span className="muted">speed</span>
          <select
            value={speed}
            aria-label="Playback speed"
            onChange={(e) => setSpeed(Number(e.target.value))}
          >
            {REPLAY_SPEEDS.map((x) => (
              <option key={x} value={x}>
                {x}×
              </option>
            ))}
          </select>
        </label>

        <span className="replay-cursor" aria-label="Cursor time (UTC)">
          {fmtUtc(cursorMs)}
        </span>

        <span className="replay-count muted">
          {session.count} record{session.count === 1 ? "" : "s"}
          {session.truncated && (
            <span
              className="replay-truncated"
              title="The window held more records than the cap; only the earliest were loaded."
            >
              {" "}
              · truncated
            </span>
          )}
        </span>

        <button
          type="button"
          className="replay-btn replay-return"
          title="Return to live"
          onClick={() => exitReplay()}
        >
          ⟲ LIVE
        </button>
      </div>

      <div className="replay-scrubber">
        <span className="replay-bound muted" aria-hidden>
          {fmtUtc(startMs)}
        </span>
        <input
          type="range"
          className="replay-range"
          aria-label="Replay scrubber (jump to time)"
          min={startMs}
          max={endMs}
          // A zero-width window (start===end) leaves the slider effectively fixed.
          step={Math.max(1, Math.floor(spanMs / 1000) || 1)}
          value={cursorMs}
          onChange={(e) => seek(Number(e.target.value))}
        />
        <span className="replay-bound muted" aria-hidden>
          {fmtUtc(endMs)}
        </span>
      </div>

      <div className="replay-elapsed muted">
        +{Math.round(elapsedMs / 1000)}s / {Math.round(spanMs / 1000)}s
      </div>
    </section>
  );
}

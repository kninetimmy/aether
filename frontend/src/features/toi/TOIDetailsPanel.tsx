// TOI details panel (PRD §24.6). Shows the selected track's identity, last-seen,
// and — honestly (T27, PRD §8.1) — its LOCAL reception: a target the operator's
// own antenna is hearing RIGHT NOW reads "live locally", while a long-quiet local
// target reads "last heard locally at T", NEVER "live". The distinction comes
// entirely from data the fusion engine already computes (record.locally_received +
// a per-contributor freshness == "live" leg vs. fusionMeta().last_local_rf_at, the
// monotonic "ever/last heard locally" timestamp that survives local expiry). A
// star toggles the stable watchlistKey. All styling routes through the centralized
// presentation registry; this component stays dumb.

import { militaryBadge, toiHighlight } from "../../map/presentationRegistry";
import { isOnWatchlist, watchlistKey } from "../../state/selectors";
import { useStore } from "../../state/store";
import { fusionMeta, type FusionContributor, type TrackRecord } from "../../types/records";

/** Whether the operator's antenna is hearing this target right now (T27). */
function liveLocalNow(track: TrackRecord): boolean {
  if (!track.locally_received) return false;
  const meta = fusionMeta(track);
  // Unfused local leg: the adapter flag alone is NOT a "right now" claim (T27,
  // §8.1) — it survives until expiry, so a quiet target would mislabel as live.
  // Fall through to the honest "locally received" / last-heard branch instead.
  if (!meta) return false;
  const contributors = Array.isArray(meta.contributors) ? meta.contributors : [];
  return contributors.some((c) => c != null && c.local_rf && c.freshness === "live");
}

function fmtTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const t = Date.parse(iso);
  return Number.isNaN(t) ? iso : new Date(t).toISOString().replace("T", " ").replace("Z", "Z");
}

function contributorRow(c: FusionContributor) {
  return (
    <li key={`${c.source ?? "?"}:${c.observed_at}`} className="toi-contributor">
      <span className={`prov ${c.local_rf ? "prov-local" : "prov-net"}`}>
        {c.local_rf ? "LOCAL" : "NET"}
      </span>
      <span className="toi-contributor-src">{c.source ?? "?"}</span>
      <span className={`toi-fresh toi-fresh-${c.freshness}`}>{c.freshness}</span>
    </li>
  );
}

export function TOIDetailsPanel() {
  const selectedTrackId = useStore((s) => s.selectedTrackId);
  const track = useStore((s) =>
    selectedTrackId ? (s.live.tracks.get(selectedTrackId) ?? null) : null,
  );
  const watchlist = useStore((s) => s.watchlist);
  const toggleWatchlist = useStore((s) => s.toggleWatchlist);
  const selectTrack = useStore((s) => s.selectTrack);

  if (!selectedTrackId) return null;

  if (!track) {
    // Selected id no longer in live state (expired/GC'd) — selection by raw id is
    // ephemeral, so degrade honestly rather than render a stale ghost.
    return (
      <section className="panel-section toi-panel" aria-label="TOI details">
        <h2>
          TOI
          <button className="toi-close" onClick={() => selectTrack(null)} title="Close">
            ×
          </button>
        </h2>
        <p className="muted">Selected track is no longer present.</p>
      </section>
    );
  }

  const meta = fusionMeta(track);
  const live = liveLocalNow(track);
  const mil = militaryBadge(track.classification);
  const onList = isOnWatchlist(track, watchlist);
  const star = toiHighlight().badge;
  // Honest local-reception line (T27): "live locally" ONLY when an antenna leg is
  // live now; otherwise the last-heard-locally timestamp, never labeled live.
  const lastLocal = meta?.last_local_rf_at ?? null;

  return (
    <section className="panel-section toi-panel" aria-label="TOI details">
      <h2>
        TOI
        <button
          type="button"
          className={`star${onList ? " on" : ""}`}
          aria-pressed={onList}
          title={onList ? "Remove from watchlist" : "Add to watchlist"}
          onClick={() => toggleWatchlist(watchlistKey(track))}
        >
          {star}
        </button>
        <button className="toi-close" onClick={() => selectTrack(null)} title="Close">
          ×
        </button>
      </h2>

      <dl className="toi-fields">
        <dt>Label</dt>
        <dd>{track.label ?? track.id}</dd>
        <dt>Type</dt>
        <dd>{track.track_type}</dd>
        <dt>Provenance</dt>
        <dd>
          <span className={`prov ${track.locally_received ? "prov-local" : "prov-net"}`}>
            {track.locally_received ? "LOCAL" : "NET"}
          </span>
          {meta && <span className="muted"> active: {meta.active_source}</span>}
        </dd>
        <dt>Last seen</dt>
        <dd>{fmtTime(track.observed_at)}</dd>
        <dt>Local reception</dt>
        <dd>
          {live ? (
            <span className="toi-live">live locally now</span>
          ) : lastLocal ? (
            <span className="toi-lastlocal">last heard locally at {fmtTime(lastLocal)}</span>
          ) : track.locally_received ? (
            <span className="toi-lastlocal">locally received</span>
          ) : (
            <span className="muted">network only</span>
          )}
        </dd>
        {mil && (
          <>
            <dt>Military</dt>
            <dd>
              <span className="mil" title={mil.title}>
                {mil.text}
              </span>
            </dd>
          </>
        )}
      </dl>

      {meta && meta.contributors.length > 0 && (
        <div className="toi-contributors">
          <h3 className="muted">Contributors ({meta.fused_count})</h3>
          <ul>
            {meta.contributors
              .filter(
                (c): c is FusionContributor => c != null && typeof c === "object",
              )
              .map(contributorRow)}
          </ul>
        </div>
      )}
    </section>
  );
}

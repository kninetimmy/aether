// Typed REST client for the operator watchlist (M6.6b, PRD §21.5/§24.6).
//
// The watchlist is now BACKEND-AUTHORITATIVE: entries live in the SQLite store and
// drive the alert engine's `watchlist` condition operator server-side. This module is
// the ONLY place the /api/v2/watchlist endpoints are spoken to. The browser keeps a
// localStorage MIRROR (store.ts) purely for instant first paint + offline tolerance;
// the server is the source of truth and a successful hydrate reconciles to it.
//
// The membership key is the stable, client-minted `watchlistKey(track)` (selectors.ts),
// which the backend recomputes identically via `watchlist_key(record)` — so a key this
// client PUTs matches the record the engine evaluates. Keys contain ':' (e.g.
// "aircraft:icao:abc123", "orbital:celestrak:25544"); they go in the URL path under the
// server's {key:path} converter, encoded with encodeURIComponent and decoded server-side.
//
// Errors surface as a typed `WatchlistError` carrying the HTTP status (0 = transport/
// parse failure) so callers can degrade honestly: a 503 means persistence is disabled
// (watchlist unavailable) and the caller keeps the localStorage cache.

/** Base path for the watchlist REST API. */
const WATCHLIST_BASE = "/api/v2/watchlist";

/** A stored watchlist entry as returned by the server (snake_case wire shape). */
export interface WatchlistEntry {
  key: string;
  label: string | null;
  priority: number | null;
  notes: string | null;
  created_at: string;
  updated_at: string;
}

/** Optional operator metadata sent on an upsert (PUT). Membership needs no body. */
export interface WatchlistEntryMeta {
  label?: string | null;
  priority?: number | null;
  notes?: string | null;
}

/** A typed watchlist-API failure carrying the HTTP status (0 = transport/parse error). */
export class WatchlistError extends Error {
  constructor(
    message: string,
    /** HTTP status, or 0 when the request never completed / response was unparseable. */
    readonly status: number,
  ) {
    super(message);
    this.name = "WatchlistError";
  }
}

/** Pull a human detail out of a non-ok JSON body ({detail: ...}); best-effort. */
async function errorDetail(res: Response): Promise<string> {
  try {
    const body: unknown = await res.json();
    if (
      body !== null &&
      typeof body === "object" &&
      "detail" in body &&
      typeof (body as { detail: unknown }).detail === "string"
    ) {
      return (body as { detail: string }).detail;
    }
  } catch {
    // Non-JSON / empty body — fall back to the status text below.
  }
  return res.statusText || `HTTP ${res.status}`;
}

/** Encode a watchlist key for the URL path (':' → '%3A', decoded by {key:path}). */
function keyPath(key: string): string {
  return `${WATCHLIST_BASE}/${encodeURIComponent(key)}`;
}

/**
 * List all watchlist entries (authoritative server state). Throws {@link WatchlistError}
 * on any non-2xx (e.g. 503 when persistence is off) or transport/parse failure — the
 * caller keeps its localStorage cache on failure rather than wiping the watchlist.
 */
export async function listWatchlist(): Promise<WatchlistEntry[]> {
  let res: Response;
  try {
    res = await fetch(WATCHLIST_BASE);
  } catch (err) {
    throw new WatchlistError(
      `watchlist request failed: ${err instanceof Error ? err.message : String(err)}`,
      0,
    );
  }
  if (!res.ok) {
    throw new WatchlistError(await errorDetail(res), res.status);
  }
  try {
    const body = (await res.json()) as { entries?: WatchlistEntry[] };
    return Array.isArray(body.entries) ? body.entries : [];
  } catch (err) {
    throw new WatchlistError(
      `watchlist response was not valid JSON: ${err instanceof Error ? err.message : String(err)}`,
      0,
    );
  }
}

/**
 * Upsert a watchlist entry (idempotent PUT) — toggle-on. The body is operator meta
 * only (membership is the key itself); an empty body marks the key with no label.
 * Throws {@link WatchlistError} on a non-2xx or transport failure.
 */
export async function putWatchlistEntry(
  key: string,
  meta: WatchlistEntryMeta = {},
): Promise<void> {
  let res: Response;
  try {
    res = await fetch(keyPath(key), {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(meta),
    });
  } catch (err) {
    throw new WatchlistError(
      `watchlist add failed: ${err instanceof Error ? err.message : String(err)}`,
      0,
    );
  }
  if (!res.ok) {
    throw new WatchlistError(await errorDetail(res), res.status);
  }
}

/**
 * Remove a watchlist entry (DELETE) — toggle-off. A 404 is treated as success (already
 * gone), so a double-remove is idempotent. Throws {@link WatchlistError} only on an
 * unexpected status or transport failure.
 */
export async function deleteWatchlistEntry(key: string): Promise<void> {
  let res: Response;
  try {
    res = await fetch(keyPath(key), { method: "DELETE" });
  } catch (err) {
    throw new WatchlistError(
      `watchlist remove failed: ${err instanceof Error ? err.message : String(err)}`,
      0,
    );
  }
  // 204 = deleted; 404 = already gone (idempotent toggle-off) — both are fine.
  if (res.ok || res.status === 404) return;
  throw new WatchlistError(await errorDetail(res), res.status);
}

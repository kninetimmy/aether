"""CelesTrak orbital-object adapter (PRD §11.14, §18.12, M6.5).

Syncs **General Perturbations (GP) element sets** from CelesTrak's public OMM service and
propagates them with **SGP4** to plot overhead objects on the COP as ``orbital_object``
tracks. This is *tracking* (propagating published elements to a position), never satellite
reception (PRD §5 settled decision).

Three cadences: one sync, two propagate (fast watchlisted / slow catalog), plus a cheap
watchlist refresh — over one provider:

1. **Sync** (default every 6 h, no faster than CelesTrak's ~2 h refresh — §38 rate limit):
   ``GET <base>/NORAD/elements/gp.php?GROUP=<slug>&FORMAT=json`` per configured group.
   ``FORMAT=json`` is passed **explicitly** — the service default changed to CSV on
   2026-05-09. The JSON is an array of OMM objects; each becomes a cached ``Satrec`` keyed
   by NORAD id, with its element-set epoch retained for an age label. On HTTP 301/403/404 the
   request is **abandoned** (the response will not change; 50 such errors in 2 h firewalls the
   IP) and the last-good cache is served — never a tight retry loop.
2. **Propagate** — two tiers over a **disjoint partition** of the synced set (ORBIT-FR-011):
   the operator's **watchlisted** objects (``orbital:celestrak:<norad>`` keys, read from the
   persistence store) ride a **fast** cadence (default 2 s) for smooth tracks, while the broad
   catalog rides the existing **slow** cadence (default 15 s). Because the partition is a strict
   set difference on NORAD id, a watchlisted object is propagated/emitted by the fast tier only —
   never double-emitted by the slow tier. Each tick propagates its subset to *now*, keeps only
   objects currently above ``min_elevation_deg`` (ORBIT-FR-007, default 10°), and emits one
   ``orbital_object`` ``TrackRecord`` each — positions labelled ``predicted=True`` with the
   element-set epoch age in attributes. A propagation/NaN error skips that object (the orbit
   is never plotted at a bad position — fail-visibly, §37). The watchlist is re-read every
   ``watchlist_refresh_s`` (default 30 s) so toggling a satellite moves it between tiers with
   no adapter restart; an absent/empty watchlist (persistence off) collapses the fast tier and
   the behaviour is identical to the single-cadence M6.5 path. The fast tier only changes the
   PROPAGATE cadence (local CPU) — it never touches the fetch/sync cadence (§38).

Capability-gated on the optional ``sgp4`` parser (the ``[orbital]`` extra), imported lazily
inside :mod:`aether.orbital.sgp4_propagate`: a missing dep degrades to one ``offline`` status
then a clean exit (the GLM/FIRMS stance, PRD §2/§37). The fake feeder ships canned OMM and
drives the **real** propagate path, so the full chain runs with no network and no ``sgp4``.

Every record carries CelesTrak attribution and a "predicted; not for navigation/operational
use" caveat (PRD §11.2/§37). Responsibility split mirrors :mod:`aether.adapters.firms`:

- :class:`CelestrakProvider` / :class:`CelestrakHttpProvider` — fetch raw OMM JSON per group
  (the live HTTPS service, or the in-process ``fake`` feeder).
- :func:`build_satrecs` — pure OMM-list → cached :class:`OrbitalElement` set (bad rows skipped).
- :func:`celestrak_records` — the ``records()`` contract: ``starting``, a sync-then-propagate
  loop with elevation filtering, last-good cache, and ``degraded``-on-failure isolation.
- :func:`run_celestrak` — bus pump + missing-``sgp4`` ``offline`` gate + jittered backoff.
"""

import asyncio
import functools
import json
import logging
import math
import random
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Protocol

import aiomqtt

from aether.bus.client import connect
from aether.config import Settings
from aether.orbital.pass_prediction import PassPrediction, predict_next_pass
from aether.orbital.sgp4_propagate import (
    OmmInitError,
    Sgp4Unavailable,
    build_satrec,
    propagate,
)
from aether.persist.watchlist import list_watchlist
from aether.schema.geometry import Point
from aether.schema.provenance import Provenance
from aether.schema.records import Record, SourceStatusRecord, TrackRecord

log = logging.getLogger(__name__)

#: Stable source name + retained health-record id (PRD §23 status stream). Records
#: publish to ``aether/v2/records/celestrak`` (derived from this name).
SOURCE = "celestrak"
STATUS_ID = f"source_status:{SOURCE}"

#: Attribution carried on every record + status so provenance stays honest (§11.2).
ATTRIBUTION = "Orbital data: CelesTrak (celestrak.org)"
#: The non-negotiable honesty caveat for this layer (predicted positions, PRD §37).
CAVEAT = "Predicted SGP4 position; not for navigation or operational use."

#: Default GP service host; ``/NORAD/elements/gp.php`` hangs off it. ``fake``/``demo``
#: selects the no-hardware feeder instead.
DEFAULT_BASE_URL = "https://celestrak.org"

#: Jittered exponential backoff bounds — shared shape with every other adapter (§17.1).
INITIAL_BACKOFF_S = 1.0
MAX_BACKOFF_S = 60.0

#: Re-attempt a failed/absent pass prediction no more often than this (s). A ~24h scan is too
#: costly to re-run on every 2s fast tick for a decayed object or one with genuinely no pass
#: in the search window (M6.8, PRD §32 #18/#19).
PASS_PREDICT_RETRY_S = 60.0

#: Provider-name aliases selecting the in-process no-hardware feeder.
_FAKE_PROVIDER_NAMES = frozenset({"fake", "demo"})

#: Hard cap on a single GP response body; a runaway is rejected rather than read unbounded.
MAX_RESPONSE_BYTES = 16 * 1024 * 1024

#: HTTP statuses that will NOT change on retry (gone/forbidden/not-found): abandon the
#: request and serve last-good, never tight-loop (§38 — 50 such errors in 2 h firewalls us).
_NO_RETRY_STATUSES = frozenset({301, 403, 404})


def _now() -> datetime:
    return datetime.now(UTC)


def _backoff(delay: float) -> tuple[float, float]:
    """Full-jitter backoff: sleep a random slice of ``delay``, then double it (capped)."""
    capped = min(delay, MAX_BACKOFF_S)
    return random.uniform(0.0, capped), min(capped * 2.0, MAX_BACKOFF_S)


# --- Parsed element model (provider output) ------------------------------------


@dataclass(frozen=True)
class OrbitalElement:
    """One CelesTrak object: its identity, group, epoch, and a built ``Satrec``."""

    norad_id: int
    object_id: str
    object_name: str
    group: str
    epoch: datetime
    satrec: Any  # sgp4.api.Satrec (untyped optional dep)


def parse_epoch(raw: str) -> datetime | None:
    """OMM ``EPOCH`` ISO-8601 (UTC) → aware UTC datetime; ``None`` if unparseable.

    CelesTrak emits e.g. ``2026-06-21T03:14:15.926784``; a bare (naive) value is read as
    UTC (the OMM epoch is UTC by definition), an offset-bearing value is converted.
    """
    try:
        dt = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


class CelestrakNoRetry(RuntimeError):
    """A GP fetch hit a non-retryable HTTP status (301/403/404): abandon, serve last-good."""


# --- Provider (raw OMM JSON fetch) ---------------------------------------------


class CelestrakProvider(Protocol):
    """A source of CelesTrak GP OMM JSON for a group slug."""

    name: str

    async def fetch_group(self, group: str) -> list[dict[str, Any]]: ...


def _blocking_get(url: str, timeout_s: float) -> bytes:
    if not url.startswith("https://"):  # public CelesTrak service is https; never downgrade
        raise ValueError(f"refusing non-https CelesTrak URL: {url!r}")
    req = urllib.request.Request(url, headers={"User-Agent": "aether-celestrak/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:  # noqa: S310 (https-checked)
            # Read one byte past the cap so an over-limit body is *detected*, not silently
            # truncated and parsed as valid OMM (a truncated body would mis-parse).
            raw = bytes(resp.read(MAX_RESPONSE_BYTES + 1))
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ValueError(f"CelesTrak response exceeded {MAX_RESPONSE_BYTES} bytes for {url!r}")
        return raw
    except urllib.error.HTTPError as exc:
        # 301/403/404 will not change on retry — abandon this request (§38 rate-limit guard).
        if exc.code in _NO_RETRY_STATUSES:
            raise CelestrakNoRetry(f"HTTP {exc.code} for {url!r}; not retrying") from None
        raise


async def _default_fetch(url: str, *, timeout_s: float) -> bytes:
    # Blocking urllib off the event loop (mirrors the FAA/USGS providers); no extra dep.
    return await asyncio.to_thread(_blocking_get, url, timeout_s)


class CelestrakHttpProvider:
    """Fetch CelesTrak GP OMM JSON over HTTPS (PRD §18.12, §38 rate limits).

    ``fetch`` is injectable so tests drive canned JSON with no network. The default GETs
    ``<base>/NORAD/elements/gp.php?GROUP=<slug>&FORMAT=json`` — ``FORMAT=json`` is **always**
    sent explicitly (the service default became CSV on 2026-05-09). ``urllib`` follows the
    301 redirect to the canonical host transparently; a *terminal* 301/403/404 raises
    :class:`CelestrakNoRetry` so the caller abandons the request and serves last-good.
    """

    name = "celestrak"

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        *,
        timeout_s: float = 15.0,
        fetch: Callable[[str], Awaitable[bytes]] | None = None,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._fetch = (
            fetch if fetch is not None else functools.partial(_default_fetch, timeout_s=timeout_s)
        )

    async def fetch_group(self, group: str) -> list[dict[str, Any]]:
        # URL-encode the operator-configurable group so a stray '&'/'='/'#' cannot inject
        # extra query parameters into the GP request. FORMAT=json stays explicit (the
        # service default became CSV 2026-05-09).
        group_q = urllib.parse.quote(group, safe="")
        url = f"{self._base}/NORAD/elements/gp.php?GROUP={group_q}&FORMAT=json"
        raw = await self._fetch(url)
        data = json.loads(raw)
        if not isinstance(data, list):
            raise ValueError("CelesTrak GP response did not return a JSON array")
        return [row for row in data if isinstance(row, dict)]


def build_provider(cfg: Settings) -> CelestrakProvider:
    """Resolve the configured CelesTrak provider (live host, or the fake feeder)."""
    base = cfg.celestrak_base_url.strip()
    if base.lower() in _FAKE_PROVIDER_NAMES:
        from aether.adapters.celestrak_fake_feeder import FakeCelestrakProvider

        # Solve the canned roster relative to the configured observer so the demo always has
        # an above-horizon object regardless of where the operator points the station.
        return FakeCelestrakProvider(
            observer_lat=cfg.celestrak_observer_lat, observer_lon=cfg.celestrak_observer_lon
        )
    return CelestrakHttpProvider(base, timeout_s=cfg.celestrak_timeout_s)


# --- Normalization (OMM list → element cache; element → TrackRecord) -----------


def build_satrecs(
    rows: Sequence[dict[str, Any]], *, group: str
) -> tuple[list[OrbitalElement], int]:
    """Pure OMM-list → ``(elements, skipped)``: build a ``Satrec`` per valid object.

    Each row is run through :func:`aether.orbital.sgp4_propagate.build_satrec`. A row that
    is missing required fields, has an unparseable epoch, or fails OMM ``initialize`` is
    **skipped** (counted), never half-built into a bad orbit (fail-visibly, §37). A missing
    ``sgp4`` dependency propagates as :class:`Sgp4Unavailable` for the capability gate.
    """
    elements: list[OrbitalElement] = []
    skipped = 0
    for row in rows:
        epoch = parse_epoch(str(row.get("EPOCH", "")))
        if epoch is None:
            skipped += 1
            continue
        try:
            satrec = build_satrec(row)
        except OmmInitError as exc:
            log.debug("CelesTrak object skipped (%s)", exc)
            skipped += 1
            continue
        norad_raw = row.get("NORAD_CAT_ID")
        try:
            norad_id = int(str(norad_raw).strip())  # JSON number or numeric string
        except (TypeError, ValueError):
            skipped += 1
            continue
        elements.append(
            OrbitalElement(
                norad_id=norad_id,
                object_id=str(row.get("OBJECT_ID", "")) or "?",
                object_name=str(row.get("OBJECT_NAME", "")) or f"NORAD {norad_id}",
                group=group,
                epoch=epoch,
                satrec=satrec,
            )
        )
    return elements, skipped


def element_to_record(
    element: OrbitalElement,
    *,
    observer_lat: float,
    observer_lon: float,
    observer_alt_m: float,
    at: datetime,
    valid_s: float,
    prediction: PassPrediction | None = None,
) -> TrackRecord | None:
    """Propagate one element to ``at`` and normalize to an ``orbital_object`` ``TrackRecord``.

    Returns ``None`` when SGP4 cannot produce a finite position (the object is skipped,
    never plotted — fail-visibly, §37). ``predicted=True`` and ``locally_received=False``;
    az/el/range/epoch/age live in ``attributes`` because the schema is ``extra="forbid"`` (no
    new top-level fields, no ``SCHEMA_VERSION`` bump — the maintainer-approved approach).

    ``prediction`` (M6.8, PRD §32 #18/#19) is the cached next/in-progress pass for this object,
    attached as ``pass_culmination_at``/``pass_max_elevation_deg`` (always present together)
    plus ``pass_rise_at``/``pass_set_at`` (each present only when that floor crossing falls
    inside the search window). Defaults to ``None`` so the slow tier — which never computes
    predictions — and every existing caller/test/bench stay byte-identical. When ``None`` the
    ``pass_*`` keys are omitted entirely rather than emitted as null/fake values (honest-
    unevaluable, §37).
    """
    state = propagate(
        element.satrec,
        at,
        observer_lat_deg=observer_lat,
        observer_lon_deg=observer_lon,
        observer_alt_m=observer_alt_m,
    )
    if state is None:
        return None

    rid = f"orbital:celestrak:{element.norad_id}"
    age_s = (at - element.epoch).total_seconds()
    attributes: dict[str, Any] = {
        "norad_id": element.norad_id,
        "object_id": element.object_id,
        "object_name": element.object_name,
        "group": element.group,
        "element_epoch_utc": element.epoch.isoformat(),
        "element_age_s": age_s,
        "azimuth_deg": state.azimuth_deg,
        "elevation_deg": state.elevation_deg,
        "slant_range_m": state.slant_range_m,
        "attribution": ATTRIBUTION,
        "caveat": CAVEAT,
    }
    if prediction is not None:
        attributes["pass_culmination_at"] = prediction.culmination_at.isoformat()
        attributes["pass_max_elevation_deg"] = prediction.max_elevation_deg
        if prediction.rise_at is not None:
            attributes["pass_rise_at"] = prediction.rise_at.isoformat()
        if prediction.set_at is not None:
            attributes["pass_set_at"] = prediction.set_at.isoformat()
    return TrackRecord(
        id=rid,
        source=SOURCE,
        observed_at=at,  # propagation time — this is a predicted position AT `at`
        received_at=at,
        published_at=at,
        correlation_key=rid,
        track_type="orbital_object",
        label=element.object_name,
        geometry=Point(coordinates=[state.sub_lon_deg, state.sub_lat_deg]),
        altitude_m=state.altitude_m,
        locally_received=False,  # network-derived element set, propagated locally
        predicted=True,  # honest labeling: this is a propagated, not observed, position
        valid_until=at + timedelta(seconds=valid_s),
        provenance=[
            Provenance(
                source=SOURCE,
                provider="celestrak",
                observed_at=at,
                received_at=at,
                local_rf=False,
                derived=True,  # position derived by propagation, not observed
                confidence="medium",
            )
        ],
        tags=["orbital", "celestrak", element.group],
        attributes=attributes,
    )


def _status(
    status: Literal["starting", "connected", "degraded", "stale", "offline", "disabled"],
    now: datetime,
    *,
    records_received: int = 0,
    records_rejected: int = 0,
    last_record_at: datetime | None = None,
    error_code: str | None = None,
    error_summary: str | None = None,
    attributes: dict[str, Any] | None = None,
) -> SourceStatusRecord:
    return SourceStatusRecord(
        id=STATUS_ID,
        source=SOURCE,
        observed_at=now,
        received_at=now,
        published_at=now,
        status=status,
        last_record_at=last_record_at,
        records_received=records_received,
        records_rejected=records_rejected,
        error_code=error_code,
        error_summary=error_summary,
        attributes={"attribution": ATTRIBUTION, **(attributes or {})},
    )


# --- Two-tier watchlist partition (ORBIT-FR-011) -------------------------------

#: Watchlist keys for orbital objects are minted as ``orbital:celestrak:<norad>`` (the same
#: identity key the alert engine + UI use); the integer suffix is the NORAD catalogue id.
_WATCHLIST_KEY_PREFIX = "orbital:celestrak:"


def _read_watchlisted_norads(path: str) -> set[int]:
    """Blocking read of the persisted watchlist → orbital NORAD ids. Drive via
    ``asyncio.to_thread`` so it never blocks the loop. Honest degradation: a missing store/
    table (``list_watchlist`` → ``[]``) or any parse issue yields an empty set, never raises."""
    out: set[int] = set()
    try:
        entries = list_watchlist(path)
    except Exception:
        log.warning("CelesTrak watchlist read failed; treating as empty", exc_info=True)
        return set()
    for entry in entries:
        if entry.key.startswith(_WATCHLIST_KEY_PREFIX):
            try:
                out.add(int(entry.key[len(_WATCHLIST_KEY_PREFIX) :]))
            except ValueError:
                continue  # malformed suffix → skip, never crash
    return out


def _partition(
    elements: list[OrbitalElement], fast_norads: set[int]
) -> tuple[list[OrbitalElement], list[OrbitalElement]]:
    """Disjoint split: ``(watchlisted-in-catalog, the rest)``. Empty watchlist → ``([], all)``.

    The split is a strict set membership test on ``norad_id``, so the fast and slow lists are
    guaranteed disjoint — the structural no-double-emit guarantee (ORBIT-FR-011): a watchlisted
    NORAD can only ever be in ``fast``, so the slow tier never re-emits it.
    """
    if not fast_norads:
        return [], list(elements)
    fast = [e for e in elements if e.norad_id in fast_norads]
    slow = [e for e in elements if e.norad_id not in fast_norads]
    return fast, slow


def _prune_pass_cache(
    pass_cache: dict[int, PassPrediction | None],
    pass_retry_at: dict[int, float],
    fast_elements: list[OrbitalElement],
) -> None:
    """Drop cache/backoff entries for NORAD ids no longer on the fast tier (M6.8).

    Called after every re-:func:`_partition` (a sync or a watchlist refresh): a watchlisted
    object that is de-watchlisted, or that drops out of the synced catalog entirely, must not
    leave its prediction/backoff entry behind forever — otherwise the two NORAD-keyed maps only
    grow for the life of the connection (bounded only by total catalog size, PRD §37 bounded
    maps). Keeps just the CURRENT fast set; a re-watchlisted object gets a fresh cold-cache
    recompute rather than resurrecting a stale one.
    """
    keep = {e.norad_id for e in fast_elements}
    for nid in [n for n in pass_cache if n not in keep]:
        del pass_cache[nid]
    for nid in [n for n in pass_retry_at if n not in keep]:
        del pass_retry_at[nid]


def _update_pass_cache(
    fast_elements: list[OrbitalElement],
    pass_cache: dict[int, PassPrediction | None],
    pass_retry_at: dict[int, float],
    *,
    observer_lat: float,
    observer_lon: float,
    observer_alt_m: float,
    wall: datetime,
    loop_time: float,
    min_elevation_deg: float,
) -> None:
    """Recompute at most ONE watchlisted object's pass prediction this fast tick (M6.8).

    ``pass_cache``/``pass_retry_at`` are mutated in place (NORAD-keyed, loop-scope state owned
    by :func:`celestrak_records`). An object "needs recompute" when (a) it has never been
    predicted yet, (b) its cached prediction is ``None`` (decayed object, or genuinely no pass
    in the search window) and the retry backoff (:data:`PASS_PREDICT_RETRY_S`) has elapsed, or
    (c) its cached pass has a ``set_at`` AND ``wall`` is past it AND the object's CURRENT
    elevation has actually dropped below ``min_elevation_deg`` — gating on the real floor
    crossing (one extra ``propagate`` call, already cheap per-tick) rather than purely on the
    predicted ``set_at`` closes the race where an optimistic-by-a-few-seconds prediction would
    silently swap in next-pass data while the object is still above the floor mid-pass.

    Only ONE element is actually recomputed (the ~24h scan) per tick — amortized so a resync
    that invalidates every cache entry at once never recomputes the whole watchlist in the same
    tick (it ramps up over a few ticks instead). (a) and (c) are treated as URGENT and win the
    slot immediately, in list order; (b) is a lower-priority fallback, only taken when nothing
    urgent needs the slot this tick. Without this priority split, a watchlist heavy in objects
    that genuinely never pass (cached ``None``, endlessly retried every
    :data:`PASS_PREDICT_RETRY_S`) could starve a real object's (c) recompute out of the single
    per-tick slot for its whole below-floor inter-pass gap — by the time the object rises again
    for its next pass, (c)'s own gate (``cur_elev < floor``) can no longer fire, so the cache
    would keep serving the PREVIOUS pass's (by-then-past) rise/culmination/set for the entire
    new pass. Scanning the whole list every tick (rather than stopping at the first (b)
    candidate) costs at most one extra cheap single-instant ``propagate`` call per already-set
    object already being checked for (c) — never an extra ~24h scan.
    """
    retry_candidate: OrbitalElement | None = None
    for element in fast_elements:
        nid = element.norad_id
        if nid not in pass_cache:
            _recompute_pass(
                element,
                pass_cache,
                pass_retry_at,
                observer_lat=observer_lat,
                observer_lon=observer_lon,
                observer_alt_m=observer_alt_m,
                wall=wall,
                loop_time=loop_time,
                min_elevation_deg=min_elevation_deg,
            )
            return
        cached = pass_cache[nid]
        if cached is not None and cached.set_at is not None and wall >= cached.set_at:
            cur_state = propagate(
                element.satrec,
                wall,
                observer_lat_deg=observer_lat,
                observer_lon_deg=observer_lon,
                observer_alt_m=observer_alt_m,
            )
            cur_elev = cur_state.elevation_deg if cur_state is not None else None
            if cur_elev is None or cur_elev < min_elevation_deg:
                _recompute_pass(
                    element,
                    pass_cache,
                    pass_retry_at,
                    observer_lat=observer_lat,
                    observer_lon=observer_lon,
                    observer_alt_m=observer_alt_m,
                    wall=wall,
                    loop_time=loop_time,
                    min_elevation_deg=min_elevation_deg,
                )
                return
        elif (
            cached is None and retry_candidate is None and loop_time >= pass_retry_at.get(nid, 0.0)
        ):
            retry_candidate = element  # lowest priority — only used if nothing urgent is found

    if retry_candidate is not None:
        _recompute_pass(
            retry_candidate,
            pass_cache,
            pass_retry_at,
            observer_lat=observer_lat,
            observer_lon=observer_lon,
            observer_alt_m=observer_alt_m,
            wall=wall,
            loop_time=loop_time,
            min_elevation_deg=min_elevation_deg,
        )


def _recompute_pass(
    element: OrbitalElement,
    pass_cache: dict[int, PassPrediction | None],
    pass_retry_at: dict[int, float],
    *,
    observer_lat: float,
    observer_lon: float,
    observer_alt_m: float,
    wall: datetime,
    loop_time: float,
    min_elevation_deg: float,
) -> None:
    """Run the ~24h pass-prediction scan for one object and cache the result (M6.8)."""
    pred = predict_next_pass(
        element.satrec,
        wall,
        observer_lat_deg=observer_lat,
        observer_lon_deg=observer_lon,
        observer_alt_m=observer_alt_m,
        min_elevation_deg=min_elevation_deg,
    )
    pass_cache[element.norad_id] = pred
    if pred is None:
        pass_retry_at[element.norad_id] = loop_time + PASS_PREDICT_RETRY_S


def _propagate_set(
    subset: list[OrbitalElement],
    *,
    observer_lat: float,
    observer_lon: float,
    observer_alt_m: float,
    at: datetime,
    valid_s: float,
    min_elevation_deg: float,
    predictions: dict[int, PassPrediction] | None = None,
) -> tuple[list[TrackRecord], int]:
    """Propagate a subset to ``at``; return ``(above-horizon records, prop_skipped)``.

    Lifted verbatim from the original inner ``for element in elements:`` block: a ``None``
    propagation (NaN/decayed) is skipped and counted; an object below the elevation floor is
    dropped (ORBIT-FR-007). Shared by both tiers so they apply identical filtering.

    ``predictions`` (M6.8) is a NORAD-keyed cache of next/in-progress passes, looked up per
    element and threaded through to :func:`element_to_record`. Defaults to ``None`` — the slow
    tier never computes predictions, so its call site is byte-identical to before this slice.
    """
    out: list[TrackRecord] = []
    skipped = 0
    for element in subset:
        pred = predictions.get(element.norad_id) if predictions else None
        record = element_to_record(
            element,
            observer_lat=observer_lat,
            observer_lon=observer_lon,
            observer_alt_m=observer_alt_m,
            at=at,
            valid_s=valid_s,
            prediction=pred,
        )
        if record is None:
            skipped += 1
            continue
        elevation = record.attributes["elevation_deg"]
        if not isinstance(elevation, (int, float)) or elevation < min_elevation_deg:
            continue  # below the horizon floor — not emitted (ORBIT-FR-007)
        out.append(record)
    return out, skipped


# --- Records stream + bus pump -------------------------------------------------


async def celestrak_records(
    provider: CelestrakProvider,
    *,
    groups: Sequence[str],
    observer_lat: float,
    observer_lon: float,
    observer_alt_m: float,
    min_elevation_deg: float = 10.0,
    sync_s: float = 21600.0,
    propagate_s: float = 15.0,
    valid_s: float = 30.0,
    now_fn: Callable[[], datetime] | None = None,
    watchlist_source: Callable[[], set[int]] | None = None,
    propagate_fast_s: float = 2.0,
    watchlist_refresh_s: float = 30.0,
) -> AsyncIterator[Record]:
    """Yield the CelesTrak record stream: ``starting``, sync GP, then propagate each tick.

    The sync fetches each group's OMM JSON no faster than ``sync_s`` (default 6 h, well above
    CelesTrak's 2 h refresh — §38), rebuilding the cached element set; on a non-retryable
    301/403/404 (:class:`CelestrakNoRetry`) or any fetch/parse error the **last-good** cache
    is kept and the source reports ``degraded`` rather than going dark.

    Between syncs the set is propagated on **two tiers** over a disjoint partition (ORBIT-FR-011).
    ``watchlist_source`` is an injected *blocking* reader returning the watchlisted NORAD ids
    (``orbital:celestrak:<norad>`` keys from the persistence store); it is driven via
    ``asyncio.to_thread`` so it never blocks the loop, and re-read every ``watchlist_refresh_s``
    (default 30 s) so toggling a satellite moves it between tiers **without an adapter restart**.
    Watchlisted objects propagate on the **fast** cadence ``propagate_fast_s`` (default 2 s) for
    smooth tracks; the broad catalog rides the existing **slow** cadence ``propagate_s`` (default
    15 s) which also emits the single ``connected`` status. Because the partition is a strict set
    difference on NORAD id, a watchlisted object is emitted by the fast tier **only** — never
    double-emitted. When ``watchlist_source`` is ``None`` (persistence off) or the watchlist is
    empty the fast tier collapses and the stream is byte-identical to the single-cadence M6.5
    path. Each tick keeps only objects above ``min_elevation_deg`` (ORBIT-FR-007). A missing
    ``sgp4`` parser propagates as :class:`Sgp4Unavailable` to :func:`run_celestrak` (the
    capability gate). Failure isolation throughout (PRD §17.4/§37) — a watchlist read error is
    logged and treated as empty, never crashing the adapter.

    **Pass prediction (M6.8, PRD §32 #18/#19):** the fast tier additionally maintains a NORAD-
    keyed cache of the next/in-progress observer pass (rise/culmination/set) for each watchlisted
    object, computed by :func:`aether.orbital.pass_prediction.predict_next_pass` and attached as
    ``pass_culmination_at``/``pass_max_elevation_deg``/``pass_rise_at``/``pass_set_at``
    attributes (see :func:`element_to_record`). The cache lives for the generator's connection
    lifetime (rebuilt on reconnect) and is recomputed lazily — at most one watchlisted object's
    ~24h scan per fast tick (see :func:`_update_pass_cache`) — never the broad catalog, and never
    the whole watchlist at once even after a resync invalidates every entry.
    """
    now = now_fn or _now
    yield _status("starting", now())
    backoff = INITIAL_BACKOFF_S
    loop = asyncio.get_event_loop()
    elements: list[OrbitalElement] = []
    fast_elements: list[OrbitalElement] = []
    slow_elements: list[OrbitalElement] = []
    fast_norads: set[int] = set()
    fast_above = 0
    pass_cache: dict[int, PassPrediction | None] = {}  # norad -> next/in-progress pass (M6.8)
    pass_retry_at: dict[int, float] = {}  # norad -> earliest loop.time() to retry a None cache
    last_record_at: datetime | None = None
    received = 0
    rejected_total = 0
    next_sync = 0.0  # force a sync on the first iteration (monotonic clock)
    next_slow = 0.0
    next_fast = math.inf  # active only while the fast (watchlisted) set is non-empty
    next_watch = 0.0 if watchlist_source is not None else math.inf  # inf ⇒ identical-to-today

    while True:
        # Capture the monotonic clock ONCE per iteration so every deadline check below uses the
        # same reference; a tier that newly activates is fired from THIS value (see the partition
        # blocks), while a tier reschedules from loop.time() post-work so cadence trails completion.
        t = loop.time()

        # --- Sync the element set when due (or on first pass) ---
        if t >= next_sync:
            wall = now()
            new_elements: list[OrbitalElement] = []
            sync_skipped = 0
            sync_failed = False
            for group in groups:
                try:
                    rows = await provider.fetch_group(group)
                except Sgp4Unavailable:
                    raise  # propagate to run_celestrak → one offline status (capability gate)
                except CelestrakNoRetry as exc:
                    log.warning("CelesTrak group %s abandoned (%s); keeping last-good", group, exc)
                    sync_failed = True
                    continue
                except Exception as exc:  # a bad fetch/body must not crash the adapter
                    log.warning("CelesTrak group %s fetch failed (%s); degrading", group, exc)
                    sync_failed = True
                    continue
                try:
                    built, skipped = build_satrecs(rows, group=group)
                except Sgp4Unavailable:
                    raise
                new_elements.extend(built)
                sync_skipped += skipped

            if new_elements or not sync_failed:
                # A successful sync (even of an empty group) replaces the cache; a fully
                # failed sync keeps the prior last-good set so the map does not go dark.
                elements = new_elements
            rejected_total += sync_skipped
            next_sync = loop.time() + sync_s
            if sync_failed and not elements:
                yield _status(
                    "degraded",
                    wall,
                    records_received=received,
                    records_rejected=rejected_total,
                    error_code="SyncFailed",
                    error_summary="CelesTrak GP sync failed and no last-good cache",
                )
                sleep_for, backoff = _backoff(backoff)
                await asyncio.sleep(sleep_for)
                continue
            backoff = INITIAL_BACKOFF_S
            # Re-partition against the (possibly new) catalog; newly-non-empty fast tier fires
            # this same wake (next_fast=t), an emptied one parks (inf) so it never wakes idle.
            fast_elements, slow_elements = _partition(elements, fast_norads)
            _prune_pass_cache(pass_cache, pass_retry_at, fast_elements)
            if fast_elements and next_fast == math.inf:
                next_fast = t
            elif not fast_elements:
                next_fast, fast_above = math.inf, 0

        # --- Re-read the watchlist (moves objects between tiers without a restart) ---
        if watchlist_source is not None and t >= next_watch:
            try:
                fast_norads = await asyncio.to_thread(watchlist_source)
            except Exception:
                log.warning("CelesTrak watchlist refresh failed; treating as empty", exc_info=True)
                fast_norads = set()
            next_watch = loop.time() + watchlist_refresh_s
            fast_elements, slow_elements = _partition(elements, fast_norads)
            _prune_pass_cache(pass_cache, pass_retry_at, fast_elements)
            # Newly-active fast tier fires this same wake (preserve phase if already active).
            if fast_elements and next_fast == math.inf:
                next_fast = t
            elif not fast_elements:
                next_fast, fast_above = math.inf, 0

        # --- Fast tier: the watchlisted subset only (smooth tracks); no status ---
        if t >= next_fast:
            wall = now()
            # Pass prediction (M6.8): at most one watchlisted object's ~24h scan per tick.
            _update_pass_cache(
                fast_elements,
                pass_cache,
                pass_retry_at,
                observer_lat=observer_lat,
                observer_lon=observer_lon,
                observer_alt_m=observer_alt_m,
                wall=wall,
                loop_time=t,
                min_elevation_deg=min_elevation_deg,
            )
            predictions: dict[int, PassPrediction] = {}
            for element in fast_elements:
                pred = pass_cache.get(element.norad_id)
                if pred is not None:
                    predictions[element.norad_id] = pred
            recs, _skipped = _propagate_set(
                fast_elements,
                observer_lat=observer_lat,
                observer_lon=observer_lon,
                observer_alt_m=observer_alt_m,
                at=wall,
                valid_s=valid_s,
                min_elevation_deg=min_elevation_deg,
                predictions=predictions,
            )
            fast_above = len(recs)
            for r in recs:
                received += 1
                last_record_at = wall
                yield r
            next_fast = loop.time() + propagate_fast_s

        # --- Slow tier: the broad catalog minus the watchlist; emits the single status ---
        if t >= next_slow:
            wall = now()
            recs, skipped_prop = _propagate_set(
                slow_elements,
                observer_lat=observer_lat,
                observer_lon=observer_lon,
                observer_alt_m=observer_alt_m,
                at=wall,
                valid_s=valid_s,
                min_elevation_deg=min_elevation_deg,
            )
            for r in recs:
                received += 1
                last_record_at = wall
                yield r
            yield _status(
                "connected",
                wall,
                records_received=received,
                records_rejected=rejected_total,
                last_record_at=last_record_at,
                attributes={
                    "tracked_objects": len(elements),  # TOTAL synced catalog (unchanged meaning)
                    "above_horizon": len(recs),  # slow tier above-horizon this tick
                    "emitted_this_tick": len(recs),
                    "prop_skipped": skipped_prop,
                    "min_elevation_deg": min_elevation_deg,
                    "groups": list(groups),
                    "watchlisted": len(fast_norads),  # orbital watchlist keys read from the DB
                    "fast_tracked": len(fast_elements),  # watchlist ∩ synced catalog
                    "slow_tracked": len(slow_elements),
                    "fast_above_horizon": fast_above,  # watched subset above horizon (last tick)
                    "propagate_fast_s": propagate_fast_s,  # cadence transparency
                },
            )
            next_slow = loop.time() + propagate_s

        # Sleep to the nearest active deadline; inactive tiers are math.inf and never picked.
        deadline = min(next_sync, next_watch, next_slow, next_fast)
        await asyncio.sleep(max(0.0, deadline - loop.time()))


async def run_celestrak(
    cfg: Settings,
    ready: asyncio.Event,
    *,
    provider: CelestrakProvider | None = None,
    propagate_s: float | None = None,
) -> None:
    """Pump the CelesTrak orbital stream onto the bus until cancelled (PRD §17.1).

    Waits for the subscriber, then publishes :func:`celestrak_records`. A missing ``sgp4``
    parser (the optional ``[orbital]`` dep) is reported once as an ``offline`` source status
    and the task exits cleanly — a missing dependency will not self-heal, so we do not spin
    (the GLM/FIRMS stance, PRD §2/§37). A broker drop triggers a jittered reconnect with a
    FRESH records generator per connection (the PEP 525 lesson). The provider is stateless
    across reconnects and injectable for tests.
    """
    await ready.wait()
    resolved_prop = propagate_s if propagate_s is not None else cfg.celestrak_propagate_s
    backoff = INITIAL_BACKOFF_S
    while True:
        try:
            async with connect(cfg, identifier="aether-celestrak") as bus:
                backoff = INITIAL_BACKOFF_S  # reset once connected
                prov = provider if provider is not None else build_provider(cfg)
                # Two-tier (ORBIT-FR-011) only when persistence is on: gating on cfg.persist
                # (not just relying on list_watchlist→[]) guarantees persist-off ⇒ ZERO
                # watchlist I/O ⇒ byte-identical to the single-cadence path, and stops a
                # leftover/foreign aether.db from silently fast-tracking objects.
                watchlist_source = (
                    functools.partial(_read_watchlisted_norads, cfg.db_path)
                    if cfg.persist
                    else None
                )
                log.info(
                    "CelesTrak adapter -> %s (groups %s, sync %.0fs, propagate %.0fs, "
                    "fast %.1fs, two-tier %s, min el %.1f)",
                    prov.name,
                    ",".join(cfg.celestrak_groups),
                    cfg.celestrak_sync_s,
                    resolved_prop,
                    cfg.celestrak_propagate_fast_s,
                    watchlist_source is not None,
                    cfg.celestrak_min_elevation_deg,
                )
                try:
                    async for record in celestrak_records(
                        prov,
                        groups=cfg.celestrak_groups,
                        observer_lat=cfg.celestrak_observer_lat,
                        observer_lon=cfg.celestrak_observer_lon,
                        observer_alt_m=cfg.celestrak_observer_alt_m,
                        min_elevation_deg=cfg.celestrak_min_elevation_deg,
                        sync_s=cfg.celestrak_sync_s,
                        propagate_s=resolved_prop,
                        valid_s=cfg.celestrak_valid_s,
                        watchlist_source=watchlist_source,
                        propagate_fast_s=cfg.celestrak_propagate_fast_s,
                        watchlist_refresh_s=cfg.celestrak_watchlist_refresh_s,
                    ):
                        await bus.publish_record(record)
                except Sgp4Unavailable as exc:
                    log.error("CelesTrak SGP4 unavailable: %s", exc)
                    await bus.publish_record(
                        _status(
                            "offline",
                            _now(),
                            error_code="Sgp4Unavailable",
                            error_summary='sgp4 not installed — `pip install "aether[orbital]"`',
                            attributes={"detail": str(exc)[:160]},
                        )
                    )
                    return  # dependency won't self-heal; don't spin
                return  # generator exhausted (only on cancellation in practice)
        except aiomqtt.MqttError as exc:
            sleep_for, backoff = _backoff(backoff)
            log.warning("CelesTrak lost broker (%s); reconnecting in %.1fs", exc, sleep_for)
            await asyncio.sleep(sleep_for)

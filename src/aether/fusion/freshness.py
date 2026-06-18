"""Per-source freshness windows and classification (PRD §15.4).

The fusion engine has to answer "is this contributor's observation still live,
merely stale, or so old it should no longer count?" — and it must answer it the
same way every time, against an explicit ``now`` (FUSION-FR-007), never a hidden
wall clock. This module is the pure, table-driven core of that decision.

Windows are keyed by *source name*, not by an inferred source class: there is no
``(local_rf, track_type) -> class`` mapping that could quietly grow into the kind
of per-source branching PRD §37 forbids in the backend. ``local_rf`` (read from
provenance) drives *precedence* ranking elsewhere; it never picks a window here.
Known source names are seeded from the PRD §15.4 ADS-B windows; anything unknown
falls back to a conservative network-grade window so a new feed degrades sanely
rather than being trusted like a local radio.

Freshness is measured on ``observed_at`` — when the *source* saw the world — not
``received_at`` (PRD §8.4, §15.4): a feed that batches or lags must not look
fresher than the airframe actually is.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

#: Live (act on it), stale (keep showing, labeled), or expired (drop from fusion).
FreshnessClass = Literal["live", "stale", "expired"]


@dataclass(frozen=True)
class FreshnessWindow:
    """Age thresholds (seconds) bounding the live/stale/expired bands.

    ``stale_s`` is informational (the live→stale boundary is read straight off
    ``live_s``); only ``live_s`` and ``expire_s`` gate :func:`classify`.
    """

    live_s: float
    stale_s: float
    expire_s: float


#: Source-name → window. ``local_adsb`` is the readsb SOURCE; ``demo`` is the
#: no-hardware demo's LOCAL leg (both ADS-B local: live 0-5s / stale 5-30s /
#: expire 60s). ``network_adsb`` is the Internet ADS-B provider SOURCE and
#: ``demo-net`` is the demo's NETWORK leg — both on the network-grade ADS-B
#: window (live 0-15s / stale 15-60s / expire 120s), looser than local because a
#: feed batches and lags more than the operator's own antenna (PRD §15.4).
DEFAULT_FRESHNESS: dict[str, FreshnessWindow] = {
    "local_adsb": FreshnessWindow(5.0, 30.0, 60.0),
    "demo": FreshnessWindow(5.0, 30.0, 60.0),
    "network_adsb": FreshnessWindow(15.0, 60.0, 120.0),
    "demo-net": FreshnessWindow(15.0, 60.0, 120.0),
}

#: Conservative window for any source not in the table — treated network-grade so
#: an unknown feed is never trusted as freshly as a local radio (PRD §15.4).
DEFAULT_FRESHNESS_FALLBACK = FreshnessWindow(15.0, 60.0, 120.0)

#: How far ahead of ``now`` an ``observed_at`` may sit before we stop trusting it.
#: A few seconds of clock skew between the station and a feed is benign and clamps
#: to age 0 (live); anything further ahead is treated as a *huge* age so the
#: contributor classifies as expired and its group can be reaped. Without this
#: cap a skewed/adversarial far-future timestamp would clamp to live forever,
#: pinning the fusion group and its track in memory permanently — the unbounded
#: growth this guards against (PRD §37 failure isolation + resource bounds).
FUTURE_SKEW_TOLERANCE_S = 5.0


def window_for(
    source: str,
    table: dict[str, FreshnessWindow],
    fallback: FreshnessWindow,
) -> FreshnessWindow:
    """Return the window for ``source``, or ``fallback`` if it is not seeded."""
    return table.get(source, fallback)


def age_seconds(observed_at: datetime, now: datetime) -> float:
    """Age of an observation at ``now``, in seconds.

    Benign clock skew (a source's ``observed_at`` up to :data:`FUTURE_SKEW_TOLERANCE_S`
    ahead of ``now``) clamps to zero rather than going negative, so a slightly
    skewed contributor reads as *live* instead of producing a nonsensical negative
    age. But an ``observed_at`` *further* in the future is suspicious — it would
    otherwise clamp to live forever and pin its fusion group and track in memory
    permanently — so it is reported as a huge age, classifying as expired and
    letting the group be reaped (PRD §37 failure isolation + resource bounds).
    """
    delta_s = (now - observed_at).total_seconds()
    if delta_s < 0.0:
        if -delta_s <= FUTURE_SKEW_TOLERANCE_S:
            return 0.0
        # Magnitude of the future skew = how stale to treat it; always positive
        # and large enough to land past any expire window.
        return -delta_s
    return delta_s


def classify(age_s: float, window: FreshnessWindow) -> FreshnessClass:
    """Bucket an age into live / stale / expired over half-open intervals.

    ``[0, live_s)`` → live, ``[live_s, expire_s)`` → stale, ``[expire_s, ∞)`` →
    expired. The boundaries are fixed and tested: ``age == live_s`` is *stale*
    and ``age == expire_s`` is *expired*.
    """
    if age_s < window.live_s:
        return "live"
    if age_s < window.expire_s:
        return "stale"
    return "expired"

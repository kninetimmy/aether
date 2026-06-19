"""Persist-time sampling gate (PRD §19.5).

The adapter-edge ``ThrottleGate`` caps each source at ~1 update per identity per
second on the *bus*; this coarser gate runs at the *persist* edge to bound DB
growth proactively (PRD §19.5), complementing the retention manager's reactive
downsample-under-pressure (PRD §19.4 ladder step 2). It admits at most one
observation per ``(source, identity)`` per per-source cadence window and drops
the rest before they reach the write queue. Records flagged high-fidelity (an
``emergency`` tag) always persist, regardless of cadence (PRD §19.5
"Watchlisted/emergency aircraft: higher fidelity while alert is active").

The persistence subscriber sees *raw per-source* records — fusion happens in
live state, a separate consumer — so one aircraft heard by both the local and
network feeds arrives as two records that share a ``correlation_key`` but carry
different ``source`` values. Keying the gate by ``(source, identity)`` keeps each
provenance stream on its own cadence (a slow 15 s network point can't suppress a
5 s local one), preserving the per-provenance history the store already holds.

A cadence of ``0`` disables the time gate for that source — every record is
admitted. That is the APRS rule ("persist every unique packet", already
edge-throttled and de-duplicated) and the safe default for any untuned source:
aether never *silently* thins a feed it wasn't told how to sample.

Memory is bounded exactly like the edge gate — time eviction (entries idle past
``ttl_s`` are forgotten) plus a size backstop — so a multi-day soak on a busy
channel can't grow the gate's table (PRD §17.3, §37). Cadence-0 sources are
never recorded, so they add nothing to that table.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime

#: Minimum entry lifetime before time eviction. An identity stays remembered at
#: least this long (and never less than its own cadence) so eviction can't race
#: the cadence it backs. One hour matches the edge gate's floor.
_GATE_MIN_TTL_S = 3600.0
#: Hard cap on tracked ``(source, identity)`` keys. Higher than the per-adapter
#: edge gate's cap because this one gate aggregates every persisted source; the
#: oldest entries are dropped past it as a junk-flood backstop (PRD §37).
_GATE_MAX_ENTRIES = 16_384


class SampleGate:
    """Per-``(source, identity)`` cadence gate bounding what reaches the store.

    Construct with a ``cadence`` map (source name → minimum seconds between
    persisted observations) and a ``default_s`` for sources absent from it. Call
    :meth:`admit` once per candidate record; ``True`` means persist it.
    """

    def __init__(
        self,
        cadence: Mapping[str, float],
        *,
        default_s: float = 0.0,
        ttl_s: float | None = None,
    ) -> None:
        self._cadence = dict(cadence)
        self._default_s = default_s
        # An entry outlives the longest cadence it gates and the min-TTL floor, so
        # time eviction never discards a key the cadence still needs.
        longest = max([default_s, *self._cadence.values()], default=0.0)
        floor = ttl_s if ttl_s is not None else _GATE_MIN_TTL_S
        self._ttl_s = max(longest, floor)
        self._last: dict[tuple[str, str], datetime] = {}

    def _cadence_for(self, source: str) -> float:
        return self._cadence.get(source, self._default_s)

    def admit(self, *, identity: str, source: str, now: datetime, high_fidelity: bool) -> bool:
        """Return ``True`` to persist this observation, ``False`` to drop it.

        ``identity`` is the fused key (``correlation_key`` or, when unfused, the
        record id). ``high_fidelity`` (an emergency-tagged track) always admits.
        A cadence of ``0`` for ``source`` admits unconditionally and records
        nothing, so cadence-0 sources never grow the gate's table.
        """
        cadence = self._cadence_for(source)
        if cadence <= 0 and not high_fidelity:
            return True  # no time gate for this source; nothing to remember
        key = (source, identity)
        self._evict(now, incoming=key)
        last = self._last.get(key)
        due = last is None or (now - last).total_seconds() >= cadence
        if high_fidelity or due:
            self._last[key] = now
            return True
        return False

    def _evict(self, now: datetime, *, incoming: tuple[str, str]) -> None:
        """Drop dead/overflow entries so the gate's table stays bounded (PRD §37).

        Time eviction forgets any key idle longer than ``ttl_s``; the size
        backstop then drops the oldest entries so that, after the caller inserts
        ``incoming``, the table holds at most :data:`_GATE_MAX_ENTRIES` — one slot
        of headroom is reserved only when ``incoming`` is a genuinely new key.
        """
        dead = [
            key for key, last in self._last.items() if (now - last).total_seconds() > self._ttl_s
        ]
        for key in dead:
            del self._last[key]
        reserve = 0 if incoming in self._last else 1
        overflow = len(self._last) + reserve - _GATE_MAX_ENTRIES
        if overflow > 0:
            oldest = sorted(self._last, key=self._last.__getitem__)[:overflow]
            for key in oldest:
                del self._last[key]

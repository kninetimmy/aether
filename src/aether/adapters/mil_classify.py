"""Military Mode-S classification basis (PRD §11.5, MIL-FR-001..005).

Pure, hardware-free decision about whether an ADS-B track is military and on what
*basis*, kept in one module so both ADS-B edges — local :mod:`~aether.adapters.readsb`
and the network :mod:`~aether.adapters.adsb_provider` — classify identically and
the rule can never drift between them.

Two — and only two — bases may set ``military`` (MIL-FR-004 forbids any movement,
callsign, route, or appearance heuristic):

- **provider** — the feed's own database flag. adsb.fi / ADS-B-Exchange expose a
  ``dbFlags`` bitmask whose bit 0 means "military" (the same bit readsb/tar1090 set
  from their aircraft database). Re-verify at build time (PRD §38).
- **address_block** — the ICAO 24-bit address falls inside a configured military
  allocation range. The ranges are **operator-supplied** (``AETHER_MIL_ICAO_BLOCKS``,
  parsed by :func:`parse_ranges`): the repo ships the *mechanism*, not a baked-in
  range table, so a stale or wrong allocation can never silently mislabel a civil
  airframe (honest-labeling decision; PRD §11.5). With no blocks configured this
  basis simply never fires — visibly inert, not a hidden guess.

Neither basis is authoritative ground truth — a provider DB can be wrong or stale
and an allocation block can hold non-military aircraft or be reassigned — so
``confidence`` never reaches ``high``; the UI must avoid certainty language
regardless (MIL-FR-005). When there is no positive evidence the classifier returns
``None`` rather than decorating every civil track with ``military=False`` noise.
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass

from aether.schema.records import Classification

log = logging.getLogger(__name__)

#: ADS-B-Exchange / readsb ``dbFlags`` bit 0 == military (PRD §38; re-verify).
_DBFLAG_MILITARY = 0x1


@dataclass(frozen=True)
class IcaoRange:
    """An inclusive military ICAO 24-bit address allocation block."""

    start: int  # inclusive
    end: int  # inclusive
    note: str | None = None

    def contains(self, addr: int) -> bool:
        return self.start <= addr <= self.end


def parse_ranges(spec: str) -> tuple[IcaoRange, ...]:
    """Parse the ``AETHER_MIL_ICAO_BLOCKS`` config string into ranges.

    Format: comma-separated ``start-end`` hex pairs, e.g.
    ``"adf7c8-afffff, 43c000-43cfff"``. A bare ``abcdef`` is the one-element range
    ``abcdef-abcdef``. Whitespace around tokens is ignored. A malformed entry is
    logged and skipped — a bad config line must degrade visibly, never crash the
    adapter (PRD §37).
    """
    ranges: list[IcaoRange] = []
    for raw in spec.split(","):
        token = raw.strip()
        if not token:
            continue
        try:
            if "-" in token:
                lo_s, hi_s = token.split("-", 1)
                lo, hi = int(lo_s, 16), int(hi_s, 16)
            else:
                lo = hi = int(token, 16)
            if lo < 0 or lo > hi:
                raise ValueError(f"bad range bounds: {token!r}")
        except ValueError:
            log.warning("ignoring malformed mil ICAO block %r", token)
            continue
        ranges.append(IcaoRange(start=lo, end=hi))
    return tuple(ranges)


def _provider_military(db_flags: object) -> bool:
    """True when the provider's ``dbFlags`` bitmask sets the military bit.

    ``bool`` is an ``int`` subclass but is never a real bitmask, so it is rejected;
    anything non-integer (a string, ``None``) means "no provider opinion".
    """
    if isinstance(db_flags, bool) or not isinstance(db_flags, int):
        return False
    return bool(db_flags & _DBFLAG_MILITARY)


def _address_military(icao_hex: str | None, ranges: Sequence[IcaoRange]) -> bool:
    """True when ``icao_hex`` parses as hex and falls inside any configured range."""
    if not icao_hex or not ranges:
        return False
    try:
        addr = int(icao_hex, 16)
    except ValueError:
        return False
    return any(r.contains(addr) for r in ranges)


def classify_military(
    icao_hex: str | None,
    *,
    db_flags: object = None,
    non_icao: bool = False,
    ranges: Sequence[IcaoRange] = (),
) -> Classification | None:
    """Classify one airframe as military with its basis, or ``None`` if unclassified.

    Only the provider flag and a configured address-block match may set ``military``
    (MIL-FR-004). A ``non_icao`` address (TIS-B / ``~``-prefixed) is not a real ICAO
    allocation, so address-block matching is skipped for it; a provider flag still
    applies. Confidence is capped below ``high`` because no basis is authoritative
    (MIL-FR-005): provider report → ``medium``, address block alone → ``low``, and
    both corroborating → ``medium`` (two non-authoritative signals do not become
    certainty).
    """
    provider = _provider_military(db_flags)
    address = (not non_icao) and _address_military(icao_hex, ranges)
    if not provider and not address:
        return None
    if provider and address:
        return Classification(
            military=True,
            basis="both",
            confidence="medium",
            note="provider DB flag + ICAO address block",
        )
    if provider:
        return Classification(
            military=True,
            basis="provider",
            confidence="medium",
            note="provider database flag",
        )
    return Classification(
        military=True,
        basis="address_block",
        confidence="low",
        note="ICAO address block",
    )

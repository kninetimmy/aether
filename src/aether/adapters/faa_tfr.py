"""FAA Temporary Flight Restriction (TFR) adapter (PRD §11.13, §18.10, M6.1).

Two-step poll of the **official FAA TFR service** (re-verified at build time per
PRD §38):

1. ``GET https://tfr.faa.gov/tfrapi/exportTfrList`` → a JSON array of light list
   rows (``notam_id``, ``type``, ``facility``, ``state``, ``description``,
   ``creation_date``) — *what TFRs exist right now* (AIRSPACE-FR-001).
2. ``GET https://tfr.faa.gov/download/detail_<notam>.xml`` (the ``notam_id``
   ``6/9513`` → ``6_9513``) → the ``<XNOTAM-Update>`` detail with geometry,
   altitudes, and effective/expiry times.

Each detail is normalized to a schema-v2 ``GeoFeatureRecord`` (``feature_type=
"tfr"``) carrying a closed-ring ``Polygon`` (one area) or ``MultiPolygon`` (several
areas), ``valid_from``/``valid_until`` (AIRSPACE-FR-002, *time-bounded*), the
original NOTAM identifiers/altitudes/purpose (AIRSPACE-FR-006, *original text
retained*), and the honest caveat that aether is **not a flight-planning product**
(AIRSPACE-FR-007). A detail whose geometry cannot be parsed is emitted as a textual
``EventRecord`` instead of an invented shape (§18.10 — *mark malformed geometry as a
textual event*).

Coordinates in the FAA feed are decimal degrees with a hemisphere suffix
(``30.40124305N`` / ``081.41470538W``); ``dateEffective``/``dateExpire`` are **local**
to ``codeTimeZone`` (e.g. ``EDT``) and are offset-converted to UTC, with the raw local
strings + zone retained verbatim so nothing is silently mislabeled (PRD §37).

Read-only public data: the FAA TFR service needs no key. aether only *fetches* — a
detail per listed TFR, bounded by ``max_details_per_poll`` and the optional
``states`` pre-filter so a nationwide list never hammers the server — and never
transmits. The product is **not authoritative** for airspace; the FAA is
(PRD §11.2/§37); records carry FAA attribution and the source NOTAM verbatim.

Responsibility split mirrors :mod:`aether.adapters.usgs`:

- :class:`FaaTfrProvider` / :class:`FaaTfrHttpProvider` — fetch the raw list +
  detail bytes (the live HTTPS service, or the in-process ``fake`` feeder).
- :func:`parse_detail` — pure ``<XNOTAM-Update>`` bytes → ``GeoFeatureRecord`` (or
  ``EventRecord`` for unparseable geometry, or ``None`` for a non-active record).
- :func:`tfr_records` — the ``records()`` contract: ``starting``, then a poll loop
  with a list fetch, revision dedupe, a bounded detail-fetch budget, AOI filtering,
  and ``degraded``-on-failure isolation (a failed fetch keeps the last good TFRs).
- :func:`run_faa_tfr` — bus connection + jittered exponential backoff on broker loss.
"""

import asyncio
import functools
import json
import logging
import random
import re
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime, timedelta, timezone
from typing import Any, Literal, Protocol

import aiomqtt

from aether.alerts.geo import haversine_m
from aether.bus.client import connect
from aether.config import Settings
from aether.schema.geometry import GeoJSONGeometry, MultiPolygon, Polygon, Position
from aether.schema.provenance import Provenance
from aether.schema.records import EventRecord, GeoFeatureRecord, Record, SourceStatusRecord

log = logging.getLogger(__name__)

#: Stable source name + retained health-record id (PRD §23 status stream). Records
#: publish to ``aether/v2/records/faa_tfr`` (PRD §17/§23, derived from this name).
SOURCE = "faa_tfr"
STATUS_ID = f"source_status:{SOURCE}"

#: FAA attribution carried on every record + status so provenance stays honest (§11.2).
ATTRIBUTION = "FAA Temporary Flight Restrictions (tfr.faa.gov)"
#: The non-negotiable honesty caveat for this layer (AIRSPACE-FR-007).
CAVEAT = "Not a flight-planning product; consult official FAA sources before flight."

#: Default service host; ``/tfrapi/exportTfrList`` and ``/download/detail_<n>.xml`` hang
#: off it. ``fake``/``demo`` selects the no-hardware feeder instead.
DEFAULT_BASE_URL = "https://tfr.faa.gov"

#: 1 nautical mile in metres (AOI radius is configured in NM, distances in metres).
_M_PER_NM = 1852.0

#: Jittered exponential backoff bounds — shared shape with every other adapter (§17.1).
INITIAL_BACKOFF_S = 1.0
MAX_BACKOFF_S = 60.0

#: Provider-name aliases selecting the in-process no-hardware feeder.
_FAKE_PROVIDER_NAMES = frozenset({"fake", "demo"})

#: Hard cap on a single response body; a runaway is truncated rather than read unbounded.
MAX_RESPONSE_BYTES = 8 * 1024 * 1024

#: Decimal-degrees-with-hemisphere coordinate, e.g. ``30.32166667N`` / ``081.435W``.
_COORD_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([NSEWnsew])\s*$")

#: US TFR time-zone abbreviations → fixed UTC offset (hours). The FAA emits the
#: DST-aware abbreviation (``EDT`` in summer, ``EST`` in winter), so the mapping is
#: unambiguous. An unmapped zone yields ``None`` valid times (raw strings are kept) —
#: we never assert a UTC time we cannot justify (PRD §37 fail-visibly).
_TZ_OFFSETS_H: dict[str, int] = {
    "UTC": 0, "GMT": 0, "Z": 0,
    "AST": -4, "ADT": -3,
    "EST": -5, "EDT": -4,
    "CST": -6, "CDT": -5,
    "MST": -7, "MDT": -6,
    "PST": -8, "PDT": -7,
    "AKST": -9, "AKDT": -8,
    "HST": -10, "HDT": -9,
    "SST": -11, "CHST": 10,
}  # fmt: skip

#: CFR section (TfrNot/codeType) → human regulatory category (best-effort label only).
_CFR_LABELS: dict[str, str] = {
    "91.137": "Hazards/disaster (91.137)",
    "91.138": "Hazards (91.138)",
    "91.141": "VIP movement (91.141)",
    "91.143": "Space operations (91.143)",
    "91.145": "Aerial event (91.145)",
    "99.7": "Security (99.7)",
}


def _now() -> datetime:
    return datetime.now(UTC)


def _backoff(delay: float) -> tuple[float, float]:
    """Full-jitter backoff: sleep a random slice of ``delay``, then double it (capped)."""
    capped = min(delay, MAX_BACKOFF_S)
    return random.uniform(0.0, capped), min(capped * 2.0, MAX_BACKOFF_S)


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


# --- Provider (raw list + detail fetch) ----------------------------------------


class FaaTfrProvider(Protocol):
    """A source of FAA TFR list rows + per-NOTAM detail XML."""

    name: str

    async def fetch_list(self) -> list[dict[str, Any]]: ...

    async def fetch_detail(self, notam_id: str) -> bytes: ...


def notam_to_path(notam_id: str) -> str:
    """``6/9513`` → ``6_9513`` for the ``detail_<n>.xml`` filename (FAA convention)."""
    return notam_id.strip().replace("/", "_")


def _blocking_get(url: str, timeout_s: float) -> bytes:
    if not url.startswith("https://"):  # public FAA service is https; never downgrade
        raise ValueError(f"refusing non-https FAA TFR URL: {url!r}")
    req = urllib.request.Request(url, headers={"User-Agent": "aether-faa-tfr/1.0"})
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:  # noqa: S310 (https-checked)
        return bytes(resp.read(MAX_RESPONSE_BYTES + 1))


async def _default_fetch(url: str, *, timeout_s: float) -> bytes:
    # Blocking urllib off the event loop (mirrors the USGS provider); no extra dep.
    return await asyncio.to_thread(_blocking_get, url, timeout_s)


class FaaTfrHttpProvider:
    """Fetch the FAA TFR list + detail XML over HTTPS (AIRSPACE-FR-001).

    ``fetch`` is injectable so tests drive canned bytes with no network. The default
    GETs ``<base>/tfrapi/exportTfrList`` (JSON) and ``<base>/download/detail_<n>.xml``.
    """

    name = "faa_tfr"

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

    async def fetch_list(self) -> list[dict[str, Any]]:
        raw = await self._fetch(f"{self._base}/tfrapi/exportTfrList")
        data = json.loads(raw)
        if not isinstance(data, list):
            raise ValueError("FAA TFR list did not return a JSON array")
        return [row for row in data if isinstance(row, dict)]

    async def fetch_detail(self, notam_id: str) -> bytes:
        return await self._fetch(f"{self._base}/download/detail_{notam_to_path(notam_id)}.xml")


def build_provider(cfg: Settings) -> FaaTfrProvider:
    """Resolve the configured FAA TFR provider (live host, or the fake feeder)."""
    base = cfg.faa_tfr_base_url.strip()
    if base.lower() in _FAKE_PROVIDER_NAMES:
        from aether.adapters.faa_tfr_fake_feeder import FakeFaaTfrProvider

        # Place canned TFRs relative to the configured AOI center so the demo renders.
        return FakeFaaTfrProvider(
            center_lat=cfg.faa_tfr_center_lat, center_lon=cfg.faa_tfr_center_lon
        )
    return FaaTfrHttpProvider(base, timeout_s=cfg.faa_tfr_timeout_s)


# --- Parsing helpers (pure) ----------------------------------------------------


def _text(el: ET.Element | None, path: str) -> str | None:
    """Stripped text of ``el.find(path)``; ``None`` when missing/empty."""
    if el is None:
        return None
    found = el.find(path)
    if found is None or found.text is None:
        return None
    text = found.text.strip()
    return text or None


def parse_coord(raw: str | None, *, is_lat: bool) -> float | None:
    """Decimal-degrees-with-hemisphere → signed float, range-checked; else ``None``.

    ``30.32166667N`` → ``+30.32166667``; ``081.435W`` → ``-81.435``. A latitude must
    carry N/S (≤ 90°), a longitude E/W (≤ 180°); a mismatch or out-of-range value is
    rejected (the caller treats a rejected vertex as malformed geometry — §18.10).
    """
    if raw is None:
        return None
    m = _COORD_RE.match(raw)
    if m is None:
        return None
    hemi = m.group(2).upper()
    if is_lat and hemi not in ("N", "S"):
        return None
    if not is_lat and hemi not in ("E", "W"):
        return None
    value = float(m.group(1))
    if hemi in ("S", "W"):
        value = -value
    limit = 90.0 if is_lat else 180.0
    if not -limit <= value <= limit:
        return None
    return value


def parse_local_dt(raw: str | None, tz: str | None) -> datetime | None:
    """Naive ``YYYY-MM-DDThh:mm:ss`` local time + ``codeTimeZone`` → aware UTC.

    Returns ``None`` for an unparseable timestamp or an **unmapped** zone — we keep the
    raw local string in attributes either way and never invent a UTC instant we cannot
    justify (PRD §37). An already-offset ISO string is honored directly.
    """
    if not raw:
        return None
    text = raw.strip()
    try:
        naive = datetime.fromisoformat(text)
    except ValueError:
        return None
    if naive.tzinfo is not None:  # already carries an offset — trust it
        return naive.astimezone(UTC)
    offset_h = _TZ_OFFSETS_H.get((tz or "").strip().upper())
    if offset_h is None:
        return None
    return naive.replace(tzinfo=timezone(timedelta(hours=offset_h))).astimezone(UTC)


def _ring_from_area(abd: ET.Element) -> list[Position] | None:
    """Build a closed GeoJSON ring from an ``abdMergedArea``'s ``Avx`` vertices.

    Each ``Avx`` carries the boundary vertex as ``geoLat``/``geoLong`` (arc vertices
    keep their endpoint coordinate, so a chord approximation is exact at the vertices);
    a bad vertex makes the whole ring unusable (``None``) rather than inventing a point.
    A ring needs ≥ 3 distinct vertices; the first is repeated to close it (RFC 7946).
    """
    ring: list[Position] = []
    for avx in abd.findall("Avx"):
        lat = parse_coord(_text(avx, "geoLat"), is_lat=True)
        lon = parse_coord(_text(avx, "geoLong"), is_lat=False)
        if lat is None or lon is None:
            return None
        ring.append([lon, lat])
    if len(ring) < 3:
        return None
    if ring[0] != ring[-1]:
        ring.append(ring[0])
    return ring


def _alt(ase: ET.Element | None, which: Literal["Upper", "Lower"]) -> dict[str, Any]:
    """Vertical limit of one area: value + unit + basis (ALT=MSL, HEI=AGL, etc.)."""
    return {
        "value": _text(ase, f"valDistVer{which}"),
        "unit": _text(ase, f"uomDistVer{which}"),
        "basis": _text(ase, f"codeDistVer{which}"),
    }


def parse_detail(
    raw: bytes,
    *,
    notam_id: str,
    list_type: str | None,
    received_at: datetime,
) -> GeoFeatureRecord | EventRecord | None:
    """Normalize one ``<XNOTAM-Update>`` detail to a schema-v2 record.

    Returns a ``GeoFeatureRecord`` (``Polygon``/``MultiPolygon``) for a parseable
    active TFR; an ``EventRecord`` when the record is active but its geometry cannot be
    built (textual fallback, §18.10); or ``None`` when there is no active ``<Add>/<Not>``
    (e.g. a cancellation), which simply drops off the live map.
    """
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        log.debug("TFR %s: XML parse failed (%s)", notam_id, exc)
        return None

    not_el = root.find("Group/Add/Not")
    if not_el is None:  # not an active addition (cancel/remove/replace) — nothing to draw
        return None

    notuid = not_el.find("NotUid")
    name = _text(notuid, "txtLocalName")
    issued_raw = _text(notuid, "dateIssued")
    tz = _text(not_el, "codeTimeZone")
    eff_raw = _text(not_el, "dateEffective")
    exp_raw = _text(not_el, "dateExpire")
    valid_from = parse_local_dt(eff_raw, tz)
    valid_until = parse_local_dt(exp_raw, tz)
    cfr = _text(not_el, "TfrNot/codeType")
    facility = _text(not_el, "codeFacility")
    purpose = _text(not_el, "txtDescrPurpose")
    cities = [
        f"{_text(g, 'txtNameCity') or ''}, {_text(g, 'txtNameUSState') or ''}".strip(", ")
        for g in not_el.findall("AffLocGroup")
    ]
    # dateIssued is the NOTAM issuance instant (UTC by convention); it is the dedupe
    # revision token and the record's "observed" time.
    issued_dt = parse_local_dt(issued_raw, "UTC")
    observed_at = issued_dt or received_at

    # Build one ring per TFR area; collect per-area altitude/name metadata alongside.
    rings: list[list[Position]] = []
    areas_meta: list[dict[str, Any]] = []
    malformed_areas = 0
    for group in not_el.findall("TfrNot/TFRAreaGroup"):
        ase = group.find("aseTFRArea")
        abd = group.find("abdMergedArea")
        ring = _ring_from_area(abd) if abd is not None else None
        if ring is None:
            malformed_areas += 1
            continue
        rings.append(ring)
        areas_meta.append(
            {
                "name": _text(ase, "txtName"),
                "altitude_upper": _alt(ase, "Upper"),
                "altitude_lower": _alt(ase, "Lower"),
            }
        )

    attributes: dict[str, Any] = {
        "notam_id": notam_id,
        "name": name,
        "list_type": list_type,  # SECURITY / VIP / HAZARDS / SPORTS (from the list row)
        "cfr_section": cfr,
        "regulatory_label": _CFR_LABELS.get(cfr or "", cfr),
        "facility": facility,
        "affected_locations": [c for c in cities if c],
        "purpose": purpose,
        "effective_local": eff_raw,
        "expire_local": exp_raw,
        "time_zone": tz,
        "time_zone_resolved": (tz or "").strip().upper() in _TZ_OFFSETS_H,
        "issued": issued_raw,
        "areas": areas_meta,
        "detail_url": f"{DEFAULT_BASE_URL}/download/detail_{notam_to_path(notam_id)}.xml",
        "attribution": ATTRIBUTION,
        "caveat": CAVEAT,
    }

    if not rings:
        # Active TFR with no usable geometry → textual event, never an invented shape.
        summary = f"TFR {notam_id}" + (f" — {name}" if name else "") + ": geometry unparseable"
        return EventRecord(
            id=f"event:tfr_unparseable:{notam_id}",
            source=SOURCE,
            observed_at=observed_at,
            received_at=received_at,
            published_at=received_at,
            correlation_key=f"tfr:faa:{notam_id}",
            event_type="tfr_geometry_unparseable",
            subject_id=f"tfr:faa:{notam_id}",
            summary=summary,
            message=(
                f"{malformed_areas} TFR area(s) had unparseable boundary geometry; "
                "shown as text only. " + CAVEAT
            ),
            severity="low",
            tags=["tfr", "faa", "unparseable"],
            attributes=attributes,
        )

    geometry: Polygon | MultiPolygon = (
        Polygon(coordinates=[rings[0]])
        if len(rings) == 1
        else MultiPolygon(coordinates=[[ring] for ring in rings])
    )
    if malformed_areas:  # partially-parsed: keep the good areas, flag the dropped ones
        attributes["dropped_areas"] = malformed_areas

    return GeoFeatureRecord(
        id=f"tfr:faa:{notam_id}",
        source=SOURCE,
        observed_at=observed_at,
        received_at=received_at,
        published_at=received_at,
        correlation_key=f"tfr:faa:{notam_id}",
        feature_type="tfr",
        geometry=geometry,
        valid_from=valid_from,
        valid_until=valid_until,
        # TFRs are not severity-ranked; the type drives the (uniform) styling, so leave
        # severity unset rather than invent a level.
        severity=None,
        label=name or f"TFR {notam_id}",
        provenance=[
            Provenance(
                source=SOURCE,
                provider="faa",
                observed_at=observed_at,
                received_at=received_at,
                local_rf=False,
                confidence="high",
            )
        ],
        tags=["tfr", "faa"] + ([list_type.lower()] if list_type else []),
        attributes=attributes,
    )


# --- AOI test ------------------------------------------------------------------


def _point_in_ring(lon: float, lat: float, ring: list[Position]) -> bool:
    """Ray-casting point-in-polygon over a closed ring (lon/lat positions)."""
    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if ((yi > lat) != (yj > lat)) and (lon < (xj - xi) * (lat - yi) / (yj - yi) + xi):
            inside = not inside
        j = i
    return inside


def _rings_of(geometry: GeoJSONGeometry) -> list[list[Position]]:
    if isinstance(geometry, Polygon):
        return geometry.coordinates
    if isinstance(geometry, MultiPolygon):
        return [ring for poly in geometry.coordinates for ring in poly]
    return []  # non-areal geometry (TFRs are only ever polygons) → no AOI rings


def tfr_in_aoi(
    geometry: GeoJSONGeometry,
    *,
    center_lat: float,
    center_lon: float,
    radius_m: float,
) -> bool:
    """A TFR is in the AOI if any boundary vertex is within ``radius_m`` of the station
    OR the station lies inside any area ring (so a large TFR enclosing the station, or a
    small one beside it, both count)."""
    for ring in _rings_of(geometry):
        for lon, lat in ((p[0], p[1]) for p in ring):
            if haversine_m(center_lon, center_lat, lon, lat) <= radius_m:
                return True
        if _point_in_ring(center_lon, center_lat, ring):
            return True
    return False


# --- Records stream + bus pump -------------------------------------------------


async def tfr_records(
    provider: FaaTfrProvider,
    *,
    center_lat: float,
    center_lon: float,
    radius_nm: float,
    poll_s: float = 300.0,
    max_details_per_poll: int = 60,
    states: frozenset[str] = frozenset(),
) -> AsyncIterator[Record]:
    """Yield the FAA TFR record stream: ``starting``, then TFRs + health each poll.

    Each poll fetches the light list, then fetches detail XML only for TFRs not already
    resolved at their current ``creation_date`` (revision dedupe), bounded by
    ``max_details_per_poll`` so a nationwide list drains over a few polls instead of one
    burst. A parsed TFR is emitted only if its geometry intersects the AOI; an
    unparseable one is emitted as a textual event. A TFR that leaves the list is
    forgotten (its feature ages out by ``valid_until``/expiry). A list-fetch failure
    yields ``degraded`` (keeping the last good TFRs on the map) and backs off — failure
    isolation (PRD §17.4/§37). ``states`` (optional) pre-filters the list to the
    operator's region to cut detail fetches.
    """
    yield _status("starting", _now())
    radius_m = radius_nm * _M_PER_NM
    received = 0
    backoff = INITIAL_BACKOFF_S
    #: notam_id → creation_date last resolved, so a re-poll fetches only new/changed
    #: TFRs. Pruned to the current listing each poll so it can't grow over a long soak.
    resolved: dict[str, str] = {}

    while True:
        now = _now()
        try:
            listing = await provider.fetch_list()
        except Exception as exc:  # a bad list fetch must not crash the adapter
            log.warning("FAA TFR list fetch failed (%s); degrading", exc)
            yield _status(
                "degraded",
                now,
                records_received=received,
                error_code=type(exc).__name__,
                error_summary=str(exc)[:200],
            )
            sleep_for, backoff = _backoff(backoff)
            await asyncio.sleep(sleep_for)
            continue
        backoff = INITIAL_BACKOFF_S

        current: dict[str, str] = {}
        to_fetch: list[dict[str, Any]] = []
        for row in listing:
            nid = row.get("notam_id")
            if not isinstance(nid, str) or not nid:
                continue
            state = str(row.get("state") or "").strip().upper()
            if states and state not in states:
                continue
            cdate = str(row.get("creation_date") or "")
            current[nid] = cdate
            if resolved.get(nid) != cdate:
                to_fetch.append(row)
        resolved = {k: v for k, v in resolved.items() if k in current}  # forget removed

        rejected = 0
        emitted = 0
        last_record_at: datetime | None = None
        for row in to_fetch[:max_details_per_poll]:
            nid = str(row["notam_id"])
            try:
                raw = await provider.fetch_detail(nid)
            except Exception as exc:  # a single bad detail must not drop the sweep
                log.debug("FAA TFR detail %s fetch failed (%s)", nid, exc)
                rejected += 1
                continue  # leave unresolved so the next poll retries it
            resolved[nid] = current[nid]  # mark resolved regardless of AOI/parse outcome
            try:
                record = parse_detail(raw, notam_id=nid, list_type=row.get("type"), received_at=now)
            except Exception as exc:  # one malformed detail must not crash the poll
                log.debug("FAA TFR detail %s parse error (%s)", nid, exc)
                rejected += 1
                continue
            if record is None:
                continue
            if isinstance(record, GeoFeatureRecord) and not tfr_in_aoi(
                record.geometry,
                center_lat=center_lat,
                center_lon=center_lon,
                radius_m=radius_m,
            ):
                continue  # outside AOI — not an error, just not ours
            received += 1
            emitted += 1
            if last_record_at is None or record.observed_at > last_record_at:
                last_record_at = record.observed_at
            yield record

        yield _status(
            "connected",
            now,
            records_received=received,
            records_rejected=rejected,
            last_record_at=last_record_at,
            attributes={
                "listed": len(listing),
                "considered": len(current),
                "fetched_this_poll": min(len(to_fetch), max_details_per_poll),
                "pending": max(0, len(to_fetch) - max_details_per_poll),
                "emitted_this_poll": emitted,
                "states_filter": sorted(states) or None,
            },
        )
        await asyncio.sleep(poll_s)


async def run_faa_tfr(
    cfg: Settings,
    ready: asyncio.Event,
    *,
    provider: FaaTfrProvider | None = None,
    poll_s: float | None = None,
) -> None:
    """Pump the FAA TFR stream onto the bus until cancelled (PRD §17.1).

    Waits for the subscriber to be live, then publishes :func:`tfr_records`. A broker
    drop triggers a jittered exponential reconnect; a FRESH records generator is built
    per connection (a generator unwound by ``MqttError`` cannot be resumed). The
    provider is stateless across reconnects and injectable for tests.
    """
    await ready.wait()
    prov = provider if provider is not None else build_provider(cfg)
    resolved_poll = poll_s if poll_s is not None else cfg.faa_tfr_poll_s
    states = frozenset(s.strip().upper() for s in cfg.faa_tfr_states if s.strip())
    log.info(
        "FAA TFR adapter -> %s (AOI %.0f NM, states=%s)",
        prov.name,
        cfg.faa_tfr_radius_nm,
        sorted(states) or "all",
    )
    backoff = INITIAL_BACKOFF_S
    while True:
        try:
            async with connect(cfg, identifier="aether-faa-tfr") as bus:
                backoff = INITIAL_BACKOFF_S
                async for record in tfr_records(
                    prov,
                    center_lat=cfg.faa_tfr_center_lat,
                    center_lon=cfg.faa_tfr_center_lon,
                    radius_nm=cfg.faa_tfr_radius_nm,
                    poll_s=resolved_poll,
                    max_details_per_poll=cfg.faa_tfr_max_details_per_poll,
                    states=states,
                ):
                    await bus.publish_record(record)
                return  # generator exhausted (only on cancellation in practice)
        except aiomqtt.MqttError as exc:
            sleep_for, backoff = _backoff(backoff)
            log.warning("FAA TFR lost broker (%s); reconnecting in %.1fs", exc, sleep_for)
            await asyncio.sleep(sleep_for)

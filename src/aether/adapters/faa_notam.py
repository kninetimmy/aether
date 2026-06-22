"""FAA NOTAM adapter — capability-gated (PRD §11.13, §18.11, M6.4).

Polls the **official FAA NOTAM API** (re-verified at build time per PRD §38):

``GET https://external-api.faa.gov/notamapi/v1/notams?responseFormat=geoJson&...``
with the operator's ``client_id``/``client_secret`` headers. The geoJson response is a
GeoJSON ``FeatureCollection``-shaped page::

    {"totalPages": N, "items": [
        {"properties": {"coreNOTAMData": {"notam": {...}, "notamTranslation": [...]}},
         "geometry": null | {"type": "GeometryCollection", "geometries": [Polygon, ...]}}
    ]}

Each item is normalized to a schema-v2 record:

- a NOTAM with FAA-**supplied** geometry → a ``GeoFeatureRecord`` (``feature_type=
  "notam_geometry"``) carrying a ``Polygon`` / ``MultiPolygon``, ``valid_from`` /
  ``valid_until`` from ``effectiveStart`` / ``effectiveEnd`` (AIRSPACE-FR-002), and the
  original NOTAM number/text retained verbatim (AIRSPACE-FR-006);
- a NOTAM with ``geometry: null`` → a textual ``EventRecord`` for the facility panel
  (AIRSPACE-FR-005), never an invented shape;
- a NOTAM whose supplied geometry cannot be built into a valid ring → a textual
  ``EventRecord`` too (the §18.11 *"draw only reliably supplied geometry"* rail).

**aether never derives geometry from NOTAM free text** (§18.11 — *avoid ad hoc
natural-language geometry guessing in v1*). It draws only the structured ``geometry``
member the FAA supplies; everything else is text.

**Capability gate (AIRSPACE-FR-008):** the API needs operator-supplied FAA credentials.
With no ``client_id``/``client_secret`` the adapter degrades *visibly* — one ``disabled``
source status, then it exits cleanly (a config gap will not self-heal). A 401/403 yields
one ``offline`` status (error ``Unauthorized``) and also exits — bad creds will not
self-heal either. It never bakes in a credential and never crashes the app
(PRD §2/§37, the same stance as FIRMS/AISStream). Read-only: aether only *fetches*, no
faster than ``poll_s`` and bounded by ``max_pages_per_poll``, and never transmits.

**Honest labeling (AIRSPACE-FR-007):** the product is **not authoritative** for airspace
and is **not a flight-planning product**; the FAA is. Every record carries FAA attribution
and that caveat. The ``locationRadius`` query is capped at the FAA maximum (100 NM); the
effective radius is surfaced in the status so a wider AOI is never silently honored.

Responsibility split mirrors :mod:`aether.adapters.faa_tfr`:

- :class:`FaaNotamProvider` / :class:`FaaNotamHttpProvider` — fetch one geoJson page (the
  live HTTPS API, or the in-process ``fake`` feeder).
- :func:`parse_feature` — pure GeoJSON Feature → ``GeoFeatureRecord`` / ``EventRecord`` /
  ``None`` (a cancellation drops off the map).
- :func:`notam_records` — the ``records()`` contract: ``starting``, then a poll loop with
  pagination, revision dedupe, and ``degraded``-on-failure isolation.
- :func:`run_faa_notam` — bus connection, the credential gate, and jittered exponential
  backoff on broker loss.
"""

import asyncio
import functools
import json
import logging
import random
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

import aiomqtt
from pydantic import ValidationError

from aether.bus.client import connect
from aether.config import Settings
from aether.schema.geometry import MultiPolygon, Polygon, Position
from aether.schema.provenance import Provenance
from aether.schema.records import EventRecord, GeoFeatureRecord, Record, SourceStatusRecord

log = logging.getLogger(__name__)

#: Stable source name + retained health-record id (PRD §23 status stream). Records
#: publish to ``aether/v2/records/faa_notam`` (PRD §17/§23, derived from this name).
SOURCE = "faa_notam"
STATUS_ID = f"source_status:{SOURCE}"

#: FAA attribution carried on every record + status so provenance stays honest (§11.2).
ATTRIBUTION = "FAA NOTAMs (external-api.faa.gov)"
#: The non-negotiable honesty caveat for this layer (AIRSPACE-FR-007).
CAVEAT = "Not a flight-planning product; consult official FAA sources before flight."

#: Default API host; ``/notamapi/v1/notams`` hangs off it. ``fake``/``demo`` (as the base
#: *or* either credential) selects the no-hardware feeder instead.
DEFAULT_BASE_URL = "https://external-api.faa.gov"
#: API path (version pinned per the verified OpenAPI 1.0.4 contract).
_NOTAM_PATH = "/notamapi/v1/notams"

#: FAA-documented maximum for the ``locationRadius`` query parameter (nautical miles).
MAX_QUERY_RADIUS_NM = 100.0
#: FAA-documented maximum ``pageSize``; we request a gentler page and paginate.
MAX_PAGE_SIZE = 1000

#: Jittered exponential backoff bounds — shared shape with every other adapter (§17.1).
INITIAL_BACKOFF_S = 1.0
MAX_BACKOFF_S = 60.0

#: Provider-name aliases selecting the in-process no-hardware feeder.
_FAKE_PROVIDER_NAMES = frozenset({"fake", "demo"})

#: Hard cap on a single response body; a runaway is truncated rather than read unbounded.
MAX_RESPONSE_BYTES = 16 * 1024 * 1024

#: NOTAM ``type`` codes: ``N`` new, ``R`` replace (active), ``C`` cancel (drops off map).
_CANCEL_TYPE = "C"


def _now() -> datetime:
    return datetime.now(UTC)


def _backoff(delay: float) -> tuple[float, float]:
    """Full-jitter backoff: sleep a random slice of ``delay``, then double it (capped)."""
    capped = min(delay, MAX_BACKOFF_S)
    return random.uniform(0.0, capped), min(capped * 2.0, MAX_BACKOFF_S)


class NotamAuthError(Exception):
    """A 401/403 from the NOTAM API — bad/expired credentials, will not self-heal."""


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


# --- Provider (raw page fetch) -------------------------------------------------


class FaaNotamProvider(Protocol):
    """A source of FAA NOTAM geoJson pages."""

    name: str

    async def fetch_page(self, page_num: int) -> dict[str, Any]: ...


def _redact(text: str, *secrets: str) -> str:
    """Strip any credential out of a string before it is logged or put on the bus."""
    out = text
    for secret in secrets:
        s = secret.strip()
        if s:
            out = out.replace(s, "***")
    return out


def _blocking_get(url: str, headers: dict[str, str], timeout_s: float) -> bytes:
    if not url.startswith("https://"):  # FAA API is https; never downgrade credentials
        raise ValueError("refusing non-https NOTAM API URL")
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:  # noqa: S310 (https-checked)
            return bytes(resp.read(MAX_RESPONSE_BYTES + 1))
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise NotamAuthError(f"NOTAM API returned HTTP {exc.code}") from None
        raise


async def _default_fetch(url: str, headers: dict[str, str], *, timeout_s: float) -> bytes:
    # Blocking urllib off the event loop (mirrors the FAA TFR provider); no extra dep.
    return await asyncio.to_thread(_blocking_get, url, headers, timeout_s)


class FaaNotamHttpProvider:
    """Fetch one FAA NOTAM geoJson page over HTTPS (AIRSPACE-FR-001/003).

    ``fetch`` is injectable so tests drive canned bytes with no network. The default GETs
    ``<base>/notamapi/v1/notams?...`` with the credentials in ``client_id``/``client_secret``
    headers (never the URL/query, so they cannot leak through a redirect or access log).
    """

    name = "faa_notam"

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        *,
        client_id: str,
        client_secret: str,
        center_lat: float,
        center_lon: float,
        radius_nm: float,
        page_size: int,
        timeout_s: float = 15.0,
        fetch: Callable[[str, dict[str, str]], Awaitable[bytes]] | None = None,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._client_id = client_id
        self._client_secret = client_secret
        self._lat = center_lat
        self._lon = center_lon
        # Cap at the FAA maximum so a wider AOI never produces a rejected query.
        self._radius_nm = min(radius_nm, MAX_QUERY_RADIUS_NM)
        self._page_size = max(1, min(page_size, MAX_PAGE_SIZE))
        self._fetch = (
            fetch if fetch is not None else functools.partial(_default_fetch, timeout_s=timeout_s)
        )

    @property
    def effective_radius_nm(self) -> float:
        return self._radius_nm

    def _url(self, page_num: int) -> str:
        query = urllib.parse.urlencode(
            {
                "responseFormat": "geoJson",
                "locationLatitude": f"{self._lat:.5f}",
                "locationLongitude": f"{self._lon:.5f}",
                "locationRadius": f"{self._radius_nm:g}",
                "pageSize": str(self._page_size),
                "pageNum": str(page_num),
            }
        )
        return f"{self._base}{_NOTAM_PATH}?{query}"

    async def fetch_page(self, page_num: int) -> dict[str, Any]:
        headers = {
            "User-Agent": "aether-faa-notam/1.0",
            "Accept": "application/json",
            "client_id": self._client_id,
            "client_secret": self._client_secret,
        }
        try:
            raw = await self._fetch(self._url(page_num), headers)
        except NotamAuthError:
            raise  # terminal: surfaced distinctly so the caller can stop, not spin
        except Exception as exc:  # redact creds out of any error before it propagates
            raise RuntimeError(_redact(str(exc), self._client_id, self._client_secret)) from None
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("NOTAM API response was not a JSON object")
        return data


def build_provider(cfg: Settings) -> FaaNotamProvider:
    """Resolve the configured FAA NOTAM provider (live API, or the fake feeder).

    Raises ``ValueError`` when the live API is selected with missing credentials — the
    capability gate (AIRSPACE-FR-008); :func:`run_faa_notam` turns that into one
    ``disabled`` status. ``fake``/``demo`` as the base *or* either credential selects the
    no-hardware feeder.
    """
    base = cfg.faa_notam_base_url.strip()
    cid = cfg.faa_notam_client_id.strip()
    secret = cfg.faa_notam_client_secret.strip()
    if base.lower() in _FAKE_PROVIDER_NAMES or {cid.lower(), secret.lower()} & _FAKE_PROVIDER_NAMES:
        from aether.adapters.faa_notam_fake_feeder import FakeFaaNotamProvider

        # Place canned NOTAMs relative to the configured AOI center so the demo renders.
        return FakeFaaNotamProvider(
            center_lat=cfg.faa_notam_center_lat, center_lon=cfg.faa_notam_center_lon
        )
    if not cid or not secret:
        raise ValueError(
            "FAA NOTAM credentials are required "
            "(set AETHER_FAA_NOTAM_CLIENT_ID and AETHER_FAA_NOTAM_CLIENT_SECRET)"
        )
    return FaaNotamHttpProvider(
        base,
        client_id=cid,
        client_secret=secret,
        center_lat=cfg.faa_notam_center_lat,
        center_lon=cfg.faa_notam_center_lon,
        radius_nm=cfg.faa_notam_radius_nm,
        page_size=cfg.faa_notam_page_size,
        timeout_s=cfg.faa_notam_timeout_s,
    )


# --- Parsing helpers (pure) ----------------------------------------------------


def _parse_iso(value: Any) -> datetime | None:
    """ISO-8601 instant (``2023-12-25T13:00:00.000Z``) → aware UTC; else ``None``.

    ``effectiveEnd`` is the literal ``PERM`` for a permanent NOTAM — that is an
    open-ended validity, so it yields ``None`` (no expiry) rather than a parse error.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or text.upper() == "PERM":
        return None
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def _polygon_rings(geom: dict[str, Any]) -> list[list[Position]] | None:
    """A GeoJSON Polygon dict → its validated rings, or ``None`` if unusable.

    The rings are run through the schema :class:`Polygon` validator so an out-of-range or
    malformed vertex is *rejected* (the caller falls back to a textual event), never
    coerced into an invented point (§18.11).
    """
    if geom.get("type") != "Polygon":
        return None
    try:
        return Polygon.model_validate({"coordinates": geom.get("coordinates")}).coordinates
    except (ValidationError, TypeError, ValueError):
        return None


def geometry_from_member(
    geometry: Any,
) -> tuple[Polygon | MultiPolygon | None, int]:
    """FAA ``geometry`` member → a drawable ``Polygon``/``MultiPolygon`` + dropped count.

    The FAA supplies either ``null`` (no usable geometry), a ``GeometryCollection`` of
    ``Polygon`` volumes, or — defensively — a bare ``Polygon``/``MultiPolygon``. Each
    contained ``Polygon`` is validated independently; a member that will not validate is
    counted as *dropped* rather than silently skewing the shape. Returns ``(None, 0)`` when
    there is nothing to draw (the caller then emits a textual event).
    """
    if not isinstance(geometry, dict):
        return None, 0

    gtype = geometry.get("type")
    members: list[dict[str, Any]]
    if gtype == "GeometryCollection":
        raw = geometry.get("geometries")
        members = [g for g in raw if isinstance(g, dict)] if isinstance(raw, list) else []
    elif gtype == "MultiPolygon":
        # Re-shape each sub-polygon into a Polygon dict so one validation path covers all.
        coords = geometry.get("coordinates")
        members = (
            [{"type": "Polygon", "coordinates": poly} for poly in coords]
            if isinstance(coords, list)
            else []
        )
    elif gtype == "Polygon":
        members = [geometry]
    else:
        return None, 0

    polys: list[list[list[Position]]] = []
    dropped = 0
    for member in members:
        rings = _polygon_rings(member)
        if rings is None:
            dropped += 1
            continue
        polys.append(rings)

    if not polys:
        return None, dropped
    if len(polys) == 1:
        return Polygon(coordinates=polys[0]), dropped
    return MultiPolygon(coordinates=polys), dropped


def _notam_attributes(notam: dict[str, Any], *, dropped_areas: int) -> dict[str, Any]:
    """Displayable NOTAM fields, with the original text/number retained (AIRSPACE-FR-006)."""
    attrs: dict[str, Any] = {
        "notam_id": notam.get("id"),
        "number": notam.get("number"),
        "notam_type": notam.get("type"),
        "classification": notam.get("classification"),
        "location": notam.get("location"),
        "icao_location": notam.get("icaoLocation"),
        "account_id": notam.get("accountId"),
        "feature_type": notam.get("featureType"),
        "selection_code": notam.get("selectionCode"),
        "issued": notam.get("issued"),
        "last_updated": notam.get("lastUpdated"),
        "effective_start": notam.get("effectiveStart"),
        "effective_end": notam.get("effectiveEnd"),
        "schedule": notam.get("schedule"),
        "text": notam.get("text"),  # original NOTAM text, verbatim
        "attribution": ATTRIBUTION,
        "caveat": CAVEAT,
    }
    if dropped_areas:
        attrs["dropped_areas"] = dropped_areas
    return attrs


def parse_feature(
    feature: dict[str, Any], *, received_at: datetime
) -> GeoFeatureRecord | EventRecord | None:
    """Normalize one geoJson NOTAM Feature to a schema-v2 record.

    Returns a ``GeoFeatureRecord`` (``Polygon``/``MultiPolygon``) for a NOTAM with FAA-
    supplied geometry; a textual ``EventRecord`` when geometry is ``null`` *or* cannot be
    built (the facility panel, AIRSPACE-FR-005); or ``None`` for a cancellation
    (``type == "C"``) or a record with no parseable NOTAM body, which simply drops off the
    live map.
    """
    props = feature.get("properties")
    core = props.get("coreNOTAMData") if isinstance(props, dict) else None
    notam = core.get("notam") if isinstance(core, dict) else None
    if not isinstance(notam, dict):
        return None

    notam_id = notam.get("id") or notam.get("number")
    if not isinstance(notam_id, str) or not notam_id:
        return None
    if str(notam.get("type") or "").strip().upper() == _CANCEL_TYPE:
        return None  # a cancellation removes the NOTAM — nothing to draw

    number = notam.get("number")
    location = notam.get("icaoLocation") or notam.get("location")
    observed_at = (
        _parse_iso(notam.get("issued")) or _parse_iso(notam.get("lastUpdated")) or received_at
    )
    valid_from = _parse_iso(notam.get("effectiveStart"))
    valid_until = _parse_iso(notam.get("effectiveEnd"))

    geometry, dropped = geometry_from_member(feature.get("geometry"))
    attributes = _notam_attributes(notam, dropped_areas=dropped)
    correlation_key = f"notam:faa:{notam_id}"
    label = f"NOTAM {number}" if number else f"NOTAM {notam_id}"
    if location:
        label = f"{label} ({location})"

    if geometry is None:
        # No usable geometry → textual facility-panel event, never an invented shape.
        why = "geometry unparseable" if dropped else "no geometry supplied"
        text = notam.get("text")
        summary = f"{label}: {why}"
        return EventRecord(
            id=f"event:notam_text:{notam_id}",
            source=SOURCE,
            observed_at=observed_at,
            received_at=received_at,
            published_at=received_at,
            correlation_key=correlation_key,
            event_type="notam_geometry_unparseable" if dropped else "notam_textual",
            subject_id=correlation_key,
            summary=summary,
            message=(f"{text} " if isinstance(text, str) and text else "") + CAVEAT,
            severity="low",
            tags=["notam", "faa", "textual"],
            attributes=attributes,
        )

    return GeoFeatureRecord(
        id=f"notam:faa:{notam_id}",
        source=SOURCE,
        observed_at=observed_at,
        received_at=received_at,
        published_at=received_at,
        correlation_key=correlation_key,
        feature_type="notam_geometry",
        geometry=geometry,
        valid_from=valid_from,
        valid_until=valid_until,
        # NOTAMs are not severity-ranked here; the type drives styling — leave unset.
        severity=None,
        label=label,
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
        tags=["notam", "faa"],
        attributes=attributes,
    )


def _revision_token(feature: dict[str, Any]) -> str:
    """A change token for one Feature: the NOTAM's ``lastUpdated`` (or ``issued``)."""
    props = feature.get("properties")
    core = props.get("coreNOTAMData") if isinstance(props, dict) else None
    notam = core.get("notam") if isinstance(core, dict) else {}
    if not isinstance(notam, dict):
        return ""
    return str(notam.get("lastUpdated") or notam.get("issued") or "")


def _feature_id(feature: dict[str, Any]) -> str | None:
    props = feature.get("properties")
    core = props.get("coreNOTAMData") if isinstance(props, dict) else None
    notam = core.get("notam") if isinstance(core, dict) else {}
    if not isinstance(notam, dict):
        return None
    nid = notam.get("id") or notam.get("number")
    return nid if isinstance(nid, str) and nid else None


# --- Records stream + bus pump -------------------------------------------------


async def notam_records(
    provider: FaaNotamProvider,
    *,
    poll_s: float = 300.0,
    max_pages_per_poll: int = 5,
) -> AsyncIterator[Record]:
    """Yield the FAA NOTAM record stream: ``starting``, then NOTAMs + health each poll.

    Each poll walks pages (bounded by ``max_pages_per_poll`` and the response's
    ``totalPages``), normalizing only NOTAMs not already resolved at their current
    revision token (dedupe across re-polls). A NOTAM that leaves the listing is forgotten
    (its feature ages out by ``valid_until``). A fetch/parse failure yields ``degraded``
    (keeping the last good NOTAMs on the map) and backs off — failure isolation
    (PRD §17.4/§37). A 401/403 yields one ``offline`` status and ends the stream — bad
    credentials will not self-heal.
    """
    yield _status("starting", _now())
    received = 0
    backoff = INITIAL_BACKOFF_S
    #: notam id → revision token last emitted, so a re-poll re-emits only new/changed
    #: NOTAMs. Pruned to the current listing each poll so it can't grow over a long soak.
    resolved: dict[str, str] = {}

    while True:
        now = _now()
        try:
            features, total_pages = await _fetch_all_pages(provider, max_pages_per_poll)
        except NotamAuthError as exc:
            log.error("FAA NOTAM unauthorized (%s); stopping", exc)
            yield _status(
                "offline",
                now,
                records_received=received,
                error_code="Unauthorized",
                error_summary=str(exc)[:200],
            )
            return  # bad credentials won't self-heal; don't spin
        except Exception as exc:  # a bad fetch/body must not crash the adapter
            log.warning("FAA NOTAM fetch failed (%s); degrading", exc)
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
        rejected = 0
        emitted = 0
        last_record_at: datetime | None = None
        for feature in features:
            nid = _feature_id(feature)
            if nid is None:
                rejected += 1
                continue
            token = _revision_token(feature)
            current[nid] = token
            if resolved.get(nid) == token:
                continue  # already emitted this revision — dedupe
            try:
                record = parse_feature(feature, received_at=now)
            except Exception as exc:  # one malformed feature must not crash the poll
                log.debug("FAA NOTAM feature %s parse error (%s)", nid, exc)
                rejected += 1
                continue
            resolved[nid] = token  # mark resolved regardless of draw/text/drop outcome
            if record is None:
                continue
            received += 1
            emitted += 1
            if last_record_at is None or record.observed_at > last_record_at:
                last_record_at = record.observed_at
            yield record
        resolved = {k: v for k, v in resolved.items() if k in current}  # forget removed

        radius = getattr(provider, "effective_radius_nm", None)
        yield _status(
            "connected",
            now,
            records_received=received,
            records_rejected=rejected,
            last_record_at=last_record_at,
            attributes={
                "listed": len(features),
                "total_pages": total_pages,
                "fetched_pages": min(total_pages, max_pages_per_poll),
                "emitted_this_poll": emitted,
                "query_radius_nm": radius,
            },
        )
        await asyncio.sleep(poll_s)


async def _fetch_all_pages(
    provider: FaaNotamProvider, max_pages: int
) -> tuple[list[dict[str, Any]], int]:
    """Fetch up to ``max_pages`` geoJson pages; return (all items, reported total pages)."""
    first = await provider.fetch_page(1)
    items = _items(first)
    total_pages = first.get("totalPages")
    total = total_pages if isinstance(total_pages, int) and total_pages > 0 else 1
    for page in range(2, min(total, max_pages) + 1):
        items.extend(_items(await provider.fetch_page(page)))
    return items, total


def _items(page: dict[str, Any]) -> list[dict[str, Any]]:
    """The ``items`` array of a geoJson page; raise on a non-NOTAM body (fail-visibly §37).

    A misconfigured query or an error envelope returns JSON without ``items`` — that is a
    *visible* failure (degraded status with a snippet), not a silently empty map.
    """
    items = page.get("items")
    if not isinstance(items, list):
        snippet = json.dumps(page)[:160]
        raise ValueError(f"NOTAM page has no 'items' array: {snippet}")
    return [item for item in items if isinstance(item, dict)]


async def run_faa_notam(
    cfg: Settings,
    ready: asyncio.Event,
    *,
    provider: FaaNotamProvider | None = None,
    poll_s: float | None = None,
) -> None:
    """Pump the FAA NOTAM stream onto the bus until cancelled (PRD §17.1).

    Waits for the subscriber to be live, then publishes :func:`notam_records`. Missing
    credentials are reported once as a ``disabled`` source status and the task exits
    cleanly — a config gap will not self-heal, so we do not spin (AIRSPACE-FR-008,
    mirroring FIRMS). A broker drop triggers a jittered exponential reconnect; a FRESH
    records generator is built per connection (a generator unwound by ``MqttError`` cannot
    be resumed). The provider is stateless across reconnects and injectable for tests.
    """
    await ready.wait()
    resolved_poll = poll_s if poll_s is not None else cfg.faa_notam_poll_s
    backoff = INITIAL_BACKOFF_S
    while True:
        try:
            async with connect(cfg, identifier="aether-faa-notam") as bus:
                backoff = INITIAL_BACKOFF_S  # reset once connected
                try:
                    prov = provider if provider is not None else build_provider(cfg)
                except ValueError as exc:
                    log.error("FAA NOTAM disabled: %s", exc)
                    await bus.publish_record(
                        _status(
                            "disabled",
                            _now(),
                            error_code="NoCredentials",
                            error_summary=str(exc)[:200],
                        )
                    )
                    return  # config gap won't self-heal; don't spin
                log.info("FAA NOTAM adapter -> %s", prov.name)
                async for record in notam_records(
                    prov,
                    poll_s=resolved_poll,
                    max_pages_per_poll=cfg.faa_notam_max_pages_per_poll,
                ):
                    await bus.publish_record(record)
                return  # generator exhausted (auth failure, or cancellation)
        except aiomqtt.MqttError as exc:
            sleep_for, backoff = _backoff(backoff)
            log.warning("FAA NOTAM lost broker (%s); reconnecting in %.1fs", exc, sleep_for)
            await asyncio.sleep(sleep_for)

"""AIS vessel adapter runner (PRD §18.5, §11.8, §17.1, §17.3).

The Internet AIS feed for the station's area of interest — vessel tracks from
`AISStream.io <https://aisstream.io/documentation>`_, a free secure-WebSocket
aggregator of the public AIS broadcasts ships transmit on 161.975/162.025 MHz.
This is a *receive-only Internet read* (the network sibling of the network ADS-B
and APRS-IS adapters): the only thing aether ever sends is the subscription
message, and there is no RF path here at all (PRD §2, §6 non-goals).

Responsibility split mirrors :mod:`aether.adapters.aprs_is` and
:mod:`aether.adapters.network_adsb`:

- :func:`ais_bbox` / :func:`build_subscription` — pure builders for the AISStream
  bounding-box AOI and the subscription JSON (unit-testable in isolation). A bad
  AOI or a missing API key raises, so the runner fails *visibly* as an ``offline``
  source status rather than connecting deaf or leaking a default.
- :class:`AisStreamSource` — the WebSocket: connect, send the subscription ONCE,
  yield raw JSON frames. Liveness is the WebSocket ping/pong the ``websockets``
  library maintains for us — unlike APRS-IS, AISStream sends *no* application-level
  keepalive, so a quiet AOI legitimately produces no frames and a data-silence
  timeout would false-reconnect; a genuinely dead socket surfaces as
  ``ConnectionClosed`` on ``recv()`` instead (PRD §17.3).
- :class:`VesselMerger` — folds AISStream's separate dynamic-position and
  static/voyage messages into ONE track per MMSI (PRD §18.5 / AIS-FR-003). Static
  data updates an accumulator and rides out on the next position report, so a
  vessel is one track carrying name/type/destination alongside its live position.
- :func:`ais_records` — the ``records()`` contract: ``starting``, then connect →
  dedup → merge → throttle → emit, with ``connected``/``degraded`` health. A
  dropped socket reconnects (re-subscribe) rather than ending the stream
  (PRD §17.4, §37). One malformed frame is a counted rejection, never fatal.
- :func:`run_ais` — bus connection + jittered exponential backoff on broker loss,
  building a FRESH records generator (and source) per reconnect (PEP 525 / M2.1b
  lesson). A missing API key / invalid AOI is reported once as ``offline`` and the
  task returns (a config error will not self-heal, so we do not spin).

Reused, not re-implemented: :class:`~aether.adapters.aprs_is.DuplicateFilter`
(bounded TTL signature dedup) and :class:`~aether.adapters.local_aprs.ThrottleGate`
(per-track publish pacing) — the same cross-adapter sharing aprs_is already does
with ``ThrottleGate``.
"""

import asyncio
import json
import logging
import math
import random
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

import aiomqtt
import websockets
from websockets.exceptions import ConnectionClosed, WebSocketException

from aether.adapters.aprs_is import DuplicateFilter
from aether.adapters.local_aprs import ThrottleGate
from aether.bus.client import connect
from aether.config import Settings
from aether.schema.geometry import Point
from aether.schema.provenance import Provenance
from aether.schema.records import Record, SourceStatusRecord, TrackRecord

log = logging.getLogger(__name__)

#: Per-source identifier; the MQTT topic suffix (PRD §23: ``records/ais``) and the
#: freshness-table key. Network-only — AIS has no local-RF leg in scope.
SOURCE = "ais"

#: Stable id for this source's retained health record (PRD §23 status stream).
STATUS_ID = f"source_status:{SOURCE}"

#: Provider name recorded in provenance (the AIS aggregator we read).
_PROVIDER = "aisstream"

#: Jittered exponential backoff bounds — shared shape with every other adapter so a
#: downed feed/broker is retried the same way (PRD §17.1, §17.4).
INITIAL_BACKOFF_S = 1.0
MAX_BACKOFF_S = 30.0

#: 1 knot = 1 NM/h → m/s; canonical internal units are SI (PRD §14.8). Shared
#: constant with the APRS adapter (speed-over-ground arrives in knots).
_KT_TO_MS = 1852.0 / 3600.0

#: 1 NM = 1 arcminute of latitude, so NM → degrees of latitude is /60 (PRD §16.2).
_NM_PER_DEG_LAT = 60.0

#: ITU-R M.1371 "not available" sentinels: a decoded value at/above these means the
#: ship did not transmit that field, so it must be dropped rather than plotted.
_SOG_NA = 102.3  # speed over ground, knots
_COG_NA = 360.0  # course over ground, degrees
_HEADING_NA = 511  # true heading, degrees

#: Largest AISStream frame we accept — a defensive payload cap (PRD §17.2). A single
#: AIS JSON envelope is well under this; a larger frame is rejected, not buffered.
_MAX_FRAME_BYTES = 64 * 1024

#: Duplicate window: the SAME vessel's SAME message at the SAME broadcast time,
#: re-reported by multiple receivers AISStream aggregates, is one observation — the
#: AIS analog of APRS multi-igate relay dedup (PRD §18.5, §37).
_DUP_TTL_S = 30.0

#: The per-MMSI static accumulator is bounded like the dedup/throttle tables
#: (PRD §27.2 "no unbounded memory growth", §37). A vessel unheard for this long is
#: dropped — its fused track has already expired from live state by then (the AIS
#: 30-min expire window, :data:`aether.fusion.freshness.DEFAULT_FRESHNESS`) — and a
#: hard size cap backstops an adversarial flood of unique MMSIs. The accumulator
#: persists across AIS socket reconnects within one bus session, so without this it
#: would grow for the life of the broker connection.
_MERGER_TTL_S = 1800.0
_MERGER_MAX_ENTRIES = 8192

#: AISStream message classes that carry a usable position fix.
_POSITION_TYPES = frozenset(
    {"PositionReport", "StandardClassBPositionReport", "ExtendedClassBPositionReport"}
)

#: ITU-R M.1371 navigational-status codes → label (standard, not inferred). Stored
#: as ``nav_status`` (raw int) plus ``nav_status_text`` so the UI need not decode.
_NAV_STATUS: dict[int, str] = {
    0: "under_way_using_engine",
    1: "at_anchor",
    2: "not_under_command",
    3: "restricted_manoeuverability",
    4: "constrained_by_draught",
    5: "moored",
    6: "aground",
    7: "engaged_in_fishing",
    8: "under_way_sailing",
    9: "reserved_hsc",
    10: "reserved_wig",
    11: "power_driven_towing_astern",
    12: "power_driven_pushing_ahead",
    13: "reserved",
    14: "ais_sart_mob_epirb",
    15: "undefined",
}


def _now() -> datetime:
    return datetime.now(UTC)


def _backoff(delay: float) -> tuple[float, float]:
    """Full-jitter backoff: sleep a random slice of ``delay``, then double it.

    Returns ``(sleep_for, next_delay)``. Identical to the other adapters' backoff
    so a downed feed/broker is retried the same way everywhere (PRD §17.1, §17.4).
    """
    capped = min(delay, MAX_BACKOFF_S)
    sleep_for = random.uniform(0.0, capped)
    return sleep_for, min(capped * 2.0, MAX_BACKOFF_S)


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
        attributes=attributes or {},
    )


# --- pure builders ------------------------------------------------------------


def ais_bbox(center_lat: float, center_lon: float, radius_nm: float) -> list[list[list[float]]]:
    """Build the AISStream ``BoundingBoxes`` param covering the AOI (PRD §18.5, §16.2).

    AISStream subscribes by rectangle, expecting a list of boxes where each box is
    two ``[lat, lon]`` corner pairs — **latitude first**, the opposite of GeoJSON's
    ``[lon, lat]`` order used everywhere else in aether. The configured NM radius
    becomes a lat/lon rectangle around the station: latitude scales at 1 NM per
    arcminute, longitude widens by ``1/cos(lat)`` toward the poles. Corners are
    clamped to WGS 84 bounds.

    Validates that the operator-supplied AOI is finite and in range, raising
    ``ValueError`` otherwise. A non-finite ``nan``/``inf`` would format into a
    syntactically valid but meaningless box the server silently ignores — leaving
    the adapter "connected" yet deaf. Raising routes a bad AOI through
    :func:`run_ais`'s ``ConfigError`` path so it fails *visibly* as ``offline``,
    the same stance as a missing API key (PRD §2/§37 fail-visibly).
    """
    if not (math.isfinite(center_lat) and -90.0 <= center_lat <= 90.0):
        raise ValueError(f"AIS center latitude out of WGS 84 range: {center_lat!r}")
    if not (math.isfinite(center_lon) and -180.0 <= center_lon <= 180.0):
        raise ValueError(f"AIS center longitude out of WGS 84 range: {center_lon!r}")
    if not (math.isfinite(radius_nm) and radius_nm > 0.0):
        raise ValueError(f"AIS radius must be a positive finite number of NM: {radius_nm!r}")
    d_lat = radius_nm / _NM_PER_DEG_LAT
    # Guard the cosine near the poles so longitude span stays finite.
    cos_lat = max(math.cos(math.radians(center_lat)), 0.01)
    d_lon = radius_nm / (_NM_PER_DEG_LAT * cos_lat)
    min_lat = max(center_lat - d_lat, -90.0)
    max_lat = min(center_lat + d_lat, 90.0)
    min_lon = max(center_lon - d_lon, -180.0)
    max_lon = min(center_lon + d_lon, 180.0)
    return [[[min_lat, min_lon], [max_lat, max_lon]]]


def build_subscription(api_key: str, bbox: list[list[list[float]]]) -> str:
    """Build the AISStream subscription JSON sent ONCE on connect (PRD §18.5).

    The API key is the only credential and travels in the subscription body (not a
    URL query or header). An empty key raises so an enabled-but-unconfigured adapter
    fails *visibly* as an ``offline`` ``ConfigError`` in :func:`run_ais` — never
    connecting anonymously and never baking in the maintainer's key (PRD §2/§37).
    The key is never logged or echoed into a status record.
    """
    key = api_key.strip()
    if not key:
        raise ValueError("AISStream API key is required (set AETHER_AIS_API_KEY)")
    return json.dumps({"APIKey": key, "BoundingBoxes": bbox})


# --- message decoding ---------------------------------------------------------


def _metadata(envelope: dict[str, Any]) -> dict[str, Any]:
    """AISStream's per-message metadata block (spelled ``MetaData``; tolerate either)."""
    meta = envelope.get("MetaData")
    if not isinstance(meta, dict):
        meta = envelope.get("Metadata")
    return meta if isinstance(meta, dict) else {}


def _message_body(envelope: dict[str, Any]) -> tuple[str | None, dict[str, Any]]:
    """The message type and its nested body (``Message[<MessageType>]``)."""
    msg_type = envelope.get("MessageType")
    if not isinstance(msg_type, str):
        return None, {}
    message = envelope.get("Message")
    body = message.get(msg_type) if isinstance(message, dict) else None
    return msg_type, body if isinstance(body, dict) else {}


def _mmsi(meta: dict[str, Any], body: dict[str, Any]) -> str | None:
    """Stable vessel identity: the MMSI as a string (PRD §15.1 ``ais:vessel:<MMSI>``)."""
    raw = meta.get("MMSI")
    if raw is None:
        raw = body.get("UserID")
    if isinstance(raw, bool) or not isinstance(raw, (int, str)):
        return None
    text = str(raw).strip()
    return text or None


def _coord(primary: Any, fallback: Any) -> float | None:
    for value in (primary, fallback):
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
            return float(value)
    return None


def _ship_type_text(code: Any) -> str | None:
    """ITU-R M.1371 vessel-type code → coarse category label (standard ranges)."""
    if not isinstance(code, int) or isinstance(code, bool) or not 1 <= code <= 99:
        return None
    specials = {
        30: "fishing",
        31: "towing",
        32: "towing_long",
        33: "dredging",
        34: "diving",
        35: "military",
        36: "sailing",
        37: "pleasure_craft",
        50: "pilot",
        51: "search_and_rescue",
        52: "tug",
        53: "port_tender",
        54: "anti_pollution",
        55: "law_enforcement",
        58: "medical_transport",
    }
    if code in specials:
        return specials[code]
    ranges = {2: "wing_in_ground", 4: "high_speed_craft", 6: "passenger", 7: "cargo", 8: "tanker"}
    return ranges.get(code // 10, "other")


def _parse_ais_time(value: Any) -> datetime | None:
    """Parse AISStream's ``time_utc`` (e.g. ``2022-12-29 18:22:32.318353 +0000 UTC``)."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if text.endswith(" UTC"):
        text = text[:-4].strip()
    for fmt in ("%Y-%m-%d %H:%M:%S.%f %z", "%Y-%m-%d %H:%M:%S %z"):
        try:
            return datetime.strptime(text, fmt).astimezone(UTC)
        except ValueError:
            continue
    return None


@dataclass(frozen=True)
class _VesselPos:
    point: Point
    speed_mps: float | None
    heading_deg: float | None
    nav_status: int | None


def _extract_position(
    msg_type: str | None, body: dict[str, Any], meta: dict[str, Any]
) -> _VesselPos | None:
    """Pull a position fix + dynamics from a position-class message, or ``None``.

    Coordinates prefer the message body, falling back to the metadata copy; an
    out-of-range or sentinel value yields no position. Speed (knots) is normalized
    to m/s; heading prefers true heading, falling back to course over ground; each
    drops to ``None`` at its ITU "not available" sentinel.
    """
    if msg_type not in _POSITION_TYPES:
        return None
    lat = _coord(body.get("Latitude"), meta.get("latitude"))
    lon = _coord(body.get("Longitude"), meta.get("longitude"))
    if lat is None or lon is None or not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        return None
    sog = body.get("Sog")
    speed_mps = (
        float(sog) * _KT_TO_MS
        if isinstance(sog, (int, float)) and not isinstance(sog, bool) and 0.0 <= sog < _SOG_NA
        else None
    )
    heading_deg: float | None = None
    th = body.get("TrueHeading")
    cog = body.get("Cog")
    if isinstance(th, (int, float)) and not isinstance(th, bool) and 0 <= th < _HEADING_NA:
        heading_deg = float(th)
    elif isinstance(cog, (int, float)) and not isinstance(cog, bool) and 0.0 <= cog < _COG_NA:
        heading_deg = float(cog)
    nav = body.get("NavigationalStatus")
    nav_status = nav if isinstance(nav, int) and not isinstance(nav, bool) else None
    return _VesselPos(Point(coordinates=[lon, lat]), speed_mps, heading_deg, nav_status)


def _merge_static(static: dict[str, Any], msg_type: str | None, body: dict[str, Any]) -> None:
    """Fold static/voyage fields into the per-MMSI accumulator (present keys only).

    Never clobbers a known value with a missing one (PRD §17.2 "avoid clearing known
    fields"). ``ShipStaticData`` carries the full set; ``ExtendedClassB`` reports
    carry name/type/dimensions alongside their position.
    """
    name = body.get("Name") or body.get("ShipName")
    if isinstance(name, str) and name.strip():
        static["vessel_name"] = name.strip()
    callsign = body.get("CallSign")
    if isinstance(callsign, str) and callsign.strip():
        static["callsign"] = callsign.strip()
    imo = body.get("ImoNumber")
    if isinstance(imo, int) and not isinstance(imo, bool) and imo > 0:
        static["imo"] = str(imo)
    destination = body.get("Destination")
    if isinstance(destination, str) and destination.strip():
        static["destination"] = destination.strip()
    ship_type = body.get("Type")
    if isinstance(ship_type, int) and not isinstance(ship_type, bool) and ship_type > 0:
        static["ship_type"] = ship_type
        text = _ship_type_text(ship_type)
        if text:
            static["ship_type_text"] = text
    draught = body.get("MaximumStaticDraught")
    if isinstance(draught, (int, float)) and not isinstance(draught, bool) and draught > 0:
        static["draught_m"] = float(draught)
    dim = body.get("Dimension")
    if isinstance(dim, dict):
        a, b, c, d = dim.get("A"), dim.get("B"), dim.get("C"), dim.get("D")
        # Each offset is a non-negative distance (ITU-R M.1371); a single component may
        # legitimately be 0 (reference point on an edge), but a negative is garbage.
        if (
            isinstance(a, (int, float))
            and not isinstance(a, bool)
            and a >= 0
            and isinstance(b, (int, float))
            and not isinstance(b, bool)
            and b >= 0
            and isinstance(c, (int, float))
            and not isinstance(c, bool)
            and c >= 0
            and isinstance(d, (int, float))
            and not isinstance(d, bool)
            and d >= 0
        ):
            static["length_m"] = float(a) + float(b)  # bow-to-stern (A + B)
            static["beam_m"] = float(c) + float(d)  # port-to-starboard (C + D)


class VesselMerger:
    """Merges AIS dynamic-position and static/voyage messages into one track per MMSI.

    AISStream delivers a vessel's position (every few seconds) and its static/voyage
    data (name, type, destination — every few minutes) as *separate* messages
    (PRD §18.5 / AIS-FR-003). This keeps a per-MMSI static accumulator and emits a
    :class:`TrackRecord` only for position-bearing messages, folding in the latest
    known static fields. A static-only message updates the accumulator and rides out
    on the next position — so the track is never blanked to a position-less point and
    its label/type catch up within one position interval.
    """

    def __init__(
        self, *, ttl_s: float = _MERGER_TTL_S, max_entries: int = _MERGER_MAX_ENTRIES
    ) -> None:
        self._ttl_s = ttl_s
        self._max_entries = max_entries
        self._static: dict[str, dict[str, Any]] = {}
        self._last_seen: dict[str, datetime] = {}

    def update(self, envelope: dict[str, Any], *, received_at: datetime) -> TrackRecord | None:
        msg_type, body = _message_body(envelope)
        meta = _metadata(envelope)
        mmsi = _mmsi(meta, body)
        if mmsi is None:
            return None
        self._evict(received_at, incoming=mmsi)
        self._last_seen[mmsi] = received_at
        static = self._static.setdefault(mmsi, {})
        _merge_static(static, msg_type, body)
        pos = _extract_position(msg_type, body, meta)
        if pos is None:
            return None  # static-only / unsupported type: accumulator updated, nothing to plot
        observed_at = _parse_ais_time(meta.get("time_utc")) or received_at
        return self._build(mmsi, static, pos, observed_at, received_at)

    def _evict(self, now: datetime, *, incoming: str) -> None:
        """Drop MMSIs unheard for ``ttl_s``, then oldest-first past the size cap.

        Mirrors :meth:`~aether.adapters.aprs_is.DuplicateFilter._evict`: the size
        reserve is conditional on ``incoming`` being a new MMSI, so re-seeing a known
        vessel reuses its slot and never over-evicts an unrelated one at capacity.
        Both maps are kept in lock-step so the static accumulator stays bounded.
        """
        dead = [m for m, t in self._last_seen.items() if (now - t).total_seconds() > self._ttl_s]
        for mmsi in dead:
            del self._last_seen[mmsi]
            self._static.pop(mmsi, None)
        reserve = 0 if incoming in self._last_seen else 1
        overflow = len(self._last_seen) + reserve - self._max_entries
        if overflow > 0:
            oldest = sorted(self._last_seen, key=self._last_seen.__getitem__)[:overflow]
            for mmsi in oldest:
                del self._last_seen[mmsi]
                self._static.pop(mmsi, None)

    @staticmethod
    def _build(
        mmsi: str,
        static: dict[str, Any],
        pos: _VesselPos,
        observed_at: datetime,
        received_at: datetime,
    ) -> TrackRecord:
        record_id = f"ais:vessel:{mmsi}"
        attributes: dict[str, Any] = {"mmsi": mmsi, **static}
        if pos.nav_status is not None:
            attributes["nav_status"] = pos.nav_status
            text = _NAV_STATUS.get(pos.nav_status)
            if text:
                attributes["nav_status_text"] = text
        return TrackRecord(
            id=record_id,
            source=SOURCE,
            observed_at=observed_at,
            received_at=received_at,
            published_at=received_at,
            correlation_key=record_id,
            track_type="vessel",
            label=static.get("vessel_name") or mmsi,
            geometry=pos.point,
            speed_mps=pos.speed_mps,
            heading_deg=pos.heading_deg,
            locally_received=False,  # AIS is a network-only Internet feed (no local RF)
            attributes=attributes,
            provenance=[
                Provenance(
                    source=SOURCE,
                    provider=_PROVIDER,
                    observed_at=observed_at,
                    received_at=received_at,
                    local_rf=False,
                )
            ],
        )


def dup_signature(envelope: dict[str, Any]) -> str | None:
    """Signature identifying the same AIS broadcast re-reported by many receivers.

    ``<MMSI>:<MessageType>:<time_utc>`` — a distinct broadcast has a distinct
    ``time_utc`` and is admitted; the same one relayed by another receiver collapses.
    Returns ``None`` (skip dedup) when MMSI or broadcast time is absent, so a missing
    timestamp can never collapse genuinely distinct messages to one signature.
    """
    meta = _metadata(envelope)
    msg_type, body = _message_body(envelope)
    mmsi = _mmsi(meta, body)
    time_utc = meta.get("time_utc")
    if mmsi is None or not isinstance(time_utc, str) or not time_utc.strip():
        return None
    return f"{mmsi}:{msg_type}:{time_utc}"


# --- WebSocket source ---------------------------------------------------------


class AisStreamSource:
    """Connects to the AISStream WebSocket, subscribes, and yields raw JSON frames.

    Owns the connection lifecycle: connect (bounded by ``timeout_s``), send the
    subscription ONCE, then read text frames until the socket closes/errors. Liveness
    is delegated to the ``websockets`` library's ping/pong keepalive — AISStream
    sends no application-level keepalive, so a quiet AOI legitimately produces no
    frames; a genuinely dead socket surfaces as ``ConnectionClosed`` on ``recv()``,
    which is mapped to ``ConnectionError`` so the runner reconnects (PRD §17.3).

    RECEIVE-ONLY: the subscription is the only thing aether ever sends, and there is
    no RF path here at all (PRD §2).
    """

    def __init__(
        self,
        url: str,
        subscription: str,
        *,
        timeout_s: float = 10.0,
    ) -> None:
        self._url = url
        self._subscription = subscription
        self._timeout_s = timeout_s
        self._ws: Any = None  # websockets client connection (typed Any at the lib boundary)

    @property
    def url(self) -> str:
        return self._url

    async def messages(self) -> AsyncIterator[str]:
        """Connect, send the subscription, then yield stripped non-empty JSON frames.

        Raises ``ConnectionError`` on a failed/slow handshake, a closed socket, or an
        over-cap frame — each recoverable by reconnecting (re-subscribe).
        """
        try:
            ws = await websockets.connect(
                self._url, open_timeout=self._timeout_s, max_size=_MAX_FRAME_BYTES
            )
        except (OSError, WebSocketException, TimeoutError) as exc:
            raise ConnectionError(f"AISStream connect failed: {exc}") from exc
        self._ws = ws
        try:
            await ws.send(self._subscription)
        except (OSError, WebSocketException) as exc:
            raise ConnectionError(f"AISStream subscribe failed: {exc}") from exc
        while True:
            try:
                raw = await ws.recv()
            except ConnectionClosed as exc:
                raise ConnectionError(f"AISStream socket closed: {exc}") from exc
            except (OSError, WebSocketException) as exc:
                raise ConnectionError(f"AISStream socket error: {exc}") from exc
            text = raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else raw
            line = text.strip()
            if line:
                yield line

    async def close(self) -> None:
        ws = self._ws
        self._ws = None
        if ws is None:
            return
        try:
            await ws.close()
        except (OSError, WebSocketException):
            pass


async def ais_records(
    source: AisStreamSource,
    *,
    throttle_s: float = 1.0,
    dup_ttl_s: float = _DUP_TTL_S,
) -> AsyncIterator[Record]:
    """Yield the AIS record stream: status, then deduped/merged/throttled tracks + health.

    Emits ``starting`` immediately, then connects/subscribes and streams. For each
    JSON frame: an exact re-report (same MMSI/type/broadcast-time) is dropped
    (PRD §18.5); otherwise it is merged into its per-MMSI track (folding static
    name/type/voyage into the live position, PRD §18.5), throttled per vessel
    (§18.1), and yielded with a ``connected`` status carrying running counts. A frame
    that is not JSON, or carries no plottable position yet (static-only), is a
    ``records_rejected``. A socket error yields ``degraded``, backs off with jitter,
    and RE-OPENS a fresh connection (re-subscribe) — one drop never ends the stream
    (PRD §17.4, §37 failure isolation).
    """
    yield _status("starting", _now())
    gate = ThrottleGate(throttle_s)
    dedup = DuplicateFilter(ttl_s=dup_ttl_s)
    merger = VesselMerger()
    received = 0
    rejected = 0
    duplicates = 0
    backoff = INITIAL_BACKOFF_S
    attrs: dict[str, Any] = {"connection": "aisstream", "url": source.url}

    def health(now: datetime, **extra: Any) -> SourceStatusRecord:
        return _status(
            "connected",
            now,
            records_received=received,
            records_rejected=rejected,
            attributes={**attrs, "duplicates": duplicates, **extra},
        )

    while True:
        try:
            async for raw in source.messages():
                backoff = INITIAL_BACKOFF_S  # a live read means we're connected
                now = _now()
                try:
                    envelope = json.loads(raw)
                except (ValueError, TypeError):
                    log.warning("skipping malformed AIS frame", exc_info=True)
                    rejected += 1
                    yield health(now)
                    continue
                if not isinstance(envelope, dict):
                    rejected += 1
                    yield health(now)
                    continue
                signature = dup_signature(envelope)
                if signature is not None and not dedup.admit(signature, now):
                    duplicates += 1  # same broadcast re-reported by another receiver
                    continue
                try:
                    track = merger.update(envelope, received_at=now)
                except Exception:  # one bad message must not drop the rest of the stream
                    log.warning("skipping unparseable AIS message", exc_info=True)
                    rejected += 1
                    yield health(now)
                    continue
                if track is None:
                    # Static-only / unsupported type: accumulator updated, nothing to plot.
                    rejected += 1
                    yield health(now)
                    continue
                if gate.admit(track.id, now, emergency=False):
                    received += 1
                    yield track
                    yield _status(
                        "connected",
                        now,
                        records_received=received,
                        records_rejected=rejected,
                        last_record_at=track.observed_at,
                        attributes={**attrs, "duplicates": duplicates},
                    )
        except (TimeoutError, ConnectionError, OSError, ValueError) as exc:
            now = _now()
            log.warning("AIS connection error (%s); backing off", exc)
            yield _status(
                "degraded",
                now,
                records_received=received,
                records_rejected=rejected,
                error_code=type(exc).__name__,
                error_summary=str(exc)[:200],
                attributes={**attrs, "duplicates": duplicates},
            )
            await source.close()
            sleep_for, backoff = _backoff(backoff)
            await asyncio.sleep(sleep_for)
            continue  # re-open a fresh connection (re-subscribe)
        # messages() returned without raising (clean EOF): treat like a drop, reconnect.
        # The production AisStreamSource.messages() never returns cleanly (it only
        # raises), so this guards a future/test source — logged for parity so a silent
        # reconnect is never invisible.
        log.warning("AIS stream ended without error; reconnecting")
        await source.close()
        sleep_for, backoff = _backoff(backoff)
        await asyncio.sleep(sleep_for)


def _build_url(cfg: Settings) -> str:
    scheme = "wss" if cfg.ais_tls else "ws"
    return f"{scheme}://{cfg.ais_host}:{cfg.ais_port}{cfg.ais_path}"


async def run_ais(
    cfg: Settings,
    ready: asyncio.Event,
    *,
    throttle_s: float | None = None,
) -> None:
    """Pump the AIS stream onto the bus until cancelled (PRD §17.1).

    Waits for the subscriber to be live (avoids a startup race), then publishes the
    :func:`ais_records` stream. A broker drop triggers a jittered exponential
    reconnect rather than crashing the lifespan.

    A FRESH records generator (and a fresh :class:`AisStreamSource`) is built per bus
    connection: an ``MqttError`` mid-publish unwinds the ``async for`` and (PEP 525)
    closes the generator, which cannot be resumed (the M2.1b lesson). The
    AOI/subscription are rebuilt inside the loop so a misconfiguration (missing API
    key, bad AOI) is reported once as an ``offline`` source status and the task then
    exits cleanly — a config error will not self-heal, so we do not spin.
    """
    await ready.wait()
    resolved_throttle = throttle_s if throttle_s is not None else cfg.ais_throttle_s
    backoff = INITIAL_BACKOFF_S
    while True:
        try:
            async with connect(cfg, identifier="aether-ais") as bus:
                backoff = INITIAL_BACKOFF_S  # reset once connected
                try:
                    bbox = ais_bbox(cfg.ais_center_lat, cfg.ais_center_lon, cfg.ais_radius_nm)
                    subscription = build_subscription(cfg.ais_api_key, bbox)
                except ValueError as exc:
                    log.error("AIS misconfigured: %s", exc)
                    await bus.publish_record(
                        _status(
                            "offline",
                            _now(),
                            error_code="ConfigError",
                            error_summary=str(exc)[:200],
                            attributes={"connection": "aisstream"},
                        )
                    )
                    return  # config won't self-heal; don't spin
                url = _build_url(cfg)
                log.info(
                    "AIS adapter -> %s (AOI %.0f NM @ %.4f,%.4f)",
                    url,
                    cfg.ais_radius_nm,
                    cfg.ais_center_lat,
                    cfg.ais_center_lon,
                )
                source = AisStreamSource(url, subscription, timeout_s=cfg.ais_timeout_s)
                async for record in ais_records(source, throttle_s=resolved_throttle):
                    await bus.publish_record(record)
                return  # generator exhausted (only on cancellation in practice)
        except aiomqtt.MqttError as exc:
            sleep_for, backoff = _backoff(backoff)
            log.warning("AIS lost broker (%s); reconnecting in %.1fs", exc, sleep_for)
            await asyncio.sleep(sleep_for)

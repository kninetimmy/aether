"""Per-connection display-stream filtering (PRD §16.3 browser→subscribe, §22.2).

A websocket client may ``subscribe`` to narrow the snapshot+delta stream to a
viewport ``bbox``, a set of ``sources``, and a set of ``track_types`` (and toggle
events/alerts). This module is the FastAPI-free core of that: a :class:`ClientFilter`
dataclass plus :func:`parse_subscribe`, so the wire-frame validation and the
``matches`` truth table can be unit-tested without a socket.

Scope (PRD §16.3 interpretation (a)): this is per-connection DISPLAY-stream
filtering only — the upstream provider re-filter (step 4) and the ingest-side
default when no browser is connected (interpretation (b)) are separate later
slices. The default filter built at connect is station-centered from the canonical
``AETHER_STATION_*`` config; an unconfigured 0,0 station degrades to UNBOUNDED,
never a degenerate null-island zero-area box (PRD §5: no station coordinates in
the repo).

Failure isolation (PRD §37): a malformed ``subscribe`` frame yields ``None`` so the
caller keeps the prior filter and never raises out of the receive loop; ``matches``
is total (it never raises) so the broadcast loop can skip-on-error per client.
"""

import logging
import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from aether.config import Settings
    from aether.state.live import StateChange

log = logging.getLogger(__name__)

#: One nautical mile in degrees of latitude (≈ 60 NM per degree).
_NM_PER_DEG_LAT = 60.0

# A bbox is GeoJSON order [minLon, minLat, maxLon, maxLat] (lon FIRST — guard the
# classic lon/lat swap). ``None`` everywhere means UNBOUNDED.
Bbox = tuple[float, float, float, float]


def _finite(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def station_bbox(settings: "Settings") -> Bbox | None:
    """The default per-connection bbox from the canonical station config.

    Returns ``None`` (UNBOUNDED) when the station is unconfigured (the 0,0
    null-island sentinel), so a default client sees everything rather than an
    empty zero-area box at null island (PRD §5). Otherwise a lat/lon box of
    ``radius_nm`` about the station, clamped to WGS84 bounds.
    """
    lat, lon, radius_nm = (
        settings.station_lat,
        settings.station_lon,
        settings.station_radius_nm,
    )
    if lat == 0.0 and lon == 0.0:
        return None
    if radius_nm <= 0.0:
        return None
    d_lat = radius_nm / _NM_PER_DEG_LAT
    cos_lat = math.cos(math.radians(lat))
    # Near the poles cos→0; fall back to an unbounded longitude span there.
    d_lon = 180.0 if cos_lat < 1e-6 else min(180.0, d_lat / cos_lat)
    return (
        max(-180.0, lon - d_lon),
        max(-90.0, lat - d_lat),
        min(180.0, lon + d_lon),
        min(90.0, lat + d_lat),
    )


@dataclass(frozen=True)
class ClientFilter:
    """A per-connection display-stream filter (PRD §22.2).

    ``None`` for ``bbox``/``sources``/``track_types`` means "unbounded / all".
    ``matches`` is total and never raises so the broadcast loop can apply it per
    client behind a try/except without aborting the fan-out for everyone else.
    """

    bbox: Bbox | None = None
    sources: frozenset[str] | None = None
    track_types: frozenset[str] | None = None
    include_events: bool = True
    include_alerts: bool = True

    def matches(self, change: "StateChange") -> bool:
        """Does this state change belong on this connection's stream?

        ``remove`` carries no record, so it is not bbox/source/type-decidable here;
        the Hub force-forwards a remove for any id it has actually sent. This is
        otherwise a thin delegate to :meth:`matches_record` (the snapshot path uses
        the latter directly, having only ``(kind, record)`` and no ``StateChange``).
        """
        if change.record is None:  # a remove — decided by the Hub's sent_ids
            return True
        return self.matches_record(change.kind, change.record)

    def matches_record(self, kind: str, record: Any) -> bool:
        """Does a single ``(kind, record)`` pass this filter? (PRD §22.2.)

        - ``source_status`` ALWAYS passes — health reaches every client regardless
          of bbox/type, so a filtered viewport never hides a dead source.
        - ``alert`` is gated by ``include_alerts`` only (alerts carry no geometry).
        - ``event`` is gated by ``include_events`` AND (no geometry OR bbox-hit).
        - ``track`` is gated by ``source ∈ sources`` AND ``track_type ∈
          track_types`` AND (geometry None OR point-in-bbox); a ``None`` geometry
          PASSES so a just-acquired track isn't hidden.
        - ``feature`` is gated by ``source ∈ sources`` AND geometry-intersects-bbox.
        """
        if kind == "source_status":
            return True
        if kind == "alert":
            return self.include_alerts
        if kind == "event":
            if not self.include_events:
                return False
            geom = getattr(record, "geometry", None)
            return geom is None or self._geometry_in_bbox(geom)
        if kind == "track":
            if not self._source_ok(record) or not self._track_type_ok(record):
                return False
            geom = getattr(record, "geometry", None)
            return geom is None or self._geometry_in_bbox(geom)
        if kind == "feature":
            if not self._source_ok(record):
                return False
            geom = getattr(record, "geometry", None)
            return geom is not None and self._geometry_in_bbox(geom)
        return True  # unknown kind: don't silently drop (PRD §13.5 generic fallback)

    def _source_ok(self, record: Any) -> bool:
        return self.sources is None or getattr(record, "source", None) in self.sources

    def _track_type_ok(self, record: Any) -> bool:
        if self.track_types is None:
            return True
        return getattr(record, "track_type", None) in self.track_types

    def _geometry_in_bbox(self, geometry: Any) -> bool:
        """True when the geometry intersects ``self.bbox`` (unbounded → always)."""
        if self.bbox is None:
            return True
        coords = getattr(geometry, "coordinates", None)
        if coords is None:
            return True  # no coordinates → can't exclude; keep it
        return _coords_intersect_bbox(coords, self.bbox)


def _point_in_bbox(lon: float, lat: float, bbox: Bbox) -> bool:
    min_lon, min_lat, max_lon, max_lat = bbox
    if not min_lat <= lat <= max_lat:
        return False
    if min_lon <= max_lon:
        return min_lon <= lon <= max_lon
    # Antimeridian-crossing box (minLon > maxLon): the longitude range is the
    # UNION of [minLon, 180] and [-180, maxLon].
    return lon >= min_lon or lon <= max_lon


def _flatten_positions(coords: Any) -> list[tuple[float, float]]:
    """Pull every [lon, lat] vertex out of arbitrarily-nested GeoJSON coordinates."""
    out: list[tuple[float, float]] = []
    if (
        isinstance(coords, list | tuple)
        and len(coords) >= 2
        and _finite(coords[0])
        and _finite(coords[1])
    ):
        out.append((float(coords[0]), float(coords[1])))
        return out
    if isinstance(coords, list | tuple):
        for item in coords:
            out.extend(_flatten_positions(item))
    return out


def _coords_intersect_bbox(coords: Any, bbox: Bbox) -> bool:
    """Conservative bbox test: any vertex inside the box counts as an intersection.

    A vertex-only test can miss a giant polygon whose edges straddle the box with
    no vertex inside, but for the geometry sizes here (tracks are points; TFRs and
    lightning clusters are small relative to a viewport) any straddling edge has a
    vertex in or near the box. A straddling-edge feature thus passes via its
    in-box vertices; this never WRONGLY hides a point (a point is its own vertex).
    """
    for lon, lat in _flatten_positions(coords):
        if _point_in_bbox(lon, lat, bbox):
            return True
    return False


def _intersect_bbox(a: Bbox, b: Bbox) -> Bbox | None:
    """Intersect two non-antimeridian boxes; ``None`` if they don't overlap."""
    min_lon = max(a[0], b[0])
    min_lat = max(a[1], b[1])
    max_lon = min(a[2], b[2])
    max_lat = min(a[3], b[3])
    if min_lon > max_lon or min_lat > max_lat:
        return None
    return (min_lon, min_lat, max_lon, max_lat)


def _parse_bbox(raw: Any) -> Bbox | None:
    """Validate a subscribe bbox: 4 finite floats, WGS84, minLat<=maxLat.

    GeoJSON order [minLon, minLat, maxLon, maxLat]. ``minLon > maxLon`` is allowed
    (an antimeridian-crossing viewport); ``minLat > maxLat`` is rejected (latitude
    never wraps). Mirrors ``schema.geometry._validate_position`` WGS84 semantics.
    Returns ``None`` on any violation (caller keeps prior filter).
    """
    if not isinstance(raw, list | tuple) or len(raw) != 4:
        return None
    if not all(_finite(v) for v in raw):
        return None
    min_lon, min_lat, max_lon, max_lat = (float(v) for v in raw)
    if not (-180.0 <= min_lon <= 180.0 and -180.0 <= max_lon <= 180.0):
        return None
    if not (-90.0 <= min_lat <= 90.0 and -90.0 <= max_lat <= 90.0):
        return None
    if min_lat > max_lat:  # latitude never wraps
        return None
    return (min_lon, min_lat, max_lon, max_lat)


def _parse_str_set(raw: Any) -> frozenset[str] | None:
    """A subscribe ``sources``/``track_types`` value: a list of strings or null.

    ``null``/missing → ``None`` (no constraint). An empty list is a real constraint
    that matches nothing; a non-list or non-string element → reject the whole frame.
    """
    if raw is None:
        return None
    if not isinstance(raw, list | tuple):
        raise ValueError("must be a list of strings or null")
    items: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            raise ValueError("list elements must be strings")
        items.append(item)
    return frozenset(items)


def parse_subscribe(frame: Any, settings: "Settings") -> ClientFilter | None:
    """Parse + validate a ``subscribe`` frame into a :class:`ClientFilter`.

    Returns ``None`` on any malformation so the caller keeps the prior filter and
    never raises out of the receive loop (PRD §37). A valid bbox is intersected
    with the station's max AOI so a client can narrow but never widen beyond the
    configured station scope; an empty intersection collapses to the station box.
    A null bbox means "the station default" (intersection with the station box is
    just the station box).
    """
    if not isinstance(frame, dict) or frame.get("type") != "subscribe":
        return None
    try:
        sources = _parse_str_set(frame.get("sources"))
        track_types = _parse_str_set(frame.get("track_types"))
    except ValueError as exc:
        log.warning("ignoring malformed subscribe (sources/track_types): %s", exc)
        return None

    raw_bbox = frame.get("bbox")
    station = station_bbox(settings)
    if raw_bbox is None:
        bbox = station  # null bbox → station default (or unbounded when unset)
    else:
        parsed = _parse_bbox(raw_bbox)
        if parsed is None:
            log.warning("ignoring malformed subscribe (bbox): %r", raw_bbox)
            return None
        bbox = _clamp_to_station(parsed, station)

    include_events = bool(frame.get("include_events", True))
    include_alerts = bool(frame.get("include_alerts", True))
    return ClientFilter(
        bbox=bbox,
        sources=sources,
        track_types=track_types,
        include_events=include_events,
        include_alerts=include_alerts,
    )


def _clamp_to_station(requested: Bbox, station: Bbox | None) -> Bbox:
    """Intersect a requested bbox with the station AOI (PRD §16.3: narrow only).

    Antimeridian-crossing requests (minLon > maxLon) skip the clamp — intersecting
    a wrapped box is ill-defined here, and the station AOI is a coarse cap, not a
    security boundary. An empty intersection falls back to the station box so the
    client still sees its scoped world rather than nothing.
    """
    if station is None:
        return requested
    if requested[0] > requested[2] or station[0] > station[2]:
        return requested
    clipped = _intersect_bbox(requested, station)
    return clipped if clipped is not None else station


def default_filter(settings: "Settings") -> ClientFilter:
    """The per-connection default applied at ``register`` before any subscribe.

    Station-scoped bbox (or unbounded when the station is unconfigured); all
    sources/types, events and alerts on — i.e. the full station picture.
    """
    return ClientFilter(bbox=station_bbox(settings))

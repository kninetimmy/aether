"""Local ADS-B (`readsb`) snapshot parser (PRD §18.1).

Pure, hardware-free normalization of a readsb/dump1090 ``aircraft.json`` snapshot
into schema v2 :class:`~aether.schema.records.TrackRecord` objects. The runner
that polls the file/URL, throttles, publishes source health, and reconnects is a
separate slice; this module is just the edge mapping so it can be fully unit
tested against fixtures with no SDR.

readsb is the operator's *own* antenna, so every track is tagged
``locally_received=True`` with a ``local_rf=True`` provenance entry — the
load-bearing distinction the COP makes between "my radio heard this" and "an
Internet feed reported it" (PRD §8.2). Aircraft identity is the ICAO 24-bit
address (``aircraft:icao:<hex>``, PRD §15.1), set as both ``id`` and
``correlation_key`` so a network observation of the same airframe fuses onto it
in M3 (PRD §11.4) rather than appearing twice.

Adapter rules honored (PRD §17.2): source-native units are normalized to SI,
impossible coordinates are rejected (geometry dropped, identity kept), native
fields are preserved in ``attributes`` rather than discarded, and one malformed
aircraft entry is skipped without dropping the rest of the snapshot.
"""

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from aether.schema.geometry import Point
from aether.schema.provenance import Provenance
from aether.schema.records import TrackRecord

log = logging.getLogger(__name__)

#: Per-source identifier; the MQTT topic suffix (PRD §23: ``records/local_adsb``).
SOURCE = "local_adsb"

#: Unit conversions from readsb's aviation units to schema-v2 SI.
_FT_TO_M = 0.3048
_KT_TO_MS = 1852.0 / 3600.0  # 1 knot = 1 NM/h
_FPM_TO_MS = _FT_TO_M / 60.0  # feet per minute -> m/s

#: Transponder emergency codes (PRD §11.2, §12 emergency template basis).
EMERGENCY_SQUAWKS = frozenset({"7500", "7600", "7700"})


def _num(value: object) -> float | None:
    """Coerce a JSON number to float, rejecting bools and non-numbers."""
    if isinstance(value, bool):  # bool is an int subclass; never an altitude/speed
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _epoch_to_dt(value: object) -> datetime | None:
    """Convert a Unix epoch-seconds number to an aware UTC datetime."""
    secs = _num(value)
    if secs is None:
        return None
    return datetime.fromtimestamp(secs, tz=UTC)


def _valid_lonlat(lon: float | None, lat: float | None) -> bool:
    """A position is usable only if both axes are present and within WGS 84 bounds."""
    if lon is None or lat is None:
        return False
    return -180.0 <= lon <= 180.0 and -90.0 <= lat <= 90.0


def _is_emergency(squawk: str | None, emergency: str | None) -> bool:
    """True for an emergency squawk or an explicit non-``none`` emergency field."""
    if squawk in EMERGENCY_SQUAWKS:
        return True
    return bool(emergency) and emergency != "none"


def aircraft_to_track(
    ac: dict[str, Any],
    *,
    snapshot_now: datetime,
    received_at: datetime,
    source: str = SOURCE,
) -> TrackRecord | None:
    """Normalize one ``aircraft.json`` entry into a :class:`TrackRecord`.

    Returns ``None`` when the entry carries no usable ICAO identity (nothing to
    track or fuse on). A present-but-impossible position drops the geometry only;
    the identified track is still published (PRD §17.2: avoid clearing known
    fields). Units are converted to SI; readsb-native fields are preserved under
    ``attributes``.
    """
    raw_hex = ac.get("hex")
    if not isinstance(raw_hex, str) or not raw_hex.strip():
        return None
    non_icao = raw_hex.startswith("~")  # TIS-B / non-ICAO address
    hex_id = (raw_hex[1:] if non_icao else raw_hex).strip().lower()
    if not hex_id:
        return None

    # observed_at: when the last (position) message was heard. readsb gives age in
    # seconds relative to the snapshot's ``now``; prefer position age when present.
    age_s = _num(ac.get("seen_pos"))
    if age_s is None:
        age_s = _num(ac.get("seen")) or 0.0
    observed_at = snapshot_now - timedelta(seconds=age_s)

    lon, lat = _num(ac.get("lon")), _num(ac.get("lat"))
    has_pos = _valid_lonlat(lon, lat)
    bad_pos = (lon is not None or lat is not None) and not has_pos

    # Altitude: baro is the operational/displayed altitude; "ground" means landed.
    raw_baro = ac.get("alt_baro")
    on_ground = raw_baro == "ground"
    if on_ground:
        altitude_m: float | None = 0.0
    else:
        alt_ft = _num(raw_baro)
        if alt_ft is None:
            alt_ft = _num(ac.get("alt_geom"))
        altitude_m = alt_ft * _FT_TO_M if alt_ft is not None else None

    geometry: Point | None = None
    if has_pos:
        coords = [float(lon), float(lat)]  # type: ignore[arg-type]
        if altitude_m is not None:
            coords.append(altitude_m)
        geometry = Point(coordinates=coords)

    gs_kt = _num(ac.get("gs"))
    speed_mps = gs_kt * _KT_TO_MS if gs_kt is not None else None
    heading_deg = _num(ac.get("track"))
    rate_fpm = _num(ac.get("baro_rate"))
    if rate_fpm is None:
        rate_fpm = _num(ac.get("geom_rate"))
    vertical_rate_mps = rate_fpm * _FPM_TO_MS if rate_fpm is not None else None

    squawk = ac.get("squawk") if isinstance(ac.get("squawk"), str) else None
    emergency = ac.get("emergency") if isinstance(ac.get("emergency"), str) else None
    flight = ac.get("flight")
    label = flight.strip() if isinstance(flight, str) and flight.strip() else hex_id.upper()

    tags: list[str] = []
    if _is_emergency(squawk, emergency):
        tags.append("emergency")
    if on_ground:
        tags.append("on_ground")
    if non_icao:
        tags.append("non_icao")
    if bad_pos:
        tags.append("bad_position")

    # Preserve readsb-native fields the schema has no first-class home for
    # (PRD §18.1: keep seen/messages/rssi/category/registration/type/dbFlags).
    attr_keys = (
        "squawk",
        "emergency",
        "category",
        "rssi",
        "messages",
        "seen",
        "seen_pos",
        "nic",
        "r",  # registration (database)
        "t",  # type designator (database)
        "desc",
        "dbFlags",
    )
    attributes: dict[str, Any] = {k: ac[k] for k in attr_keys if k in ac}
    if on_ground:
        attributes["on_ground"] = True

    return TrackRecord(
        id=f"aircraft:icao:{hex_id}",
        source=source,
        observed_at=observed_at,
        received_at=received_at,
        published_at=received_at,
        correlation_key=f"aircraft:icao:{hex_id}",
        track_type="aircraft",
        label=label,
        geometry=geometry,
        altitude_m=altitude_m,
        speed_mps=speed_mps,
        heading_deg=heading_deg,
        vertical_rate_mps=vertical_rate_mps,
        locally_received=True,
        tags=tags,
        attributes=attributes,
        provenance=[
            Provenance(
                source=source,
                observed_at=observed_at,
                received_at=received_at,
                local_rf=True,
            )
        ],
    )


def parse_aircraft_snapshot(
    data: dict[str, Any],
    *,
    received_at: datetime,
    source: str = SOURCE,
) -> list[TrackRecord]:
    """Normalize a full ``aircraft.json`` snapshot into a list of tracks.

    The snapshot's top-level ``now`` (epoch seconds) anchors per-aircraft message
    ages; it falls back to ``received_at`` if absent. A malformed individual entry
    is logged and skipped so one bad record never drops the whole snapshot
    (PRD §17.2, §37 failure isolation).
    """
    snapshot_now = _epoch_to_dt(data.get("now")) or received_at
    aircraft = data.get("aircraft")
    if not isinstance(aircraft, list):
        return []

    tracks: list[TrackRecord] = []
    for entry in aircraft:
        if not isinstance(entry, dict):
            continue
        try:
            track = aircraft_to_track(
                entry, snapshot_now=snapshot_now, received_at=received_at, source=source
            )
        except Exception:  # one bad aircraft must not drop the rest of the snapshot
            log.warning("skipping malformed readsb aircraft entry", exc_info=True)
            continue
        if track is not None:
            tracks.append(track)
    return tracks

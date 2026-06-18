"""APRS (TNC2) packet parser (PRD §18.3, §18.4).

Pure, hardware-free normalization of decoded APRS traffic — TNC2 monitor lines —
into schema v2 :class:`~aether.schema.records.TrackRecord` objects. It is the
*shared* edge mapping for both APRS sources, because both deliver the same TNC2
``SRC>DEST,PATH:INFO`` line shape: the local adapter (:mod:`aether.adapters.local_aprs`)
decodes Dire Wolf KISS/AX.25 frames into TNC2 lines, and the APRS-IS display
adapter (:mod:`aether.adapters.aprs_is`, M3.4) reads TNC2 lines straight off the
APRS-IS socket. Keeping one parser avoids two APRS decoders drifting apart and
guarantees both sources mint the *same* identity key, which is what lets fusion
collapse them. The module is pure so it unit-tests against fixture packets with no
SDR, no Dire Wolf, and no network.

The ``local_rf`` flag is the one thing that differs between the two callers and
the load-bearing distinction the COP makes between "my radio heard this" and "an
Internet feed reported it" (PRD §8.2): the local adapter parses with the default
``local_rf=True`` (the operator's *own* 144.39 MHz antenna), so every record is
tagged ``locally_received=True`` with a ``local_rf=True`` provenance entry; the
APRS-IS adapter passes ``local_rf=False`` so its observations are network-only
until fused with a local one. Identity follows PRD §15.1: a transmitting station
is ``aprs:station:<CALLSIGN-SSID>`` and an object/item is ``aprs:object:<NAME>``,
set as both ``id`` and ``correlation_key`` — independent of the source — so a
local-RF and an APRS-IS observation of the same identity fuse into one track
(PRD §15.3) rather than appearing twice.

Adapter rules honored (PRD §17.2): source-native units are normalized to SI
(knots→m/s, feet→m), impossible coordinates drop the geometry while keeping the
identity, native fields are preserved under ``attributes`` rather than discarded,
and one malformed packet is skipped without dropping the rest of a batch.

Scope (first cut): uncompressed and compressed positions, objects, items, status,
and weather (positionless and position-attached). Mic-E, telemetry, messages, and
raw-GPS frames are recognized and skipped (return ``None``) — they are tracked as
follow-up work, not silently mis-parsed.
"""

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Literal

from aether.schema.geometry import Point
from aether.schema.provenance import Provenance
from aether.schema.records import TrackRecord

log = logging.getLogger(__name__)

#: Per-source identifier; the MQTT topic suffix (PRD §23: ``records/local_aprs``).
SOURCE = "local_aprs"

#: APRS packets are tiny; anything larger than this is not a real frame (PRD §17.2
#: payload-size limits). A guard against a pathological line wedging a regex.
MAX_PACKET_LEN = 1024

#: Unit conversions from APRS-native units to schema-v2 SI.
_KT_TO_MS = 1852.0 / 3600.0  # 1 knot = 1 NM/h
_MPH_TO_MS = 1609.344 / 3600.0  # statute mile per hour (APRS weather wind)
_FT_TO_M = 0.3048

#: A plausible AX.25/APRS station callsign: 1–6 alphanumerics, optional ``-SSID``.
#: Lenient on SSID (APRS-IS allows alphanumeric SSIDs) but anchored so junk header
#: text is rejected before we try to treat it as a transmitting station.
_CALLSIGN_RE = re.compile(r"^[A-Z0-9]{1,6}(-[A-Z0-9]{1,2})?$")

#: Uncompressed position data extension: course/speed as ``CSE/SPD`` (deg/knots).
_CSE_SPD_RE = re.compile(r"^(\d{3})/(\d{3})")

#: Altitude comment extension ``/A=nnnnnn`` in feet, anywhere in the comment.
_ALTITUDE_RE = re.compile(r"/A=(-?\d{6})")

#: APRS data-type identifiers we deliberately do not decode in this slice. Each is
#: recognized so it is skipped cleanly rather than mis-read as a position.
_DEFERRED_TYPES = frozenset({"`", "'", ":", "T", "$", "?", "<", "#", "*", "&"})


def _valid_callsign(value: str) -> bool:
    """True for a plausibly-valid transmitting station callsign."""
    return bool(_CALLSIGN_RE.match(value))


def _base91(chars: str) -> int:
    """Decode a base-91 run (APRS compressed coordinates, PRD §18.3).

    Each byte contributes ``(ord - 33)`` as a base-91 digit, most significant
    first. Raises nothing for our callers — inputs are pre-sliced to fixed width.
    """
    value = 0
    for ch in chars:
        value = value * 91 + (ord(ch) - 33)
    return value


@dataclass(frozen=True)
class _Position:
    """A decoded APRS position with its symbol and any inline motion/altitude."""

    lat: float
    lon: float
    symbol_table: str
    symbol_code: str
    course_deg: float | None = None
    speed_kt: float | None = None
    altitude_m: float | None = None
    comment: str = ""
    ambiguous: bool = False
    is_weather: bool = False
    weather: dict[str, Any] = field(default_factory=dict)


def _valid_lonlat(lon: float, lat: float) -> bool:
    """A position is usable only within WGS 84 bounds (PRD §17.2)."""
    return -180.0 <= lon <= 180.0 and -90.0 <= lat <= 90.0


def _parse_uncompressed_latlon(lat_s: str, lon_s: str) -> tuple[float, float, bool] | None:
    """Decode ``DDMM.hhN`` / ``DDDMM.hhW`` to signed decimal degrees.

    Spaces in the minutes positions encode APRS position ambiguity; they are
    treated as zeros and the ``ambiguous`` flag is returned so the UI can show a
    coarse fix honestly. Returns ``None`` if either axis is unparseable.
    """
    if len(lat_s) != 8 or len(lon_s) != 9:
        return None
    lat_hemi, lon_hemi = lat_s[7].upper(), lon_s[8].upper()
    if lat_hemi not in "NS" or lon_hemi not in "EW":
        return None
    ambiguous = " " in lat_s[:7] or " " in lon_s[:8]
    lat_digits = lat_s[:7].replace(" ", "0")
    lon_digits = lon_s[:8].replace(" ", "0")
    try:
        lat = int(lat_digits[0:2]) + float(lat_digits[2:7]) / 60.0
        lon = int(lon_digits[0:3]) + float(lon_digits[3:8]) / 60.0
    except ValueError:
        return None
    if lat_hemi == "S":
        lat = -lat
    if lon_hemi == "W":
        lon = -lon
    if not _valid_lonlat(lon, lat):
        return None
    return lat, lon, ambiguous


def _parse_uncompressed(text: str) -> _Position | None:
    """Parse an uncompressed position payload (``DDMM.hhN/DDDMM.hhW$<ext><comment>``)."""
    if len(text) < 19:
        return None
    latlon = _parse_uncompressed_latlon(text[0:8], text[9:18])
    if latlon is None:
        return None
    lat, lon, ambiguous = latlon
    symbol_table, symbol_code = text[8], text[18]
    data = text[19:]
    is_weather = symbol_code == "_"

    course = speed = None
    rest = data
    ext = _CSE_SPD_RE.match(data)
    if ext and not is_weather:
        course_val = int(ext.group(1))
        course = float(course_val) if 1 <= course_val <= 360 else None
        speed = float(ext.group(2))  # knots
        rest = data[7:]
    elif ext and is_weather:
        rest = data[7:]  # the ddd/sss is wind, consumed by the weather parser below

    altitude_m, rest = _extract_altitude(rest)
    weather = _parse_weather(data) if is_weather else {}
    comment = "" if is_weather else rest.strip()

    return _Position(
        lat=lat,
        lon=lon,
        symbol_table=symbol_table,
        symbol_code=symbol_code,
        course_deg=course,
        speed_kt=speed,
        altitude_m=altitude_m,
        comment=comment,
        ambiguous=ambiguous,
        is_weather=is_weather,
        weather=weather,
    )


def _parse_compressed(text: str) -> _Position | None:
    """Parse a 13-byte compressed position (APRS spec ch. 9 / PRD §18.3).

    Layout: symbol-table, 4 base-91 latitude bytes, 4 base-91 longitude bytes,
    symbol-code, two course/speed (or altitude) bytes, and a compression-type
    byte. Course/speed is decoded when present; the altitude variant is detected
    via the type byte but left in ``attributes`` rather than guessed.
    """
    if len(text) < 13:
        return None
    chunk = text[:13]
    if any(not (33 <= ord(c) <= 126) for c in chunk[1:9]):
        return None  # coordinate bytes must be printable base-91
    symbol_table, symbol_code = chunk[0], chunk[9]
    lat = 90.0 - _base91(chunk[1:5]) / 380926.0
    lon = -180.0 + _base91(chunk[5:9]) / 190463.0
    if not _valid_lonlat(lon, lat):
        return None

    course = speed = altitude_m = None
    c_byte, s_byte, type_byte = chunk[10], chunk[11], chunk[12]
    if c_byte != " ":
        if (ord(type_byte) - 33) & 0x18 == 0x10:
            altitude_m = (1.002 ** ((ord(c_byte) - 33) * 91 + (ord(s_byte) - 33))) * _FT_TO_M
        else:
            course_val = (ord(c_byte) - 33) * 4
            course = float(course_val) if 1 <= course_val <= 360 else None
            speed = 1.08 ** (ord(s_byte) - 33) - 1.0  # knots

    return _Position(
        lat=lat,
        lon=lon,
        symbol_table=symbol_table,
        symbol_code=symbol_code,
        course_deg=course,
        speed_kt=speed,
        altitude_m=altitude_m,
        comment=text[13:].strip(),
        is_weather=symbol_code == "_",
    )


def _parse_position(text: str) -> _Position | None:
    """Dispatch a position payload to the compressed or uncompressed decoder.

    Per the APRS spec the two are distinguished by the first byte: a digit (or a
    space, for an ambiguous fix) begins an uncompressed latitude; anything else is
    a compressed symbol-table id.
    """
    if not text:
        return None
    first = text[0]
    if first.isdigit() or first == " ":
        return _parse_uncompressed(text)
    return _parse_compressed(text)


def _extract_altitude(comment: str) -> tuple[float | None, str]:
    """Pull a ``/A=nnnnnn`` (feet) altitude out of a comment, returning SI meters."""
    match = _ALTITUDE_RE.search(comment)
    if match is None:
        return None, comment
    altitude_m = int(match.group(1)) * _FT_TO_M
    cleaned = (comment[: match.start()] + comment[match.end() :]).strip()
    return altitude_m, cleaned


def _parse_weather(data: str) -> dict[str, Any]:
    """Parse common APRS weather fields into a dict, keeping native units explicit.

    Wind is the ``ddd/sss`` data extension (position-attached) or the ``cdddssss``
    run (positionless). Native units are kept under self-describing keys; only
    temperature gets a convenience SI value (``temp_c``) since the rest (rainfall
    hundredths-of-inch, pressure tenths-of-hPa) are reported faithfully as-is.
    """
    weather: dict[str, Any] = {}
    wind = re.match(r"(\d{3})/(\d{3})", data) or re.search(r"c(\d{3})s(\d{3})", data)
    if wind:
        weather["wind_dir_deg"] = int(wind.group(1))
        weather["wind_speed_mph"] = int(wind.group(2))
    for key, pattern in (
        ("gust_mph", r"g(\d{3})"),
        ("temp_f", r"t(-?\d{2,3})"),
        ("rain_last_hr_in", r"r(\d{3})"),
        ("rain_24h_in", r"p(\d{3})"),
        ("rain_since_midnight_in", r"P(\d{3})"),
        ("humidity_pct", r"h(\d{2})"),
        ("pressure_hpa", r"b(\d{5})"),
    ):
        match = re.search(pattern, data)
        if match is None:
            continue
        raw = int(match.group(1))
        if key in ("rain_last_hr_in", "rain_24h_in", "rain_since_midnight_in"):
            weather[key] = raw / 100.0
        elif key == "humidity_pct":
            weather[key] = 100 if raw == 0 else raw  # APRS encodes 100% as "00"
        elif key == "pressure_hpa":
            weather[key] = raw / 10.0
        else:
            weather[key] = raw
    if "temp_f" in weather:
        weather["temp_c"] = round((weather["temp_f"] - 32) * 5.0 / 9.0, 2)
    return weather


def _from_dhm(day: int, hour: int, minute: int, received_at: datetime) -> datetime | None:
    """Resolve a day/hour/minute APRS timestamp against the receipt date.

    The packet carries no month/year, so they come from ``received_at``. A time
    that lands implausibly in the future (the day-of-month hasn't arrived yet this
    month) is rolled back to the previous month.
    """
    if not (1 <= day <= 31 and 0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    try:
        candidate = received_at.replace(day=day, hour=hour, minute=minute, second=0, microsecond=0)
    except ValueError:
        return None
    if candidate - received_at > timedelta(hours=12):
        month_start = received_at.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        prev_month_end = month_start - timedelta(seconds=1)
        try:
            candidate = prev_month_end.replace(
                day=day, hour=hour, minute=minute, second=0, microsecond=0
            )
        except ValueError:
            return None
    return candidate


def _parse_timestamp(ts: str, received_at: datetime) -> datetime | None:
    """Decode an APRS timestamp (DHM zulu/local, HMS, or weather MDHM) to UTC.

    Returns ``None`` on any unrecognized or out-of-range form so the caller can
    fall back to ``received_at`` — for locally heard RF the receive time is a
    sound stand-in for the source event time.
    """
    try:
        if len(ts) >= 7 and ts[6] in "z/":  # day-hour-minute (zulu or local)
            return _from_dhm(int(ts[0:2]), int(ts[2:4]), int(ts[4:6]), received_at)
        if len(ts) >= 7 and ts[6] == "h":  # hour-minute-second, today (zulu)
            return received_at.replace(
                hour=int(ts[0:2]), minute=int(ts[2:4]), second=int(ts[4:6]), microsecond=0
            )
        if len(ts) >= 8 and ts[0:8].isdigit():  # month-day-hour-minute (positionless weather)
            month, day = int(ts[0:2]), int(ts[2:4])
            base = _from_dhm(day, int(ts[4:6]), int(ts[6:8]), received_at)
            if base is not None and 1 <= month <= 12:
                return base.replace(month=month)
            return base
    except (ValueError, OverflowError):
        return None
    return None


def _split_tnc2(line: str) -> tuple[str, str, list[str], str] | None:
    """Split a TNC2 monitor line ``SRC>DEST,PATH:INFO`` into its parts.

    Returns ``None`` for anything without a header/info boundary or a plausible
    source callsign. The info field may itself contain ``:`` (messages), so only
    the first one separates header from payload.
    """
    line = line.strip()
    if not line or len(line) > MAX_PACKET_LEN or ">" not in line or ":" not in line:
        return None
    header, _, info = line.partition(":")
    source, sep, dest_path = header.partition(">")
    if not sep:
        return None
    source = source.strip().upper()
    if not _valid_callsign(source):
        return None
    parts = dest_path.split(",")
    dest = parts[0].strip().upper()
    path = [hop.strip() for hop in parts[1:] if hop.strip()]
    return source, dest, path, info


def _build_track(
    *,
    record_id: str,
    track_type: Literal["aprs_station", "aprs_object"],
    label: str,
    source: str,
    observed_at: datetime,
    received_at: datetime,
    position: _Position | None,
    tags: list[str],
    attributes: dict[str, Any],
    local_rf: bool,
) -> TrackRecord:
    """Assemble a :class:`TrackRecord` from common APRS pieces.

    Geometry is omitted when there is no position (status, position-less weather);
    the identity is still published so the station appears and later fuses.

    ``local_rf`` sets both ``locally_received`` and the lone provenance entry's
    ``local_rf`` flag: ``True`` for the local-RF adapter, ``False`` for APRS-IS.
    """
    geometry: Point | None = None
    altitude_m = speed_mps = heading_deg = None
    if position is not None:
        altitude_m = position.altitude_m
        heading_deg = position.course_deg
        speed_mps = position.speed_kt * _KT_TO_MS if position.speed_kt is not None else None
        coords = [position.lon, position.lat]
        if altitude_m is not None:
            coords.append(altitude_m)
        geometry = Point(coordinates=coords)
        attributes["aprs_symbol"] = position.symbol_table + position.symbol_code
        if position.ambiguous:
            tags.append("position_ambiguity")
        if position.is_weather:
            tags.append("weather")
            if position.weather:
                attributes["weather"] = position.weather
        if position.comment:
            attributes["comment"] = position.comment

    return TrackRecord(
        id=record_id,
        source=source,
        observed_at=observed_at,
        received_at=received_at,
        published_at=received_at,
        correlation_key=record_id,
        track_type=track_type,
        label=label,
        geometry=geometry,
        altitude_m=altitude_m,
        speed_mps=speed_mps,
        heading_deg=heading_deg,
        locally_received=local_rf,
        tags=tags,
        attributes=attributes,
        provenance=[
            Provenance(
                source=source,
                observed_at=observed_at,
                received_at=received_at,
                local_rf=local_rf,
            )
        ],
    )


def parse_aprs_packet(
    line: str,
    *,
    received_at: datetime,
    source: str = SOURCE,
    local_rf: bool = True,
    _depth: int = 0,
) -> TrackRecord | None:
    """Normalize one decoded APRS (TNC2) line into a :class:`TrackRecord`.

    Returns ``None`` when the line is not a frame we decode in this slice (Mic-E,
    telemetry, messages, raw GPS), carries no usable identity, or is malformed.
    A third-party (``}``) frame is unwrapped once and its inner frame parsed.

    ``local_rf`` (default ``True``) flows to the record's ``locally_received`` and
    provenance ``local_rf``: the local-RF adapter keeps the default; the APRS-IS
    display adapter passes ``local_rf=False`` so its observations are network-only.
    """
    parsed = _split_tnc2(line)
    if parsed is None:
        return None
    source_call, dest, path, info = parsed
    if not info:
        return None

    data_type = info[0]

    # Third-party traffic: the payload is itself a TNC2 frame. Unwrap once. The
    # inner frame keeps the outer frame's provenance class (``local_rf``): if my
    # antenna heard the relay, I heard the relayed packet locally too.
    if data_type == "}":
        if _depth:
            return None
        inner = parse_aprs_packet(
            info[1:], received_at=received_at, source=source, local_rf=local_rf, _depth=1
        )
        if inner is not None:
            inner.tags.append("third_party")
            inner.attributes["relayed_by"] = source_call
        return inner

    if data_type in _DEFERRED_TYPES:
        log.debug("skipping unsupported APRS data type %r from %s", data_type, source_call)
        return None

    base_attrs: dict[str, Any] = {"aprs_dest": dest}
    if path:
        base_attrs["aprs_path"] = path

    # --- Object: ;NNNNNNNNN*DDHHMMzPOSITION ----------------------------------
    if data_type == ";":
        if len(info) < 18 or info[10] not in "*_":
            return None
        name = info[1:10].strip()
        if not name:
            return None
        position = _parse_position(info[18:])
        observed_at = _parse_timestamp(info[11:18], received_at) or received_at
        tags = ["object"] + (["killed"] if info[10] == "_" else [])
        base_attrs["reported_by"] = source_call
        return _build_track(
            record_id=f"aprs:object:{name}",
            track_type="aprs_object",
            label=name,
            source=source,
            observed_at=observed_at,
            received_at=received_at,
            position=position,
            tags=tags,
            attributes=base_attrs,
            local_rf=local_rf,
        )

    # --- Item: )NAME!POSITION (name 3–9 chars, delimited by ! live / _ killed)
    if data_type == ")":
        delim = next((i for i in range(4, min(len(info), 11)) if info[i] in "!_"), None)
        if delim is None:
            return None
        name = info[1:delim].strip()
        if not name:
            return None
        position = _parse_position(info[delim + 1 :])
        tags = ["item"] + (["killed"] if info[delim] == "_" else [])
        base_attrs["reported_by"] = source_call
        return _build_track(
            record_id=f"aprs:object:{name}",
            track_type="aprs_object",
            label=name,
            source=source,
            observed_at=received_at,
            received_at=received_at,
            position=position,
            tags=tags,
            attributes=base_attrs,
            local_rf=local_rf,
        )

    # --- Status: >[timestamp]text -------------------------------------------
    if data_type == ">":
        text = info[1:].strip()
        base_attrs["status"] = text
        return _build_track(
            record_id=f"aprs:station:{source_call}",
            track_type="aprs_station",
            label=source_call,
            source=source,
            observed_at=received_at,
            received_at=received_at,
            position=None,
            tags=["status"],
            attributes=base_attrs,
            local_rf=local_rf,
        )

    # --- Positionless weather: _MDHM<weather> -------------------------------
    if data_type == "_":
        observed_at = _parse_timestamp(info[1:9], received_at) or received_at
        weather = _parse_weather(info[9:])
        if weather:
            base_attrs["weather"] = weather
        return _build_track(
            record_id=f"aprs:station:{source_call}",
            track_type="aprs_station",
            label=source_call,
            source=source,
            observed_at=observed_at,
            received_at=received_at,
            position=None,
            tags=["weather"],
            attributes=base_attrs,
            local_rf=local_rf,
        )

    # --- Position with/without timestamp ------------------------------------
    if data_type in "!=":
        position = _parse_position(info[1:])
        observed_at = received_at
    elif data_type in "/@":
        position = _parse_position(info[8:])
        observed_at = _parse_timestamp(info[1:8], received_at) or received_at
    else:
        return None

    if position is None:
        return None
    return _build_track(
        record_id=f"aprs:station:{source_call}",
        track_type="aprs_station",
        label=source_call,
        source=source,
        observed_at=observed_at,
        received_at=received_at,
        position=position,
        tags=[],
        attributes=base_attrs,
        local_rf=local_rf,
    )


def parse_aprs_lines(
    lines: list[str],
    *,
    received_at: datetime,
    source: str = SOURCE,
    local_rf: bool = True,
) -> list[TrackRecord]:
    """Normalize a batch of decoded APRS lines into tracks.

    A malformed individual line is logged and skipped so one bad packet never
    drops the rest of a batch (PRD §17.2, §37 failure isolation). ``local_rf`` is
    forwarded to every record (default ``True`` for local RF; APRS-IS passes
    ``False``).
    """
    tracks: list[TrackRecord] = []
    for line in lines:
        try:
            track = parse_aprs_packet(
                line, received_at=received_at, source=source, local_rf=local_rf
            )
        except Exception:  # one bad packet must not drop the rest
            log.warning("skipping malformed APRS packet", exc_info=True)
            continue
        if track is not None:
            tracks.append(track)
    return tracks

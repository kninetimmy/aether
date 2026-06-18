"""Network ADS-B provider interface + the adsb.fi implementation (PRD §18.2).

Internet ADS-B feeds report the *same airframes* the local radio hears, so they
must normalize to the exact identity the local adapter uses — ``aircraft:icao:
<hex>`` — and carry ``local_rf=False`` provenance, so the fusion engine collapses
a local+network pair into one track (PRD §11.4) instead of showing it twice.

Two layers, per PRD §18.2:

- :class:`AircraftProvider` (Protocol) + the provider-neutral
  :class:`AircraftObservation`. Every provider parses *its own* wire format into
  this one model; provider-specific fields never leak past it except through
  ``attributes``.
- :class:`AdsbFiProvider` — the default open provider. Its ``/v3`` endpoint is
  ADS-B-Exchange-v2-compatible (aircraft under ``ac``; ``now`` in **milliseconds**,
  unlike readsb's seconds) and caps a query at 250 NM, which is *why* the 500 NM
  AOI must be tiled (:mod:`aether.adapters.aoi`).

:func:`observation_to_track` (shared across providers) builds the schema-v2
``TrackRecord``; :func:`dedupe_observations` collapses the same airframe seen in
overlapping tiles to one observation (NETADSB-FR-005). The HTTP fetch is injected
so the parser stays pure and CI never makes a live call.
"""

import asyncio
import functools
import json
import logging
import math
import urllib.request
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from aether.adapters.aoi import GeoRegion
from aether.schema.geometry import Point
from aether.schema.provenance import Provenance
from aether.schema.records import TrackRecord

log = logging.getLogger(__name__)

#: Per-source identifier shared by every network ADS-B provider; the MQTT topic
#: suffix (``records/network_adsb``) and the fusion freshness-table key. The
#: *specific* provider (adsb.fi, adsb.lol, …) rides in ``Provenance.provider`` so
#: the freshness window stays provider-stable and the backend stays generic.
SOURCE = "network_adsb"

#: Transponder emergency codes (PRD §11.2); mirrors the local adapter.
EMERGENCY_SQUAWKS = frozenset({"7500", "7600", "7700"})

#: Unit conversions from ADS-B aviation units to schema-v2 SI. Kept local to this
#: provider rather than imported from the readsb adapter so the two edges stay
#: decoupled (PRD §18.2 "keep the response parser provider-specific").
_FT_TO_M = 0.3048
_KT_TO_MS = 1852.0 / 3600.0
_FPM_TO_MS = _FT_TO_M / 60.0

#: adsb.fi open-data API (PRD §38; re-verified at build time).
ADSBFI_BASE_URL = "https://opendata.adsb.fi/api"
#: The ``/v3/.../dist`` query is capped at 250 NM by the provider.
ADSBFI_MAX_RADIUS_NM = 250.0
#: Reject an over-large response before parsing (PRD §17.2 size limits).
MAX_RESPONSE_BYTES = 16 * 1024 * 1024

#: An async HTTP GET returning raw bytes; injected so tests use fixtures, never
#: the network (CI makes no live calls).
AircraftFetch = Callable[[str], Awaitable[bytes]]


@dataclass(frozen=True)
class AircraftObservation:
    """One airframe as seen by a network provider, normalized to SI and identity.

    Provider-neutral: an adsb.fi row and an OpenSky row both become this. The
    ICAO 24-bit address is the identity (``track_key`` -> ``aircraft:icao:<hex>``),
    shared with the local adapter so the same airframe fuses rather than doubling.
    """

    icao_hex: str
    observed_at: datetime
    received_at: datetime
    non_icao: bool = False
    label: str | None = None
    geometry: Point | None = None
    altitude_m: float | None = None
    speed_mps: float | None = None
    heading_deg: float | None = None
    vertical_rate_mps: float | None = None
    on_ground: bool = False
    bad_position: bool = False
    emergency: bool = False
    attributes: dict[str, Any] = field(default_factory=dict)

    @property
    def track_key(self) -> str:
        return f"aircraft:icao:{self.icao_hex}"


class AircraftProvider(Protocol):
    """A network ADS-B source that returns observations for a query region (PRD §18.2)."""

    #: Human-readable provider label, recorded in ``Provenance.provider``.
    name: str
    #: Largest radius (NM) one query may request; drives AOI tiling.
    max_radius_nm: float

    async def fetch_region(self, region: GeoRegion) -> list[AircraftObservation]: ...


def _finite(value: object) -> float | None:
    """Coerce a JSON number to a finite float, rejecting bools and NaN/Infinity.

    Non-finite values are invalid JSON to re-emit and would crash the record's
    serialization on publish to the bus (PRD §17.2 "validate source responses").
    """
    if isinstance(value, bool):  # bool is an int subclass; never a measurement
        return None
    if isinstance(value, (int, float)):
        result = float(value)
        return result if math.isfinite(result) else None
    return None


def _valid_lonlat(lon: float | None, lat: float | None) -> bool:
    if lon is None or lat is None:
        return False
    return -180.0 <= lon <= 180.0 and -90.0 <= lat <= 90.0


def _ms_to_dt(value: object) -> datetime | None:
    """Convert ADS-B-Exchange epoch **milliseconds** to an aware UTC datetime."""
    millis = _finite(value)
    if millis is None:
        return None
    try:
        return datetime.fromtimestamp(millis / 1000.0, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


#: adsb.fi/ADSBx-native fields with no first-class schema home, preserved verbatim
#: under ``attributes`` (PRD §18.1 spirit). ``dbFlags`` carries the provider's
#: military bit, consumed by the later classification slice (PRD §11.5).
_PRESERVED_FIELDS = (
    "squawk",
    "emergency",
    "category",
    "type",
    "rssi",
    "messages",
    "seen",
    "seen_pos",
    "nic",
    "r",
    "t",
    "dbFlags",
)


def _is_emergency(squawk: object, emergency: object) -> bool:
    if isinstance(squawk, str) and squawk in EMERGENCY_SQUAWKS:
        return True
    return isinstance(emergency, str) and emergency not in ("", "none")


def parse_aircraft(
    ac: dict[str, Any],
    *,
    snapshot_now: datetime,
    received_at: datetime,
) -> AircraftObservation | None:
    """Normalize one adsb.fi ``ac`` entry into an :class:`AircraftObservation`.

    Returns ``None`` when the row carries no usable ICAO identity. A present-but-
    impossible position drops the geometry only (the airframe is still tracked,
    flagged ``bad_position``); units are converted to SI and native fields kept
    under ``attributes`` (PRD §17.2).
    """
    raw_hex = ac.get("hex")
    if not isinstance(raw_hex, str) or not raw_hex.strip():
        return None
    non_icao = raw_hex.startswith("~")  # TIS-B / non-ICAO address
    hex_id = (raw_hex[1:] if non_icao else raw_hex).strip().lower()
    if not hex_id:
        return None

    # observed_at: position age preferred, else message age, relative to ``now``.
    age_s = _finite(ac.get("seen_pos"))
    if age_s is None:
        age_s = _finite(ac.get("seen")) or 0.0
    try:
        observed_at = snapshot_now - timedelta(seconds=age_s)
    except (OverflowError, ValueError):  # absurd age: keep the track, anchor at now
        observed_at = snapshot_now

    raw_baro = ac.get("alt_baro")
    on_ground = raw_baro == "ground"
    if on_ground:
        altitude_m: float | None = 0.0
    else:
        alt_ft = _finite(raw_baro)
        if alt_ft is None:
            alt_ft = _finite(ac.get("alt_geom"))
        altitude_m = alt_ft * _FT_TO_M if alt_ft is not None else None

    lon, lat = _finite(ac.get("lon")), _finite(ac.get("lat"))
    has_pos = _valid_lonlat(lon, lat)
    bad_pos = (lon is not None or lat is not None) and not has_pos
    geometry: Point | None = None
    if has_pos:
        coords = [float(lon), float(lat)]  # type: ignore[arg-type]
        if altitude_m is not None:
            coords.append(altitude_m)
        geometry = Point(coordinates=coords)

    gs_kt = _finite(ac.get("gs"))
    speed_mps = gs_kt * _KT_TO_MS if gs_kt is not None else None
    heading_deg = _finite(ac.get("track"))
    rate_fpm = _finite(ac.get("baro_rate"))
    if rate_fpm is None:
        rate_fpm = _finite(ac.get("geom_rate"))
    vertical_rate_mps = rate_fpm * _FPM_TO_MS if rate_fpm is not None else None

    flight = ac.get("flight")
    label = flight.strip() if isinstance(flight, str) and flight.strip() else hex_id.upper()
    attributes = {k: ac[k] for k in _PRESERVED_FIELDS if k in ac}
    if on_ground:
        attributes["on_ground"] = True

    return AircraftObservation(
        icao_hex=hex_id,
        observed_at=observed_at,
        received_at=received_at,
        non_icao=non_icao,
        label=label,
        geometry=geometry,
        altitude_m=altitude_m,
        speed_mps=speed_mps,
        heading_deg=heading_deg,
        vertical_rate_mps=vertical_rate_mps,
        on_ground=on_ground,
        bad_position=bad_pos,
        emergency=_is_emergency(ac.get("squawk"), ac.get("emergency")),
        attributes=attributes,
    )


def parse_response(payload: dict[str, Any], *, received_at: datetime) -> list[AircraftObservation]:
    """Normalize a full adsb.fi ``/v3`` response into observations.

    The top-level ``now`` (epoch **milliseconds**) anchors per-aircraft ages,
    falling back to ``received_at`` if absent or unparsable. The aircraft array is
    under ``ac``. One malformed row is logged and skipped so a single bad entry
    never drops the rest of the response (PRD §17.2, §37 failure isolation).
    """
    if not isinstance(payload, dict):
        return []
    snapshot_now = _ms_to_dt(payload.get("now")) or received_at
    aircraft = payload.get("ac")
    if not isinstance(aircraft, list):
        return []
    observations: list[AircraftObservation] = []
    for entry in aircraft:
        if not isinstance(entry, dict):
            continue
        try:
            obs = parse_aircraft(entry, snapshot_now=snapshot_now, received_at=received_at)
        except Exception:  # one bad aircraft must not drop the rest of the response
            log.warning("skipping malformed adsb.fi aircraft entry", exc_info=True)
            continue
        if obs is not None:
            observations.append(obs)
    return observations


def dedupe_observations(observations: Iterable[AircraftObservation]) -> list[AircraftObservation]:
    """Collapse the same airframe (by ICAO identity) to its freshest observation.

    Overlapping tiles return the same aircraft more than once; the operator must
    see it once (NETADSB-FR-005, PRD §16.4 "deduplicate by stable identity"). The
    freshest (latest ``observed_at``) wins; first-seen order is otherwise kept so
    the result is deterministic for a given input order.
    """
    best: dict[str, AircraftObservation] = {}
    for obs in observations:
        existing = best.get(obs.icao_hex)
        if existing is None or obs.observed_at > existing.observed_at:
            best[obs.icao_hex] = obs
    return list(best.values())


def observation_to_track(
    obs: AircraftObservation,
    *,
    source: str = SOURCE,
    provider: str = "adsb.fi",
) -> TrackRecord:
    """Build the schema-v2 network :class:`TrackRecord` from an observation.

    Network provenance: ``locally_received=False`` and a single ``local_rf=False``
    provenance entry naming the provider, so fusion ranks fresh local fields above
    it but still fills gaps from it (FUSION-FR-002/003) and the operator can always
    collapse to local-only.
    """
    tags: list[str] = []
    if obs.emergency:
        tags.append("emergency")
    if obs.on_ground:
        tags.append("on_ground")
    if obs.non_icao:
        tags.append("non_icao")
    if obs.bad_position:
        tags.append("bad_position")

    return TrackRecord(
        id=obs.track_key,
        source=source,
        observed_at=obs.observed_at,
        received_at=obs.received_at,
        published_at=obs.received_at,
        correlation_key=obs.track_key,
        track_type="aircraft",
        label=obs.label,
        geometry=obs.geometry,
        altitude_m=obs.altitude_m,
        speed_mps=obs.speed_mps,
        heading_deg=obs.heading_deg,
        vertical_rate_mps=obs.vertical_rate_mps,
        locally_received=False,
        tags=tags,
        attributes=dict(obs.attributes),
        provenance=[
            Provenance(
                source=source,
                provider=provider,
                observed_at=obs.observed_at,
                received_at=obs.received_at,
                local_rf=False,
            )
        ],
    )


def _blocking_get(url: str, timeout_s: float) -> bytes:
    """Single blocking HTTPS GET with a size cap; run off-loop via ``to_thread``."""
    if not url.startswith("https://"):  # provider base_url is https; never downgrade
        raise ValueError(f"refusing non-https provider URL: {url!r}")
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:  # noqa: S310 (https-checked)
        return bytes(resp.read(MAX_RESPONSE_BYTES + 1))


async def _default_fetch(url: str, *, timeout_s: float) -> bytes:
    return await asyncio.to_thread(_blocking_get, url, timeout_s)


class AdsbFiProvider:
    """The default open provider: adsb.fi ``/v3/lat/{lat}/lon/{lon}/dist/{dist}``.

    Concrete :class:`AircraftProvider`. The HTTP fetch is injectable (default: a
    size-capped urllib GET on a worker thread); tests pass a fake returning fixture
    bytes so no live call is made (PRD §34: no live APIs in the test path).
    """

    name = "adsb.fi"
    max_radius_nm = ADSBFI_MAX_RADIUS_NM

    def __init__(
        self,
        *,
        fetch: AircraftFetch | None = None,
        base_url: str = ADSBFI_BASE_URL,
        timeout_s: float = 10.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._fetch: AircraftFetch = (
            fetch if fetch is not None else functools.partial(_default_fetch, timeout_s=timeout_s)
        )

    def build_url(self, region: GeoRegion) -> str:
        """The ``/v3`` query URL for ``region`` (radius clamped to the provider cap)."""
        dist = min(region.radius_nm, self.max_radius_nm)
        return (
            f"{self._base_url}/v3"
            f"/lat/{region.center_lat:.6f}"
            f"/lon/{region.center_lon:.6f}"
            f"/dist/{dist:g}"
        )

    async def fetch_region(self, region: GeoRegion) -> list[AircraftObservation]:
        url = self.build_url(region)
        raw = await self._fetch(url)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ValueError(
                f"adsb.fi response {len(raw)} bytes exceeds limit {MAX_RESPONSE_BYTES}"
            )
        payload = json.loads(raw)
        received_at = datetime.now(UTC)
        return parse_response(payload, received_at=received_at)

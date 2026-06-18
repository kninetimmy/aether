"""Area-of-interest geometry and provider tiling (PRD §16.2, §16.4).

A network provider often caps a single radius query *below* the configured AOI
(adsb.fi's ``/v3/.../dist/{nm}`` tops out at 250 NM, the default AOI is 500 NM).
:func:`tile_region` turns one oversized AOI disk into a deterministic set of
overlapping compliant query disks whose union covers it, so the adapter can
fetch the whole AOI as several allowed requests and deduplicate the results
(NETADSB-FR-005). The grid is a pure function of the inputs — same AOI, same
tiles, every time — so tiling never depends on a hidden clock or call order.

This module is source-agnostic: it knows only circles on a sphere. The ADS-B
adapter consumes it, but so can any later viewport-driven feed (AIS, APRS-IS).
Distances are nautical miles; coordinates are WGS 84 decimal degrees.
"""

import math
from dataclasses import dataclass

#: One nautical mile is 1/60 of a degree of latitude by definition.
_NM_PER_DEG_LAT = 60.0

#: Clamp the latitude used for the longitude/degree scaling away from the poles
#: so ``cos(lat)`` never collapses to zero and explodes the east/west spacing.
_MAX_ABS_LAT_FOR_SCALING = 89.9


@dataclass(frozen=True)
class GeoRegion:
    """A circular query region: a center and a radius in nautical miles."""

    center_lat: float
    center_lon: float
    radius_nm: float

    def __post_init__(self) -> None:
        if not -90.0 <= self.center_lat <= 90.0:
            raise ValueError(f"center_lat {self.center_lat} out of range [-90, 90]")
        if not -180.0 <= self.center_lon <= 180.0:
            raise ValueError(f"center_lon {self.center_lon} out of range [-180, 180]")
        if not self.radius_nm > 0.0:
            raise ValueError(f"radius_nm must be positive, got {self.radius_nm}")


def _offset(
    center_lat: float, center_lon: float, *, north_nm: float, east_nm: float
) -> tuple[float, float]:
    """Return ``(lat, lon)`` offset from a center by N/E nautical-mile components.

    A local flat-earth approximation: good to a fraction of a percent over the
    few-hundred-NM tile spacings here, and the tile overlap absorbs the residual.
    Longitude degrees shrink with ``cos(lat)``; latitude is clamped off the poles
    so the scaling stays finite.
    """
    clamped = max(-_MAX_ABS_LAT_FOR_SCALING, min(_MAX_ABS_LAT_FOR_SCALING, center_lat))
    dlat = north_nm / _NM_PER_DEG_LAT
    dlon = east_nm / (_NM_PER_DEG_LAT * math.cos(math.radians(clamped)))
    return center_lat + dlat, center_lon + dlon


def tile_region(
    aoi: GeoRegion,
    *,
    max_radius_nm: float,
    overlap: float = 0.15,
) -> list[GeoRegion]:
    """Cover ``aoi`` with compliant overlapping query disks (PRD §16.4).

    When the AOI already fits one allowed query (``radius_nm <= max_radius_nm``)
    the AOI itself is returned unchanged — one request, no waste (PRD §16.4
    "prefer fewer requests"). Otherwise a square grid of ``max_radius_nm`` disks
    is laid down at a spacing chosen so the disks overlap with no gaps, and every
    tile whose disk can cover a point of the AOI is kept.

    ``overlap`` (0–1) trims the grid spacing below the bare covering pitch to add
    margin against the flat-earth approximation and the AOI rim; the default is a
    modest 15%. The returned list is deterministic (row-major, north-to-south)
    and never empty.
    """
    if not 0.0 <= overlap < 1.0:
        raise ValueError(f"overlap must be in [0, 1), got {overlap}")
    if not max_radius_nm > 0.0:
        raise ValueError(f"max_radius_nm must be positive, got {max_radius_nm}")

    r = max_radius_nm
    if aoi.radius_nm <= r:
        return [aoi]

    # Square grid of radius-r disks covers the plane when the cell half-diagonal
    # does not exceed r, i.e. spacing <= r*sqrt(2). Shrink by ``overlap`` for margin.
    spacing = r * math.sqrt(2.0) * (1.0 - overlap)
    # Any AOI point's nearest grid center is within half a cell diagonal; including
    # every tile whose center is within this reach of the AOI center guarantees the
    # union covers the whole AOI disk.
    reach = aoi.radius_nm + spacing * math.sqrt(2.0) / 2.0
    steps = math.ceil(reach / spacing)

    tiles: list[GeoRegion] = []
    for j in range(steps, -steps - 1, -1):  # north (top) to south, deterministic
        north_nm = j * spacing
        for i in range(-steps, steps + 1):  # west to east
            east_nm = i * spacing
            if math.hypot(north_nm, east_nm) > reach:
                continue
            lat, lon = _offset(aoi.center_lat, aoi.center_lon, north_nm=north_nm, east_nm=east_nm)
            lat = max(-90.0, min(90.0, lat))
            lon = ((lon + 180.0) % 360.0) - 180.0  # wrap into [-180, 180)
            tiles.append(GeoRegion(center_lat=lat, center_lon=lon, radius_nm=r))
    return tiles

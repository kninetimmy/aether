"""Unit tests for AOI provider tiling (PRD §16.4, NETADSB-FR-005)."""

import math

import pytest

from aether.adapters.aoi import GeoRegion, tile_region

#: Earth radius in nautical miles, for the spherical cross-check below.
_EARTH_NM = 3440.065


def _distance_nm(a_lat: float, a_lon: float, b_lat: float, b_lon: float) -> float:
    """Great-circle distance (haversine), NM — the *true* metric tiles must cover."""
    p1, p2 = math.radians(a_lat), math.radians(b_lat)
    dphi = math.radians(b_lat - a_lat)
    dlam = math.radians(b_lon - a_lon)
    h = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlam / 2) ** 2
    return 2 * _EARTH_NM * math.asin(math.sqrt(h))


def _destination(lat: float, lon: float, bearing_deg: float, dist_nm: float) -> tuple[float, float]:
    """Point ``dist_nm`` from (lat, lon) along ``bearing_deg`` (spherical)."""
    delta = dist_nm / _EARTH_NM
    theta = math.radians(bearing_deg)
    phi1, lam1 = math.radians(lat), math.radians(lon)
    phi2 = math.asin(
        math.sin(phi1) * math.cos(delta) + math.cos(phi1) * math.sin(delta) * math.cos(theta)
    )
    lam2 = lam1 + math.atan2(
        math.sin(theta) * math.sin(delta) * math.cos(phi1),
        math.cos(delta) - math.sin(phi1) * math.sin(phi2),
    )
    return math.degrees(phi2), math.degrees(lam2)


def test_single_tile_when_aoi_fits() -> None:
    aoi = GeoRegion(40.0, -95.0, 200.0)
    tiles = tile_region(aoi, max_radius_nm=250.0)
    assert tiles == [aoi]  # one query, no waste


def test_aoi_at_cap_is_one_tile() -> None:
    aoi = GeoRegion(40.0, -95.0, 250.0)
    assert tile_region(aoi, max_radius_nm=250.0) == [aoi]


def test_oversized_aoi_splits_into_capped_tiles() -> None:
    aoi = GeoRegion(40.0, -95.0, 500.0)
    tiles = tile_region(aoi, max_radius_nm=250.0)
    assert len(tiles) > 1
    assert all(t.radius_nm == 250.0 for t in tiles)
    assert all(t.radius_nm <= 250.0 for t in tiles)


def test_tiling_is_deterministic() -> None:
    aoi = GeoRegion(40.7, -95.2, 500.0)
    assert tile_region(aoi, max_radius_nm=250.0) == tile_region(aoi, max_radius_nm=250.0)


def test_tiles_cover_the_whole_aoi_disk() -> None:
    # Every point of the 500 NM AOI must lie within some 250 NM tile (no gaps).
    aoi = GeoRegion(40.0, -95.0, 500.0)
    tiles = tile_region(aoi, max_radius_nm=250.0)
    for dist in (0.0, 120.0, 250.0, 380.0, 460.0, 500.0):
        for bearing in range(0, 360, 30):
            plat, plon = _destination(aoi.center_lat, aoi.center_lon, float(bearing), dist)
            nearest = min(_distance_nm(plat, plon, t.center_lat, t.center_lon) for t in tiles)
            assert nearest <= 250.0 + 1e-6, (
                f"gap at dist={dist} bearing={bearing}: {nearest:.1f} NM"
            )


def test_geo_region_rejects_out_of_range() -> None:
    with pytest.raises(ValueError):
        GeoRegion(91.0, 0.0, 100.0)
    with pytest.raises(ValueError):
        GeoRegion(0.0, 181.0, 100.0)
    with pytest.raises(ValueError):
        GeoRegion(0.0, 0.0, 0.0)


def test_tile_region_rejects_bad_params() -> None:
    aoi = GeoRegion(40.0, -95.0, 500.0)
    with pytest.raises(ValueError):
        tile_region(aoi, max_radius_nm=0.0)
    with pytest.raises(ValueError):
        tile_region(aoi, max_radius_nm=250.0, overlap=1.0)

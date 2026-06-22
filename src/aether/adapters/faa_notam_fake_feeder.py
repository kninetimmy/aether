"""No-hardware feeder: a fake FAA NOTAM provider returning canned geoJson pages.

Stands in for the live FAA NOTAM API so the M6.4 path runs with no network and no
credentials (PRD §6 no-hardware gate, §34 "every source ships a fake/replay feeder"). It
is the NOTAM sibling of the other fake feeders: real, production-wired code selected by
config (``AETHER_FAA_NOTAM_BASE_URL=fake`` or either credential set to ``fake``), never a
live call.

The canned roster is placed *relative to the configured AOI center* (like the FAA TFR
feeder) so the demo renders wherever the operator points the station, paginated across two
pages, and it exercises every branch of the adapter:

- a single-area airspace NOTAM AT the center → an in-AOI ``Polygon`` GeoFeature;
- a two-area NOTAM straddling the center → an in-AOI ``MultiPolygon`` GeoFeature;
- a NOTAM with ``geometry: null`` → a textual facility-panel event (AIRSPACE-FR-005);
- a NOTAM whose supplied vertex is out of range → unparseable geometry → a textual event;
- a cancelled (``type == "C"``) NOTAM → dropped from the live map.

Effective windows are stamped at ``fetch`` time (``now_fn`` is injectable for deterministic
tests) so the demo's NOTAMs read as currently active rather than stale.
"""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

#: Two pages so the pagination loop is exercised end to end.
_TOTAL_PAGES = 2


def _ring(center_lat: float, center_lon: float, half_deg: float) -> list[list[float]]:
    """A small closed GeoJSON ring ([lon, lat] vertices, first repeated) around a center."""
    s, w = center_lat - half_deg, center_lon - half_deg
    n, e = center_lat + half_deg, center_lon + half_deg
    return [[w, s], [e, s], [e, n], [w, n], [w, s]]


class FakeFaaNotamProvider:
    """A controllable :class:`~aether.adapters.faa_notam.FaaNotamProvider` with canned NOTAMs.

    Exposes :pyattr:`effective_radius_nm` so the status surfaces a query radius just like
    the live provider, keeping the demo and production status shapes identical.
    """

    name = "fake"
    effective_radius_nm = 100.0

    def __init__(
        self,
        *,
        center_lat: float = 0.0,
        center_lon: float = 0.0,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self._lat = center_lat
        self._lon = center_lon
        self._now = now_fn or (lambda: datetime.now(UTC))

    def _notam(
        self,
        *,
        nid: str,
        number: str,
        ntype: str,
        text: str,
        feature_type: str,
    ) -> dict[str, Any]:
        now = self._now()
        iso = "%Y-%m-%dT%H:%M:%S.000Z"
        return {
            "id": nid,
            "number": number,
            "type": ntype,
            "classification": "DOM",
            "location": "ZZZ",
            "icaoLocation": "KZZZ",
            "accountId": "KZZZ",
            "featureType": feature_type,
            "issued": (now - timedelta(hours=2)).strftime(iso),
            "lastUpdated": (now - timedelta(hours=1)).strftime(iso),
            "effectiveStart": (now - timedelta(hours=1)).strftime(iso),
            "effectiveEnd": (now + timedelta(days=2)).strftime(iso),
            "text": text,
        }

    def _feature(self, notam: dict[str, Any], geometry: dict[str, Any] | None) -> dict[str, Any]:
        return {
            "type": "Feature",
            "properties": {"coreNOTAMData": {"notam": notam}},
            "geometry": geometry,
        }

    def _all_features(self) -> list[dict[str, Any]]:
        lat, lon = self._lat, self._lon
        poly = {"type": "Polygon", "coordinates": [_ring(lat, lon, 0.1)]}
        poly_b = {"type": "Polygon", "coordinates": [_ring(lat + 0.3, lon + 0.3, 0.05)]}
        bad = {"type": "Polygon", "coordinates": [[[lon, 200.0], [lon, lat], [lon + 0.1, lat]]]}
        return [
            # single Polygon at the center → in-AOI GeoFeature
            self._feature(
                self._notam(
                    nid="NOTAM_FAKE_1",
                    number="01/001",
                    ntype="N",
                    text="!ZZZ 01/001 ZZZ AIRSPACE UAS WI AN AREA DEFINED AS ...",
                    feature_type="AIRSPACE",
                ),
                {"type": "GeometryCollection", "geometries": [poly]},
            ),
            # two Polygons → MultiPolygon GeoFeature
            self._feature(
                self._notam(
                    nid="NOTAM_FAKE_2",
                    number="01/002",
                    ntype="N",
                    text="!ZZZ 01/002 ZZZ AIRSPACE TWO AREAS ...",
                    feature_type="AIRSPACE",
                ),
                {"type": "GeometryCollection", "geometries": [poly, poly_b]},
            ),
            # null geometry → textual facility-panel event
            self._feature(
                self._notam(
                    nid="NOTAM_FAKE_3",
                    number="01/003",
                    ntype="N",
                    text="!ZZZ 01/003 ZZZ RWY 12/30 CLSD",
                    feature_type="RWY",
                ),
                None,
            ),
            # malformed geometry (lat 200°) → unparseable → textual event
            self._feature(
                self._notam(
                    nid="NOTAM_FAKE_4",
                    number="01/004",
                    ntype="N",
                    text="!ZZZ 01/004 ZZZ OBST TOWER ...",
                    feature_type="OBST",
                ),
                {"type": "GeometryCollection", "geometries": [bad]},
            ),
            # cancelled NOTAM → dropped from the live map
            self._feature(
                self._notam(
                    nid="NOTAM_FAKE_5",
                    number="01/005",
                    ntype="C",
                    text="!ZZZ 01/005 ZZZ NAV VOR U/S CANCELLED",
                    feature_type="NAV",
                ),
                {"type": "GeometryCollection", "geometries": [poly]},
            ),
        ]

    async def fetch_page(self, page_num: int) -> dict[str, Any]:
        features = self._all_features()
        # Three on page 1, the rest on page 2 — a real two-page response shape.
        page_1, page_2 = features[:3], features[3:]
        items = page_1 if page_num <= 1 else page_2
        return {
            "pageSize": 3,
            "pageNum": page_num,
            "totalCount": len(features),
            "totalPages": _TOTAL_PAGES,
            "items": items if page_num <= _TOTAL_PAGES else [],
        }

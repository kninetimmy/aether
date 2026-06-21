"""No-hardware feeder: a fake FAA TFR provider returning canned list + detail XML.

Stands in for the live FAA TFR service so the M6.1 path runs with no network and no
key (PRD §6 no-hardware gate, §34 "every source ships a fake/replay feeder"). It is the
TFR sibling of the other fake feeders: real, production-wired code selected by config
(``AETHER_FAA_TFR_BASE_URL=fake``), never a live call.

The canned roster is placed *relative to the configured AOI center* (like the USGS fake
feeder) so the demo renders wherever the operator points the station, and it exercises
every branch of the adapter:

- a single-area security TFR AT the center → an in-AOI ``Polygon``;
- a two-area VIP TFR straddling the center → an in-AOI ``MultiPolygon``;
- a hazard TFR ~25° away → outside a 500 NM AOI, so the AOI filter drops it;
- a sport TFR with a bad boundary vertex → unparseable geometry → a textual event.

Detail bytes are stamped at ``fetch`` time (``now_fn`` is injectable for deterministic
tests) so the demo's TFRs read with fresh effective windows rather than stale ones.
"""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

#: List rows mirror the live ``exportTfrList`` shape (notam_id/type/facility/state/...).
_ROWS: list[dict[str, Any]] = [
    {"notam_id": "6/0001", "type": "SECURITY", "facility": "ZAB", "state": "XX",
     "description": "Fake security TFR at AOI center", "creation_date": "06/21/2026"},
    {"notam_id": "6/0002", "type": "VIP", "facility": "ZAB", "state": "XX",
     "description": "Fake VIP TFR (two areas) at AOI center", "creation_date": "06/21/2026"},
    {"notam_id": "6/0003", "type": "HAZARDS", "facility": "ZSE", "state": "YY",
     "description": "Fake hazard TFR far outside the AOI", "creation_date": "06/21/2026"},
    {"notam_id": "6/0004", "type": "SPORTS", "facility": "ZAB", "state": "XX",
     "description": "Fake sport TFR with unparseable geometry", "creation_date": "06/21/2026"},
]  # fmt: skip


def _coord(value: float, *, is_lat: bool) -> str:
    """Format a signed degree value the FAA way: ``|value|`` + hemisphere letter."""
    hemi = ("N" if value >= 0 else "S") if is_lat else ("E" if value >= 0 else "W")
    return f"{abs(value):.6f}{hemi}"


def _square(center_lat: float, center_lon: float, half_deg: float) -> list[tuple[float, float]]:
    """A small CCW square ring (lat, lon corners) around a center, for canned geometry."""
    return [
        (center_lat - half_deg, center_lon - half_deg),
        (center_lat - half_deg, center_lon + half_deg),
        (center_lat + half_deg, center_lon + half_deg),
        (center_lat + half_deg, center_lon - half_deg),
    ]


def _avx(corners: list[tuple[float, float]], *, malformed: bool = False) -> str:
    out = []
    for i, (lat, lon) in enumerate(corners):
        # A single bad latitude (>90°) makes the whole ring unparseable (§18.10 path).
        lat_s = "200.0N" if (malformed and i == 0) else _coord(lat, is_lat=True)
        lon_s = _coord(lon, is_lat=False)
        out.append(f"<Avx><geoLat>{lat_s}</geoLat><geoLong>{lon_s}</geoLong></Avx>")
    return "".join(out)


def _area_group(corners: list[tuple[float, float]], *, name: str, malformed: bool = False) -> str:
    return (
        "<TFRAreaGroup>"
        "<aseTFRArea>"
        f"<txtName>{name}</txtName>"
        "<codeDistVerUpper>ALT</codeDistVerUpper><valDistVerUpper>2500</valDistVerUpper>"
        "<uomDistVerUpper>FT</uomDistVerUpper>"
        "<codeDistVerLower>HEI</codeDistVerLower><valDistVerLower>0</valDistVerLower>"
        "<uomDistVerLower>FT</uomDistVerLower>"
        "</aseTFRArea>"
        f"<abdMergedArea>{_avx(corners, malformed=malformed)}</abdMergedArea>"
        "</TFRAreaGroup>"
    )


class FakeFaaTfrProvider:
    """A controllable :class:`~aether.adapters.faa_tfr.FaaTfrProvider` with canned TFRs."""

    name = "fake"

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

    async def fetch_list(self) -> list[dict[str, Any]]:
        return [dict(row) for row in _ROWS]

    def _detail(
        self,
        notam_id: str,
        *,
        name: str,
        cfr: str,
        area_groups: str,
        city: str,
        state: str,
    ) -> bytes:
        now = self._now()
        eff = (now + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S")
        exp = (now + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%S")
        issued = now.strftime("%Y-%m-%dT%H:%M:%S")
        xml = (
            '<XNOTAM-Update version="0.1">'
            "<Group><Add><Not>"
            "<NotUid>"
            f"<txtLocalName>{name}</txtLocalName>"
            f"<dateIssued>{issued}</dateIssued>"
            "</NotUid>"
            f"<dateEffective>{eff}</dateEffective>"
            f"<dateExpire>{exp}</dateExpire>"
            "<codeTimeZone>UTC</codeTimeZone>"
            f"<txtDescrPurpose>Fake {name}.</txtDescrPurpose>"
            f"<AffLocGroup><txtNameCity>{city}</txtNameCity>"
            f"<txtNameUSState>{state}</txtNameUSState></AffLocGroup>"
            "<codeFacility>ZAB</codeFacility>"
            f"<TfrNot><codeType>{cfr}</codeType>{area_groups}</TfrNot>"
            "</Not></Add></Group>"
            "</XNOTAM-Update>"
        )
        return xml.encode("utf-8")

    async def fetch_detail(self, notam_id: str) -> bytes:
        lat, lon = self._lat, self._lon
        if notam_id == "6/0001":  # single-area Polygon at the center → in-AOI
            return self._detail(
                notam_id,
                name="Center Security TFR",
                cfr="99.7",
                area_groups=_area_group(_square(lat, lon, 0.1), name="Area A"),
                city="Centerville",
                state="XX",
            )
        if notam_id == "6/0002":  # two areas → MultiPolygon, in-AOI
            return self._detail(
                notam_id,
                name="Center VIP TFR",
                cfr="91.141",
                area_groups=(
                    _area_group(_square(lat, lon, 0.1), name="Area A")
                    + _area_group(_square(lat + 0.3, lon + 0.3, 0.05), name="Area B")
                ),
                city="Centerville",
                state="XX",
            )
        if notam_id == "6/0003":  # ~25° away → outside a 500 NM AOI
            return self._detail(
                notam_id,
                name="Distant Hazard TFR",
                cfr="91.137",
                area_groups=_area_group(_square(lat + 25.0, lon + 25.0, 0.2), name="Area A"),
                city="Faraway",
                state="YY",
            )
        if notam_id == "6/0004":  # bad vertex → unparseable geometry → textual event
            return self._detail(
                notam_id,
                name="Broken Sport TFR",
                cfr="91.145",
                area_groups=_area_group(_square(lat, lon, 0.1), name="Area A", malformed=True),
                city="Centerville",
                state="XX",
            )
        raise FileNotFoundError(f"no canned TFR detail for {notam_id!r}")

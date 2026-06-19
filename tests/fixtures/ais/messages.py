"""Canned AISStream envelopes + builders for the AIS adapter unit tests.

Shapes mirror real AISStream.io WebSocket frames — ``MessageType`` / ``MetaData`` /
``Message[<MessageType>]`` — so the parser and the dynamic/static merger are
exercised against the wire format with no live feed or API key (PRD §34 #5 parser
fixtures). No secrets and no real vessel data: MMSIs and names are obvious
placeholders.
"""

from typing import Any

#: A representative broadcast timestamp in AISStream's ``time_utc`` format. Tests that
#: care about dedup pass an explicit value; tests that care about freshness omit it so
#: ``observed_at`` falls back to receipt time.
TIME_UTC = "2026-06-18 12:00:00.000000 +0000 UTC"


def position_report(
    mmsi: int,
    lat: float,
    lon: float,
    *,
    sog: float | None = 10.0,
    cog: float | None = 90.0,
    heading: int | None = 90,
    nav: int | None = 0,
    ship_name: str = "",
    message_type: str = "PositionReport",
    time_utc: str | None = TIME_UTC,
) -> dict[str, Any]:
    """Build a position-class AISStream envelope (default Class A ``PositionReport``).

    Pass ``sog``/``cog``/``heading``/``nav`` as ``None`` to omit the field, or as an
    ITU "not available" sentinel (``Sog>=102.3``, ``Cog==360``, ``TrueHeading==511``)
    to exercise sentinel handling.
    """
    body: dict[str, Any] = {"UserID": mmsi, "Latitude": lat, "Longitude": lon}
    if sog is not None:
        body["Sog"] = sog
    if cog is not None:
        body["Cog"] = cog
    if heading is not None:
        body["TrueHeading"] = heading
    if nav is not None:
        body["NavigationalStatus"] = nav
    meta: dict[str, Any] = {"MMSI": mmsi, "ShipName": ship_name, "latitude": lat, "longitude": lon}
    if time_utc is not None:
        meta["time_utc"] = time_utc
    return {"MessageType": message_type, "MetaData": meta, "Message": {message_type: body}}


def ship_static(
    mmsi: int,
    *,
    name: str = "DEMO SHIP",
    callsign: str = "DEMO",
    imo: int = 1000001,
    ship_type: int = 70,
    destination: str = "PORT DEMO",
    dimension: dict[str, int] | None = None,
    time_utc: str | None = TIME_UTC,
) -> dict[str, Any]:
    """Build a ``ShipStaticData`` AISStream envelope (static + voyage data)."""
    body: dict[str, Any] = {
        "UserID": mmsi,
        "Name": name,
        "CallSign": callsign,
        "ImoNumber": imo,
        "Type": ship_type,
        "Destination": destination,
        "Dimension": dimension if dimension is not None else {"A": 100, "B": 20, "C": 10, "D": 10},
    }
    meta: dict[str, Any] = {"MMSI": mmsi, "ShipName": name}
    if time_utc is not None:
        meta["time_utc"] = time_utc
    return {"MessageType": "ShipStaticData", "MetaData": meta, "Message": {"ShipStaticData": body}}

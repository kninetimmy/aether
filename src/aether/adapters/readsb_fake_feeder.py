"""No-hardware feeder: write an evolving ``aircraft.json`` for the local adapter.

Stands in for a real `readsb` receiver so the M2.1 local ADS-B path runs with no
SDR (PRD §6 no-hardware gate, §34 "every source ships a fake/replay feeder").
:func:`fake_snapshot` is the pure generator (reused by tests); the ``__main__``
writes it to a file atomically on an interval so :class:`ReadsbSource`'s file
poller can read it.

Point the adapter at the file it writes::

    python -m aether.adapters.readsb_fake_feeder /tmp/aircraft.json &
    AETHER_DEMO_SOURCE=0 AETHER_LOCAL_ADSB=1 \
        AETHER_LOCAL_ADSB_SOURCE=/tmp/aircraft.json \
        uvicorn aether.backend.main:app --app-dir src

This writes a data file only; it never transmits or touches a radio.
"""

import json
import math
import os
import sys
import time
from typing import Any

#: Two notional aircraft orbiting points near a home station, plus one squawking
#: an emergency code so the throttle's immediate-publish path is exercised.
_ORBIT_CENTERS = [(-95.2, 40.7), (-94.8, 40.9)]
_EMERGENCY_CENTER = (-95.0, 40.8)


def fake_snapshot(tick: int, *, now_epoch: float) -> dict[str, Any]:
    """Build one ``aircraft.json`` snapshot for ``tick`` (readsb field shape)."""
    aircraft: list[dict[str, Any]] = []
    for n, (lon0, lat0) in enumerate(_ORBIT_CENTERS):
        heading = (tick * 10 + n * 180) % 360
        angle = math.radians(heading)
        aircraft.append(
            {
                "hex": f"a0000{n}",
                "flight": f"FAKE{n}  ",
                "lat": lat0 + 0.1 * math.sin(angle),
                "lon": lon0 + 0.1 * math.cos(angle),
                "alt_baro": 10000 + n * 2000,
                "gs": 300.0 + n * 20,
                "track": float(heading),
                "baro_rate": 0,
                "squawk": "1200",
                "emergency": "none",
                "category": "A3",
                "rssi": -14.2,
                "messages": 1000 + tick,
                "seen": 0.1,
                "seen_pos": 0.2,
            }
        )
    # An aircraft that flips to a hijack squawk after a few ticks.
    elon, elat = _EMERGENCY_CENTER
    emergency = tick >= 3
    aircraft.append(
        {
            "hex": "e00001",
            "flight": "FAKE911 ",
            "lat": elat,
            "lon": elon + 0.02 * tick,
            "alt_baro": 8000,
            "gs": 250.0,
            "track": 90.0,
            "squawk": "7500" if emergency else "4444",
            "emergency": "hijack" if emergency else "none",
            "category": "A2",
            "seen": 0.1,
            "seen_pos": 0.2,
        }
    )
    return {"now": now_epoch, "messages": 100000 + tick, "aircraft": aircraft}


def _write_atomic(path: str, data: dict[str, Any]) -> None:
    """Write JSON via a temp file + rename so readers never see a partial file."""
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    os.replace(tmp, path)


def _main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: python -m aether.adapters.readsb_fake_feeder <aircraft.json> [interval_s]")
        return 2
    path = argv[1]
    interval_s = float(argv[2]) if len(argv) > 2 else 1.0
    print(f"fake readsb feeder -> {path} every {interval_s}s (Ctrl-C to stop)")
    tick = 0
    try:
        while True:
            _write_atomic(path, fake_snapshot(tick, now_epoch=time.time()))
            tick += 1
            time.sleep(interval_s)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))

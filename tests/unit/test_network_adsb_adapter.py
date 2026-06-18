"""Unit tests for the network ADS-B adapter runner (PRD §18.2, §16.4, §17.1).

The pure provider/parser is tested in ``test_adsb_provider.py``; this covers the
runtime *around* it — tiled sweep, cross-tile dedupe, per-tile and whole-sweep
failure isolation, provider selection, and the no-hardware fake feeder. No broker,
no live call: a stub provider stands in for the network.
"""

import asyncio
import dataclasses
from collections.abc import Iterable
from datetime import UTC, datetime

import pytest

from aether.adapters.adsb_provider import AdsbFiProvider, AircraftObservation
from aether.adapters.aoi import GeoRegion
from aether.adapters.network_adsb import (
    SOURCE,
    aoi_from_settings,
    build_provider,
    network_adsb_records,
)
from aether.adapters.network_adsb_fake_feeder import FakeAircraftProvider
from aether.config import Settings
from aether.schema.records import SourceStatusRecord, TrackRecord

NOW = datetime(2026, 6, 17, 12, 0, 0, tzinfo=UTC)


def _obs(icao: str) -> AircraftObservation:
    return AircraftObservation(icao_hex=icao, observed_at=NOW, received_at=NOW)


class _StubProvider:
    """A controllable :class:`AircraftProvider`: same roster per tile, optional faults.

    ``max_radius_nm`` is small so a several-hundred-NM AOI tiles into many disks,
    exercising the cross-tile dedupe. ``fail_indices`` fails those tile calls (by
    call order); ``always_fail`` fails every call. ``calls`` records how many tiles
    were actually fetched.
    """

    name = "stub"
    max_radius_nm = 100.0

    def __init__(
        self,
        roster: Iterable[str] = ("a1b2c3", "cafe01"),
        *,
        fail_indices: frozenset[int] = frozenset(),
        always_fail: bool = False,
    ) -> None:
        self._roster = list(roster)
        self._fail_indices = fail_indices
        self._always_fail = always_fail
        self.calls = 0

    async def fetch_region(self, region: GeoRegion) -> list[AircraftObservation]:
        index = self.calls
        self.calls += 1
        if self._always_fail or index in self._fail_indices:
            raise RuntimeError(f"tile {index} boom")
        return [_obs(icao) for icao in self._roster]


async def _drive_one_sweep(provider: _StubProvider, aoi: GeoRegion) -> list:
    """Collect records through the first completed sweep (starting + sweep status).

    Stops after the second status record (``starting`` then the sweep result) and
    closes the generator, so the trailing poll/backoff sleep never runs in tests.
    """
    agen = network_adsb_records(provider, aoi, poll_s=0.0, rate_limit_s=0.0)
    records: list = []
    statuses = 0
    async for record in agen:
        records.append(record)
        if isinstance(record, SourceStatusRecord):
            statuses += 1
            if statuses >= 2:
                break
    await agen.aclose()
    return records


def test_first_record_is_starting_status() -> None:
    records = asyncio.run(_drive_one_sweep(_StubProvider(), GeoRegion(40.0, -95.0, 50.0)))
    assert isinstance(records[0], SourceStatusRecord)
    assert records[0].status == "starting"
    assert records[0].source == SOURCE


def test_sweep_dedupes_airframes_across_overlapping_tiles() -> None:
    # A 300 NM AOI against a 100 NM provider tiles into many overlapping disks, each
    # returning the same two airframes; the operator must see each one once.
    provider = _StubProvider(roster=("a1b2c3", "cafe01"))
    aoi = GeoRegion(40.0, -95.0, 300.0)
    records = asyncio.run(_drive_one_sweep(provider, aoi))

    assert provider.calls > 1  # the AOI really did tile into multiple requests
    tracks = [r for r in records if isinstance(r, TrackRecord)]
    assert {t.id for t in tracks} == {"aircraft:icao:a1b2c3", "aircraft:icao:cafe01"}
    assert len(tracks) == 2  # deduped, not one-per-tile

    status = records[-1]
    assert isinstance(status, SourceStatusRecord)
    assert status.status == "connected"
    assert status.attributes["aircraft_visible"] == 2
    assert status.attributes["tiles_ok"] == provider.calls


def test_network_tracks_carry_non_local_provenance() -> None:
    records = asyncio.run(
        _drive_one_sweep(_StubProvider(roster=("a1b2c3",)), GeoRegion(40.0, -95.0, 50.0))
    )
    track = next(r for r in records if isinstance(r, TrackRecord))
    assert track.source == SOURCE
    assert track.locally_received is False
    assert track.correlation_key == "aircraft:icao:a1b2c3"
    assert len(track.provenance) == 1
    assert track.provenance[0].local_rf is False
    assert track.provenance[0].provider == "stub"  # the provider name rides through


def test_one_failing_tile_is_isolated_and_reported_degraded() -> None:
    # Tile 0 errors; the remaining tiles still yield the airframes, and the sweep
    # is honestly reported as degraded (partial coverage), never fatal.
    provider = _StubProvider(roster=("a1b2c3",), fail_indices=frozenset({0}))
    records = asyncio.run(_drive_one_sweep(provider, GeoRegion(40.0, -95.0, 300.0)))

    tracks = [r for r in records if isinstance(r, TrackRecord)]
    assert {t.id for t in tracks} == {"aircraft:icao:a1b2c3"}
    status = records[-1]
    assert isinstance(status, SourceStatusRecord)
    assert status.status == "degraded"
    assert status.error_code == "PartialSweep"
    assert status.attributes["tiles_ok"] == provider.calls - 1


def test_whole_sweep_failure_yields_degraded_with_no_tracks() -> None:
    provider = _StubProvider(always_fail=True)
    records = asyncio.run(_drive_one_sweep(provider, GeoRegion(40.0, -95.0, 50.0)))

    assert not [r for r in records if isinstance(r, TrackRecord)]
    status = records[-1]
    assert isinstance(status, SourceStatusRecord)
    assert status.status == "degraded"
    assert status.error_code == "RuntimeError"  # the underlying tile error type
    assert status.attributes["tiles_ok"] == 0


def test_build_provider_selects_fake_adsbfi_and_rejects_unknown() -> None:
    base = Settings()
    assert isinstance(
        build_provider(dataclasses.replace(base, network_adsb_provider="fake")),
        FakeAircraftProvider,
    )
    assert isinstance(
        build_provider(dataclasses.replace(base, network_adsb_provider="adsb.fi")),
        AdsbFiProvider,
    )
    # Case/alias tolerance, then a hard error on an unknown name (no silent fallback).
    assert isinstance(
        build_provider(dataclasses.replace(base, network_adsb_provider="ADSBFI")),
        AdsbFiProvider,
    )
    with pytest.raises(ValueError, match="unknown network ADS-B provider"):
        build_provider(dataclasses.replace(base, network_adsb_provider="bogus"))


def test_aoi_from_settings_uses_center_and_radius() -> None:
    cfg = dataclasses.replace(
        Settings(),
        network_adsb_center_lat=40.5,
        network_adsb_center_lon=-95.25,
        network_adsb_radius_nm=250.0,
    )
    aoi = aoi_from_settings(cfg)
    assert (aoi.center_lat, aoi.center_lon, aoi.radius_nm) == (40.5, -95.25, 250.0)


def test_fake_provider_roster_is_fresh_and_overlaps_local_identity() -> None:
    provider = FakeAircraftProvider(now_fn=lambda: NOW)
    obs = asyncio.run(provider.fetch_region(GeoRegion(0.0, 0.0, 250.0)))
    hexes = {o.icao_hex for o in obs}
    # a1b2c3 overlaps the readsb fixture; a00000 the readsb fake feeder.
    assert {"a1b2c3", "a00000", "cafe01"} <= hexes
    assert all(o.observed_at == NOW for o in obs)  # stamped fresh for fusion

"""Unit tests for the persist-time sampling gate (PRD §19.5)."""

from datetime import UTC, datetime, timedelta

from aether.persist.sampling import _GATE_MAX_ENTRIES, SampleGate

T0 = datetime(2026, 6, 19, 12, 0, 0, tzinfo=UTC)


def _at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def _gate(**cadence: float) -> SampleGate:
    return SampleGate(cadence)


# -- cadence gating ----------------------------------------------------------


def test_first_observation_is_always_admitted() -> None:
    gate = _gate(local_adsb=5.0)
    assert gate.admit(identity="a", source="local_adsb", now=T0, high_fidelity=False)


def test_sub_cadence_repeat_is_dropped() -> None:
    gate = _gate(local_adsb=5.0)
    gate.admit(identity="a", source="local_adsb", now=T0, high_fidelity=False)
    # 4 s < 5 s cadence → dropped.
    assert not gate.admit(identity="a", source="local_adsb", now=_at(4), high_fidelity=False)


def test_observation_after_cadence_is_admitted() -> None:
    gate = _gate(local_adsb=5.0)
    gate.admit(identity="a", source="local_adsb", now=T0, high_fidelity=False)
    assert gate.admit(identity="a", source="local_adsb", now=_at(5), high_fidelity=False)


def test_cadence_is_measured_from_last_admitted_not_last_seen() -> None:
    gate = _gate(local_adsb=5.0)
    assert gate.admit(identity="a", source="local_adsb", now=T0, high_fidelity=False)
    # Dropped at 4 s must NOT reset the clock...
    assert not gate.admit(identity="a", source="local_adsb", now=_at(4), high_fidelity=False)
    # ...so 5 s after the *admit* still admits, and 8 s (4 s after that) does not.
    assert gate.admit(identity="a", source="local_adsb", now=_at(5), high_fidelity=False)
    assert not gate.admit(identity="a", source="local_adsb", now=_at(8), high_fidelity=False)


# -- per (source, identity) independence -------------------------------------


def test_same_identity_different_sources_have_independent_budgets() -> None:
    """One aircraft heard by local (5 s) and network (15 s) keeps both streams."""
    gate = _gate(local_adsb=5.0, network_adsb=15.0)
    abc = "aircraft:icao:abc"
    assert gate.admit(identity=abc, source="local_adsb", now=T0, high_fidelity=False)
    # Different source → not suppressed by the local admit at the same instant.
    assert gate.admit(identity=abc, source="network_adsb", now=T0, high_fidelity=False)
    # At 5 s local is due again; network (15 s) is not.
    assert gate.admit(identity=abc, source="local_adsb", now=_at(5), high_fidelity=False)
    assert not gate.admit(identity=abc, source="network_adsb", now=_at(5), high_fidelity=False)


def test_distinct_identities_do_not_share_a_budget() -> None:
    gate = _gate(local_adsb=5.0)
    assert gate.admit(identity="a", source="local_adsb", now=T0, high_fidelity=False)
    assert gate.admit(identity="b", source="local_adsb", now=T0, high_fidelity=False)


# -- cadence 0 (APRS "every unique packet") + defaults -----------------------


def test_zero_cadence_admits_every_record() -> None:
    gate = _gate(local_aprs=0.0)
    for s in (0, 0, 0, 1):
        assert gate.admit(identity="N0CALL", source="local_aprs", now=_at(s), high_fidelity=False)


def test_unknown_source_uses_default_cadence() -> None:
    gate = SampleGate({"local_adsb": 5.0}, default_s=0.0)
    # demo isn't tuned → default 0 → always admitted (keeps no-hw demo full-fidelity).
    for s in (0, 0, 0):
        assert gate.admit(identity="demo01", source="demo", now=_at(s), high_fidelity=False)


def test_nonzero_default_gates_untuned_sources() -> None:
    gate = SampleGate({}, default_s=10.0)
    assert gate.admit(identity="x", source="sonde", now=T0, high_fidelity=False)
    assert not gate.admit(identity="x", source="sonde", now=_at(9), high_fidelity=False)
    assert gate.admit(identity="x", source="sonde", now=_at(10), high_fidelity=False)


# -- high-fidelity (emergency) bypass ----------------------------------------


def test_high_fidelity_bypasses_cadence() -> None:
    gate = _gate(local_adsb=5.0)
    gate.admit(identity="a", source="local_adsb", now=T0, high_fidelity=False)
    # Emergency point 1 s later still persists despite the 5 s cadence (PRD §19.5).
    assert gate.admit(identity="a", source="local_adsb", now=_at(1), high_fidelity=True)


def test_high_fidelity_admits_even_on_zero_cadence_source() -> None:
    gate = _gate(local_aprs=0.0)
    assert gate.admit(identity="a", source="local_aprs", now=T0, high_fidelity=True)


# -- bounded memory (PRD §17.3, §37 soak safety) -----------------------------


def test_zero_cadence_keys_are_never_stored() -> None:
    gate = _gate(local_aprs=0.0)
    for i in range(1000):
        gate.admit(identity=f"call{i}", source="local_aprs", now=T0, high_fidelity=False)
    assert len(gate._last) == 0


def test_idle_entries_are_time_evicted() -> None:
    gate = SampleGate({"local_adsb": 5.0}, ttl_s=100.0)
    gate.admit(identity="old", source="local_adsb", now=T0, high_fidelity=False)
    # A fresh admit well past the TTL forgets the idle "old" key.
    gate.admit(identity="new", source="local_adsb", now=_at(200), high_fidelity=False)
    assert ("local_adsb", "old") not in gate._last
    assert ("local_adsb", "new") in gate._last


def test_table_is_size_capped_under_a_flood() -> None:
    gate = SampleGate({"local_adsb": 5.0}, ttl_s=1_000_000.0)
    # All within the TTL window, so only the size backstop bounds the table.
    for i in range(_GATE_MAX_ENTRIES + 500):
        gate.admit(identity=f"id{i}", source="local_adsb", now=_at(i), high_fidelity=False)
    assert len(gate._last) <= _GATE_MAX_ENTRIES


def test_ttl_never_below_longest_cadence() -> None:
    # A cadence longer than the requested TTL floor wins, so eviction can't race it.
    gate = SampleGate({"slow": 500.0}, ttl_s=10.0)
    assert gate._ttl_s == 500.0

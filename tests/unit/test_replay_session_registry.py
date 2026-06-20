"""Unit tests for the bounded replay session registry (M4.8, PRD §19.6/§37)."""

import pytest

from aether.replay.session import ReplaySession, SessionRegistry


def _session(sid: str) -> ReplaySession:
    return ReplaySession(
        session_id=sid,
        start="2026-06-19T12:00:00+00:00",
        end="2026-06-19T13:00:00+00:00",
        sources=None,
        count=0,
        truncated=False,
        created_at="2026-06-19T12:30:00+00:00",
    )


def test_add_get_delete_roundtrip() -> None:
    reg = SessionRegistry()
    reg.add(_session("a"))
    assert reg.get("a") is not None
    assert reg.get("missing") is None
    assert reg.delete("a") is True
    assert reg.get("a") is None
    assert reg.delete("a") is False  # already gone


def test_evicts_oldest_beyond_cap() -> None:
    reg = SessionRegistry(max_sessions=2)
    reg.add(_session("a"))
    reg.add(_session("b"))
    reg.add(_session("c"))  # over cap → oldest ("a") evicted
    assert reg.get("a") is None
    assert reg.get("b") is not None
    assert reg.get("c") is not None
    assert len(reg) == 2


def test_re_adding_same_id_refreshes_recency() -> None:
    reg = SessionRegistry(max_sessions=2)
    reg.add(_session("a"))
    reg.add(_session("b"))
    reg.add(_session("a"))  # touch "a" → it is now newest
    reg.add(_session("c"))  # over cap → oldest ("b") evicted, not "a"
    assert reg.get("a") is not None
    assert reg.get("b") is None
    assert reg.get("c") is not None


def test_rejects_zero_cap() -> None:
    with pytest.raises(ValueError):
        SessionRegistry(max_sessions=0)

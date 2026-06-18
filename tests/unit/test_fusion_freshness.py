"""Unit tests for the pure freshness windows + classification (PRD §15.4)."""

from datetime import UTC, datetime, timedelta

from aether.fusion.freshness import (
    DEFAULT_FRESHNESS,
    DEFAULT_FRESHNESS_FALLBACK,
    FreshnessWindow,
    age_seconds,
    classify,
    window_for,
)

T0 = datetime(2026, 6, 15, 12, 0, 0, tzinfo=UTC)

LOCAL = DEFAULT_FRESHNESS["local_adsb"]  # 5 / 30 / 60
NET = DEFAULT_FRESHNESS["demo-net"]  # 15 / 60 / 120


def test_age_seconds_clamps_negative_to_zero() -> None:
    # observed_at slightly in the future (benign clock skew) → age 0, never negative.
    assert age_seconds(T0 + timedelta(seconds=5), T0) == 0.0
    assert age_seconds(T0 - timedelta(seconds=5), T0) == 5.0


def test_age_seconds_far_future_reads_as_huge_age() -> None:
    # A timestamp well beyond the skew tolerance is suspicious: it must NOT clamp
    # to live forever (which would pin the group/track in memory). It reports a
    # large positive age so it classifies as expired and can be reaped (PRD §37).
    far = age_seconds(T0 + timedelta(hours=1), T0)
    assert far == 3600.0
    assert classify(far, NET) == "expired"
    assert classify(far, LOCAL) == "expired"


def test_classify_local_boundaries() -> None:
    assert classify(0.0, LOCAL) == "live"
    assert classify(4.999, LOCAL) == "live"
    assert classify(5.0, LOCAL) == "stale"  # age == live_s is stale
    assert classify(59.999, LOCAL) == "stale"
    assert classify(60.0, LOCAL) == "expired"  # age == expire_s is expired
    assert classify(1000.0, LOCAL) == "expired"


def test_classify_network_boundaries() -> None:
    assert classify(0.0, NET) == "live"
    assert classify(15.0, NET) == "stale"
    assert classify(60.0, NET) == "stale"
    assert classify(120.0, NET) == "expired"


def test_window_for_known_source_vs_fallback() -> None:
    assert window_for("local_adsb", DEFAULT_FRESHNESS, DEFAULT_FRESHNESS_FALLBACK) == LOCAL
    assert window_for("demo-net", DEFAULT_FRESHNESS, DEFAULT_FRESHNESS_FALLBACK) == NET
    # An unknown source falls back to the conservative network-grade window.
    unknown = window_for("some-new-feed", DEFAULT_FRESHNESS, DEFAULT_FRESHNESS_FALLBACK)
    assert unknown == DEFAULT_FRESHNESS_FALLBACK == FreshnessWindow(15.0, 60.0, 120.0)


def test_aprs_sources_share_the_minutes_grade_window() -> None:
    # Both APRS legs (local RF + APRS-IS) get the APRS-mobile window, NOT the 120s
    # network fallback — a station beaconing every few minutes must stay live
    # between beacons, and the two legs must coexist long enough to fuse (PRD §15.4).
    aprs = FreshnessWindow(300.0, 1800.0, 7200.0)
    assert window_for("local_aprs", DEFAULT_FRESHNESS, DEFAULT_FRESHNESS_FALLBACK) == aprs
    assert window_for("aprs_is", DEFAULT_FRESHNESS, DEFAULT_FRESHNESS_FALLBACK) == aprs
    # A 4-minute-old beacon is still live; the network fallback would call it expired.
    assert classify(240.0, aprs) == "live"
    assert classify(240.0, DEFAULT_FRESHNESS_FALLBACK) == "expired"
    # Boundaries: 5 min → stale, 2 h → expired.
    assert classify(300.0, aprs) == "stale"
    assert classify(7200.0, aprs) == "expired"

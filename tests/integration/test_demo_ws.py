"""End-to-end over the real bus: demo publisher -> MQTT -> backend -> REST + ws.

PRD §31.3 flow #1 (fake source -> adapter -> MQTT -> backend -> WebSocket). Skips
when no broker is reachable (see conftest); CI starts Mosquitto so it runs there.
"""

import dataclasses
import time

from fastapi.testclient import TestClient

from aether.backend.main import SUBSCRIBE_MIN_INTERVAL_S, create_app
from aether.config import Settings

# A bbox over the demo aircraft cluster: includes demo02 (-94.8,40.9) and demo04
# (-94.6,41.1); excludes demo01 (-95.2,40.7) and demo03 (-95.4,40.5).
_DEMO_BBOX = [-95.0, 40.6, -94.5, 41.2]

# Wire-frame types the client may receive (PRD §22.3-22.4).
_DELTA_TYPES = {
    "track_upsert",
    "feature_upsert",
    "event",
    "alert_upsert",
    "source_status",
    "remove",
}


def _app(settings: Settings) -> TestClient:
    # Fast demo ticks; the bus path is otherwise identical to production.
    fast = dataclasses.replace(settings, demo_source=True)
    return TestClient(create_app(settings=fast, demo_interval_s=0.05))


def test_health_and_state(broker_settings: Settings) -> None:
    with _app(broker_settings) as client:
        health = client.get("/api/health").json()
        assert health["status"] == "ok"

        state = client.get("/api/state").json()
        assert state["type"] == "snapshot"
        for bucket in ("tracks", "features", "events", "alerts", "source_status"):
            assert bucket in state


def test_ws_streams_snapshot_then_sequential_deltas(broker_settings: Settings) -> None:
    with _app(broker_settings) as client, client.websocket_connect("/ws/v2") as ws:
        snapshot = ws.receive_json()
        assert snapshot["type"] == "snapshot"

        seen_types: set[str] = set()
        last_seq = snapshot["seq"]
        for _ in range(20):
            delta = ws.receive_json()
            assert delta["type"] in _DELTA_TYPES
            assert delta["seq"] > last_seq  # monotonic, no gaps on a fresh client
            last_seq = delta["seq"]
            seen_types.add(delta["type"])

        # The demo moves aircraft every tick, so tracks must arrive over the bus.
        assert "track_upsert" in seen_types


def test_ws_track_delta_carries_provenance_and_locality(broker_settings: Settings) -> None:
    with _app(broker_settings) as client, client.websocket_connect("/ws/v2") as ws:
        ws.receive_json()  # snapshot
        track = None
        for _ in range(40):
            msg = ws.receive_json()
            if msg["type"] == "track_upsert":
                track = msg["record"]
                break
        assert track is not None
        assert track["kind"] == "track"
        assert "locally_received" in track
        assert track["geometry"]["type"] == "Point"


def _drain_tracks(ws: object, frames: int) -> dict[str, dict]:
    """Read ``frames`` deltas, returning the latest track record per id."""
    by_id: dict[str, dict] = {}
    last_seq = -1
    for _ in range(frames):
        msg = ws.receive_json()  # type: ignore[attr-defined]
        assert msg["seq"] > last_seq  # monotonic, no gaps on a fresh client
        last_seq = msg["seq"]
        if msg["type"] == "track_upsert":
            by_id[msg["record"]["id"]] = msg["record"]
    return by_id


def test_demo_fusion_appears_as_one_track(broker_settings: Settings) -> None:
    with _app(broker_settings) as client, client.websocket_connect("/ws/v2") as ws:
        ws.receive_json()  # snapshot
        tracks = _drain_tracks(ws, 80)

        # demo01: local + network fuse into one track, two contributors.
        d01 = tracks["aircraft:icao:demo01"]
        block = d01["attributes"]["fusion"]
        assert block["fused_count"] == 2
        contributors = {c["source"] for c in block["contributors"]}
        assert contributors == {"demo", "demo-net"}
        prov = {p["source"] for p in d01["provenance"]}
        assert prov == {"demo", "demo-net"}
        # Network fills speed + label; local wins position.
        assert d01["speed_mps"] == 120.0
        assert d01["label"] == "DEMO-FUSE"

        # demo03 local-only, demo04 network-only.
        assert tracks["aircraft:icao:demo03"]["locally_received"] is True
        assert tracks["aircraft:icao:demo04"]["locally_received"] is False

        # The demo carries a military-classification example on each §11.5 basis
        # (PRD §31.4): demo03 provider-reported, demo04 address-block — both hedged
        # below "high" confidence.
        d03_cls = tracks["aircraft:icao:demo03"]["classification"]
        d04_cls = tracks["aircraft:icao:demo04"]["classification"]
        assert d03_cls is not None and d03_cls["basis"] == "provider"
        assert d04_cls is not None and d04_cls["basis"] == "address_block"
        assert {d03_cls["confidence"], d04_cls["confidence"]}.isdisjoint({"high"})
        # demo02 survives and renders (its strict LOCAL→NET flip is asserted at the
        # engine-unit level — 0.05s ticks can't exceed the 60s local-expire window).
        assert "aircraft:icao:demo02" in tracks


def test_snapshot_and_deltas_carry_cseq(broker_settings: Settings) -> None:
    # The additive per-connection cseq rides every server frame (PRD §22.4); the
    # default (unconfigured station) filter is unbounded, so cseq tracks seq 1:1.
    with _app(broker_settings) as client, client.websocket_connect("/ws/v2") as ws:
        snapshot = ws.receive_json()
        assert snapshot["type"] == "snapshot"
        assert snapshot["cseq"] == 0
        last_cseq = 0
        for _ in range(20):
            delta = ws.receive_json()
            assert delta["cseq"] == last_cseq + 1  # contiguous, no gaps
            last_cseq = delta["cseq"]


def test_subscribe_filters_snapshot_and_resets_cseq(broker_settings: Settings) -> None:
    with _app(broker_settings) as client, client.websocket_connect("/ws/v2") as ws:
        ws.receive_json()  # default (unbounded) snapshot
        ws.send_json({"type": "subscribe", "bbox": _DEMO_BBOX, "track_types": ["aircraft"]})
        # The next snapshot is the resubscribe response: filtered + cseq reset to 0.
        snapshot = _next_snapshot(ws)
        assert snapshot["cseq"] == 0
        ids = {t["id"] for t in snapshot["tracks"]}
        # Tracks outside the bbox are absent; the cluster inside is present.
        assert "aircraft:icao:demo03" not in ids
        assert "aircraft:icao:demo01" not in ids


def test_subscribed_cseq_gap_free_while_global_seq_skips(broker_settings: Settings) -> None:
    with _app(broker_settings) as client, client.websocket_connect("/ws/v2") as ws:
        ws.receive_json()
        ws.send_json({"type": "subscribe", "bbox": _DEMO_BBOX, "track_types": ["aircraft"]})
        snapshot = _next_snapshot(ws)
        last_cseq = snapshot["cseq"]
        saw_seq_skip = False
        last_seq = snapshot["seq"]
        # Across many filtered frames cseq stays contiguous even where the GLOBAL
        # seq legitimately skips (frames for out-of-bbox tracks are filtered out and
        # consume no cseq) — proving "filtered" is cleanly separated from "dropped".
        for _ in range(60):
            delta = ws.receive_json()
            assert delta["cseq"] == last_cseq + 1
            last_cseq = delta["cseq"]
            if delta["seq"] > last_seq + 1:
                saw_seq_skip = True
            last_seq = delta["seq"]
        assert saw_seq_skip, "expected the global seq to skip for a filtered client"


def test_resubscribe_widen_yields_fresh_snapshot_with_cseq_reset(
    broker_settings: Settings,
) -> None:
    with _app(broker_settings) as client, client.websocket_connect("/ws/v2") as ws:
        ws.receive_json()
        ws.send_json({"type": "subscribe", "bbox": _DEMO_BBOX, "track_types": ["aircraft"]})
        narrow = _next_snapshot(ws)
        narrow_ids = {t["id"] for t in narrow["tracks"]}
        assert "aircraft:icao:demo03" not in narrow_ids
        # Drain a few deltas so cseq advances, then widen — the widened subscribe is
        # a resync point: a FRESH filtered snapshot with cseq back at 0 that now
        # backfills the previously-filtered tracks (a pure delta stream could not).
        for _ in range(5):
            ws.receive_json()
        # The client debounces (~300ms) > the server's subscribe min-interval, so a
        # genuine viewport change is never coalesced away. Wait past the guard so the
        # widen is accepted as a fresh resync point rather than rate-limited.
        time.sleep(SUBSCRIBE_MIN_INTERVAL_S + 0.05)
        ws.send_json({"type": "subscribe", "bbox": None, "track_types": ["aircraft"]})
        wide = _next_snapshot(ws)
        assert wide["cseq"] == 0
        wide_ids = {t["id"] for t in wide["tracks"]}
        assert "aircraft:icao:demo03" in wide_ids  # backfilled by the resync snapshot


def test_remove_for_filtered_out_id_is_forwarded(broker_settings: Settings) -> None:
    # A track sent in a snapshot, then narrowed out of the filter, must still get
    # its remove forwarded so the client never strands a ghost. We assert the
    # mechanism via the Hub directly (deterministic; the demo never removes a track
    # mid-run, so an end-to-end remove can't be provoked over the bus reliably).
    from datetime import UTC, datetime

    from aether.backend.hub import Hub
    from aether.backend.subscription import ClientFilter
    from aether.schema.geometry import Point
    from aether.schema.records import TrackRecord

    hub = Hub()
    conn = hub.register(ClientFilter())  # unbounded → track is sent
    track = TrackRecord(
        id="aircraft:icao:ghost",
        source="demo",
        observed_at=datetime(2026, 6, 15, tzinfo=UTC),
        received_at=datetime(2026, 6, 15, tzinfo=UTC),
        published_at=datetime(2026, 6, 15, tzinfo=UTC),
        track_type="aircraft",
        geometry=Point(coordinates=[10.0, 10.0]),
        locally_received=False,
    )
    hub.publish(track)
    assert "aircraft:icao:ghost" in conn.sent_ids
    # Narrow the filter so the track would now be filtered OUT...
    conn.filter = ClientFilter(bbox=(-1.0, -1.0, 1.0, 1.0))
    rm = hub.state.remove("track", "aircraft:icao:ghost")
    hub._dispatch(conn, rm)  # type: ignore[attr-defined]
    # The remove was force-forwarded despite the filter, and the id forgotten.
    frames = [conn.queue.get_nowait() for _ in range(conn.queue.qsize())]
    assert any(f["type"] == "remove" and f["id"] == "aircraft:icao:ghost" for f in frames)
    assert "aircraft:icao:ghost" not in conn.sent_ids


def test_remove_for_never_sent_id_produces_no_frame_and_no_cseq(
    broker_settings: Settings,
) -> None:
    # A remove for an id this connection was never sent (e.g. an AOI-wide expire()
    # for a track outside the client's viewport) must be dropped entirely: no frame
    # enqueued and no cseq consumed — otherwise every global remove burns cseq and
    # bandwidth on a viewport-narrowed client.
    from aether.backend.hub import Hub
    from aether.backend.subscription import ClientFilter

    hub = Hub()
    conn = hub.register(ClientFilter())
    assert conn.cseq == 0
    rm = hub.state.remove("track", "aircraft:icao:never-sent")
    hub._dispatch(conn, rm)  # type: ignore[attr-defined]
    assert conn.queue.qsize() == 0  # no frame enqueued
    assert conn.cseq == 0  # no cseq consumed


def test_malformed_subscribe_keeps_connection(broker_settings: Settings) -> None:
    with _app(broker_settings) as client, client.websocket_connect("/ws/v2") as ws:
        ws.receive_json()
        ws.send_json({"type": "subscribe", "bbox": [1.0, 2.0]})  # bad arity → ignored
        # The connection survives and keeps streaming deltas (prior filter kept).
        delta = ws.receive_json()
        assert "cseq" in delta


def test_default_snapshot_station_scoped_when_configured(broker_settings: Settings) -> None:
    scoped = dataclasses.replace(
        broker_settings,
        demo_source=True,
        station_lat=41.1,
        station_lon=-94.6,
        station_radius_nm=20.0,
    )
    far = {"aircraft:icao:demo01", "aircraft:icao:demo03"}
    with TestClient(create_app(settings=scoped, demo_interval_s=0.05)) as client:
        with client.websocket_connect("/ws/v2") as ws:
            snapshot = ws.receive_json()
            assert far.isdisjoint({t["id"] for t in snapshot["tracks"]})
            # And the default filter scopes the DELTA stream too: the far cluster
            # never reaches this station-scoped client over many frames.
            for _ in range(60):
                msg = ws.receive_json()
                if msg["type"] == "track_upsert":
                    assert msg["record"]["id"] not in far


def _next_snapshot(ws: object) -> dict:
    """Read frames until the next ``snapshot`` (skips interleaved deltas)."""
    for _ in range(200):
        msg = ws.receive_json()  # type: ignore[attr-defined]
        if msg["type"] == "snapshot":
            return msg  # type: ignore[no-any-return]
    raise AssertionError("no snapshot arrived")

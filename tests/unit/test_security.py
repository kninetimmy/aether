"""Browser-security hardening: Host/Origin validation + response headers (M7.1, PRD §26.2).

Two layers:

* The pure ASGI :class:`SecurityMiddleware` is driven directly with hand-built
  scopes — hermetic, no broker/portal — to prove Host/Origin acceptance and rejection
  on **both** ``http`` and ``websocket`` scopes and the stamped hardening headers.
* One :class:`TestClient` pass over :func:`create_app` proves the middleware is wired
  (HTTP only, so no lifespan/broker is needed): a loopback Host serves, a foreign Host
  is 403, and the default field value leaves direct-construction callers unaffected.
"""

import asyncio

from fastapi.testclient import TestClient

from aether.backend.main import create_app
from aether.backend.security import (
    SecurityMiddleware,
    _host_only,
    _origin_host,
    trusted_hosts,
)
from aether.config import DEFAULT_CONTENT_SECURITY_POLICY, Settings

LOOPBACK = frozenset({"localhost", "127.0.0.1", "::1"})


# --------------------------------------------------------------------------- helpers


class _StubApp:
    """Inner ASGI app that records whether it ran and emits a minimal 200/accept."""

    def __init__(self) -> None:
        self.called = False
        self.seen_scope: dict | None = None

    async def __call__(self, scope, receive, send) -> None:
        self.called = True
        self.seen_scope = scope
        if scope["type"] == "http":
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [(b"content-type", b"text/plain")],
                }
            )
            await send({"type": "http.response.body", "body": b"ok"})
        elif scope["type"] == "websocket":
            await receive()  # websocket.connect
            await send({"type": "websocket.accept"})


def _http_scope(host="localhost:8000", origin=None):
    headers = [(b"host", host.encode())] if host is not None else []
    if origin is not None:
        headers.append((b"origin", origin.encode()))
    return {"type": "http", "headers": headers}


def _ws_scope(host="localhost:8000", origin=None):
    scope = _http_scope(host, origin)
    scope["type"] = "websocket"
    return scope


def _drive(mw, scope, incoming=None):
    """Run the middleware once; return the list of messages it sent downstream."""

    async def run():
        queued = list(incoming or [{"type": "websocket.connect"}])
        sent: list[dict] = []

        async def receive():
            return queued.pop(0) if queued else {"type": "http.disconnect"}

        async def send(message):
            sent.append(message)

        await mw(scope, receive, send)
        return sent

    return asyncio.run(run())


def _start_headers(sent):
    """The header dict from the response-start (or websocket close) the middleware sent."""
    for msg in sent:
        if msg["type"] in ("http.response.start", "websocket.close"):
            return {k.decode(): v.decode() for k, v in msg.get("headers", [])}, msg
    raise AssertionError(f"no start/close message in {sent!r}")


# ------------------------------------------------------------------- pure host parsing


def test_host_only_strips_port_and_brackets():
    assert _host_only("localhost:8000") == "localhost"
    assert _host_only("127.0.0.1:8000") == "127.0.0.1"
    assert _host_only("[::1]:8000") == "::1"
    assert _host_only("Pi.Tailnet.TS.NET") == "pi.tailnet.ts.net"  # lowercased
    assert _host_only("example.com") == "example.com"
    assert _host_only(None) == ""
    assert _host_only("") == ""


def test_origin_host_extracts_hostname_or_none():
    assert _origin_host("http://localhost:5173") == "localhost"
    assert _origin_host("https://evil.example") == "evil.example"
    assert _origin_host("https://pi.tailnet.ts.net") == "pi.tailnet.ts.net"
    assert _origin_host("null") == ""  # sandboxed-iframe Origin: present but hostless
    assert _origin_host(None) is None  # header absent — distinct from ""


def test_trusted_hosts_is_loopback_plus_operator_allowlist():
    cfg = Settings(allowed_hosts=("Pi.Tailnet.TS.NET", "  ", "other.host"))
    assert trusted_hosts(cfg) == LOOPBACK | {"pi.tailnet.ts.net", "other.host"}
    assert trusted_hosts(Settings()) == LOOPBACK  # no allow-list → loopback only


# -------------------------------------------------------------------- HTTP scope gating


def test_http_trusted_host_passes_and_stamps_hardening_headers():
    app = _StubApp()
    mw = SecurityMiddleware(app, allowed_hosts=LOOPBACK, content_security_policy="csp-value")
    sent = _drive(mw, _http_scope(host="localhost:8000"))
    headers, start = _start_headers(sent)
    assert app.called is True
    assert start["status"] == 200
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["x-frame-options"] == "SAMEORIGIN"
    assert headers["referrer-policy"] == "no-referrer"
    assert headers["content-security-policy"] == "csp-value"


def test_http_absent_origin_is_allowed_non_browser_client():
    app = _StubApp()
    mw = SecurityMiddleware(app, allowed_hosts=LOOPBACK)
    _drive(mw, _http_scope(host="127.0.0.1:8000", origin=None))
    assert app.called is True  # curl / server-to-server (no Origin) still served


def test_http_trusted_origin_is_allowed():
    app = _StubApp()
    mw = SecurityMiddleware(app, allowed_hosts=LOOPBACK)
    _drive(mw, _http_scope(host="localhost:8000", origin="http://localhost:5173"))
    assert app.called is True  # vite dev proxy forwards Origin: localhost — trusted


def test_http_untrusted_host_is_rejected_403():
    app = _StubApp()
    mw = SecurityMiddleware(app, allowed_hosts=LOOPBACK)
    sent = _drive(mw, _http_scope(host="evil.example"))
    _headers, start = _start_headers(sent)
    assert app.called is False  # inner app never runs (DNS-rebinding blocked)
    assert start["status"] == 403


def test_http_untrusted_origin_is_rejected_403_even_with_trusted_host():
    app = _StubApp()
    mw = SecurityMiddleware(app, allowed_hosts=LOOPBACK)
    sent = _drive(mw, _http_scope(host="localhost:8000", origin="https://evil.example"))
    _headers, start = _start_headers(sent)
    assert app.called is False  # cross-site fetch blocked though Host would pass
    assert start["status"] == 403


def test_operator_allowlist_admits_the_tailscale_name():
    app = _StubApp()
    cfg = Settings(allowed_hosts=("pi.tailnet.ts.net",))
    mw = SecurityMiddleware(app, allowed_hosts=trusted_hosts(cfg))
    _drive(mw, _http_scope(host="pi.tailnet.ts.net", origin="https://pi.tailnet.ts.net"))
    assert app.called is True  # Tailscale Serve path (Host + Origin both the MagicDNS name)


def test_empty_csp_omits_the_header_but_keeps_the_hardening_headers():
    app = _StubApp()
    mw = SecurityMiddleware(app, allowed_hosts=LOOPBACK, content_security_policy="")
    headers, _start = _start_headers(_drive(mw, _http_scope()))
    assert "content-security-policy" not in headers
    assert headers["x-content-type-options"] == "nosniff"


def test_disabled_middleware_is_a_transparent_passthrough():
    app = _StubApp()
    mw = SecurityMiddleware(app, allowed_hosts=LOOPBACK, enabled=False)
    headers, start = _start_headers(_drive(mw, _http_scope(host="evil.example")))
    assert app.called is True  # untrusted host served when disabled
    assert start["status"] == 200
    assert "x-content-type-options" not in headers  # no stamping when off


def test_lifespan_scope_passes_straight_through():
    app = _StubApp()
    mw = SecurityMiddleware(app, allowed_hosts=frozenset(), enabled=True)

    async def run():
        await mw({"type": "lifespan"}, _noop_receive, _noop_send)

    asyncio.run(run())
    assert app.called is True  # startup/shutdown is never gated


async def _noop_receive():
    return {"type": "lifespan.startup"}


async def _noop_send(_message):
    return None


# --------------------------------------------------------------- websocket scope gating


def test_ws_untrusted_origin_is_closed_before_accept():
    app = _StubApp()
    mw = SecurityMiddleware(app, allowed_hosts=LOOPBACK)
    sent = _drive(
        mw,
        _ws_scope(host="localhost:8000", origin="https://evil.example"),
        incoming=[{"type": "websocket.connect"}],
    )
    assert app.called is False  # the ws endpoint never sees a foreign-origin handshake
    assert sent == [{"type": "websocket.close", "code": 1008}]


def test_ws_untrusted_host_is_closed_before_accept():
    app = _StubApp()
    mw = SecurityMiddleware(app, allowed_hosts=LOOPBACK)
    sent = _drive(mw, _ws_scope(host="evil.example"), incoming=[{"type": "websocket.connect"}])
    assert app.called is False
    assert sent == [{"type": "websocket.close", "code": 1008}]


def test_ws_trusted_handshake_reaches_the_endpoint():
    app = _StubApp()
    mw = SecurityMiddleware(app, allowed_hosts=LOOPBACK)
    sent = _drive(
        mw,
        _ws_scope(host="localhost:8000", origin="http://localhost:5173"),
        incoming=[{"type": "websocket.connect"}],
    )
    assert app.called is True
    assert sent == [{"type": "websocket.accept"}]  # inner app accepted, no close


# ---------------------------------------------------------------- wired into create_app


def _client(**overrides) -> TestClient:
    # No `with` → lifespan/broker never starts; the HTTP route + middleware still run.
    cfg = Settings(demo_source=False, persist=False, security_enabled=True, **overrides)
    return TestClient(create_app(settings=cfg), base_url="http://localhost")


def test_create_app_serves_trusted_host_with_headers():
    resp = _client().get("/api/health")
    assert resp.status_code == 200
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["content-security-policy"] == DEFAULT_CONTENT_SECURITY_POLICY


def test_create_app_rejects_foreign_host():
    resp = TestClient(
        create_app(settings=Settings(demo_source=False, persist=False, security_enabled=True)),
        base_url="http://evil.example",
    ).get("/api/health")
    assert resp.status_code == 403


def test_create_app_rejects_foreign_origin():
    resp = _client().get("/api/health", headers={"origin": "https://evil.example"})
    assert resp.status_code == 403


def test_security_disabled_by_field_default_serves_any_host():
    # The field default is False, so the 829 direct-construction tests are unaffected:
    # a foreign Host is served and no hardening headers are stamped.
    resp = TestClient(
        create_app(settings=Settings(demo_source=False, persist=False)),
        base_url="http://testserver",
    ).get("/api/health")
    assert resp.status_code == 200
    assert "x-content-type-options" not in resp.headers

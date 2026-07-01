"""Browser-security hardening middleware (M7.1, PRD §26.2).

A **pure-ASGI** middleware: it must wrap both ``http`` and ``websocket`` scopes, so
it cannot be a Starlette ``BaseHTTPMiddleware`` (those see HTTP only, which would
leave ``/ws/v2`` — the live data stream — unguarded). It enforces the
loopback/Tailscale trust boundary the PRD requires:

* **Host validation** (DNS-rebinding defence): the Host header's hostname must be
  trusted. A malicious page that resolves its own domain to ``127.0.0.1`` to reach
  the loopback backend still sends *its own* name in the Host header → rejected.
* **Origin validation** (cross-site defence): when an Origin header is present — every
  browser ``fetch``/``XHR``/WebSocket handshake sends one — its hostname must be
  trusted. A page on ``evil.example`` scripting a request to the COP sends
  ``Origin: https://evil.example`` → rejected, even though its Host would pass. A
  request with *no* Origin (``curl``, server-to-server, the test client) is allowed:
  the attack surface here is the browser, which always sends Origin on these requests.
* **Response headers**: a CSP (map-compatible, configurable) plus ``nosniff``,
  ``X-Frame-Options: SAMEORIGIN`` and a strict ``Referrer-Policy`` on every HTTP
  response.

Trusted hostnames = loopback (always) ∪ ``allowed_hosts`` (the operator adds their
Tailscale MagicDNS name to reach the COP over Serve). Matching is hostname-only —
port- and scheme-insensitive — like Django's ``ALLOWED_HOSTS``. Disabled ⇒ a
transparent passthrough (:data:`Settings.security_enabled`).
"""

from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any
from urllib.parse import urlsplit

from aether.config import LOOPBACK_HOSTS, Settings

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

#: Unconditional hardening headers stamped onto every HTTP response (PRD §26.2).
#: ``nosniff`` blocks MIME-confusion; ``SAMEORIGIN`` blocks clickjacking of the COP
#: in a foreign frame; ``no-referrer`` keeps the (private) tailnet URL out of any
#: outbound tile/CDN request's Referer. CSP is appended separately (configurable).
_HARDENING_HEADERS: tuple[tuple[bytes, bytes], ...] = (
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"SAMEORIGIN"),
    (b"referrer-policy", b"no-referrer"),
)


def trusted_hosts(cfg: Settings) -> frozenset[str]:
    """The set of trusted hostnames: loopback (always) ∪ the operator's allow-list."""
    return frozenset(LOOPBACK_HOSTS) | {h.strip().lower() for h in cfg.allowed_hosts if h.strip()}


def _host_only(host_header: str | None) -> str:
    """Lowercase hostname of a Host header, stripping the port and IPv6 brackets.

    ``localhost:8000`` → ``localhost``; ``[::1]:8000`` → ``::1``; ``pi.ts.net`` →
    ``pi.ts.net``. Empty/missing → ``""`` (never trusted). A bracketed IPv6 literal is
    unwrapped; otherwise a single trailing ``:port`` is dropped (a bare IPv6 literal is
    not valid in a Host header, so a lone colon is always a port separator).
    """
    if not host_header:
        return ""
    value = host_header.strip()
    if value.startswith("["):  # [IPv6]:port
        end = value.find("]")
        return value[1:end].lower() if end != -1 else value.lower()
    return value.rsplit(":", 1)[0].lower() if ":" in value else value.lower()


def _origin_host(origin: str | None) -> str | None:
    """Hostname of an Origin header (``scheme://host[:port]``); ``None`` if absent.

    Returns ``""`` for a present-but-hostless Origin (e.g. the literal ``null`` from a
    sandboxed iframe), which is never trusted — distinct from ``None`` (header absent),
    which the caller treats as a non-browser request and allows.
    """
    if origin is None:
        return None
    return (urlsplit(origin).hostname or "").lower()


class _Headers:
    """Case-insensitive first-match lookup over the raw ASGI header list."""

    def __init__(self, raw: list[tuple[bytes, bytes]]) -> None:
        self._raw = raw

    def get(self, name: str) -> str | None:
        key = name.lower().encode("latin-1")
        for k, v in self._raw:
            if k.lower() == key:
                return v.decode("latin-1")
        return None


class SecurityMiddleware:
    """Validate Host/Origin and stamp hardening headers (see the module docstring)."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        allowed_hosts: frozenset[str],
        content_security_policy: str = "",
        enabled: bool = True,
    ) -> None:
        self.app = app
        self.allowed_hosts = allowed_hosts
        self.csp = content_security_policy
        self.enabled = enabled

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # Only http/websocket carry Host/Origin; ``lifespan`` (and anything else) passes
        # straight through so the middleware never interferes with startup/shutdown.
        if not self.enabled or scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        headers = _Headers(list(scope.get("headers") or []))
        if not self._is_trusted(headers):
            await self._reject(scope, receive, send)
            return

        if scope["type"] == "http":
            await self.app(scope, receive, self._stamp_headers(send))
            return
        await self.app(scope, receive, send)

    def _is_trusted(self, headers: _Headers) -> bool:
        if _host_only(headers.get("host")) not in self.allowed_hosts:
            return False
        origin_host = _origin_host(headers.get("origin"))
        # Origin absent → non-browser client, allowed (Host already validated). Present
        # → its hostname must be trusted (empty string for a hostless Origin never is).
        return origin_host is None or origin_host in self.allowed_hosts

    def _stamp_headers(self, send: Send) -> Send:
        async def wrapped(message: Message) -> None:
            if message["type"] == "http.response.start":
                out = list(message.get("headers") or [])
                out.extend(_HARDENING_HEADERS)
                if self.csp:
                    out.append((b"content-security-policy", self.csp.encode("latin-1")))
                message = {**message, "headers": out}
            await send(message)

        return wrapped

    async def _reject(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "websocket":
            # Consume the connect event, then close before accepting: uvicorn turns a
            # pre-accept close into an HTTP 403 handshake failure. This is the portable
            # denial across ASGI servers (the websocket denial-response extension is not
            # universally available); 1008 = policy violation.
            await receive()
            await send({"type": "websocket.close", "code": 1008})
            return
        body = b"Forbidden: untrusted Host or Origin"
        await send(
            {
                "type": "http.response.start",
                "status": 403,
                "headers": [
                    (b"content-type", b"text/plain; charset=utf-8"),
                    (b"content-length", str(len(body)).encode("latin-1")),
                    *_HARDENING_HEADERS,
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})

"""The check against requests that another website makes a browser send.

A request that changes something and carries the owner's sign-in cookie must
come from Hybro's own address, and so must sign-in and setup: its ``Origin``
has to name the host and port the browser used. A request without the cookie
is not checked here, so programs, which send no ``Origin``, are not affected.

"Hybro's own address" is ``X-Forwarded-Host`` when present, else ``Host``.
Caddy in the image and the Next rewrite proxy in development both put the
browser's host there, and a web page cannot set that header on a cross-site
request without a preflight. Nothing else stops a plain cross-site POST:
FastAPI parses a body without a ``Content-Type`` as JSON, and ``SameSite``
ignores ports.
"""

from __future__ import annotations

import re

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from api_gateway.owner_session import SESSION_COOKIE

_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_DEFAULT_PORTS = frozenset({80, 443})
# A host name, an IPv4 address or a bracketed IPv6 address, and an optional
# port. Anything else, such as user info, a path or a stray character, is not
# an address.
_ADDRESS = re.compile(r"(\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9._-]+)(?::([0-9]{1,5}))?")


def _host_and_port(netloc: str) -> tuple[str, int | None] | None:
    """Lower-cased host and its port, ``None`` for a default or absent port."""
    match = _ADDRESS.fullmatch(netloc)
    if match is None:
        return None
    port = int(match.group(2)) if match.group(2) else None
    return match.group(1).lower(), None if port in _DEFAULT_PORTS else port


def same_address(origin: str, host: str) -> bool:
    """Whether ``origin`` names the host and port in ``host``.

    Ports 80 and 443 count as no port, because an ``Origin`` on a default
    port carries none. The scheme is not compared.
    """
    scheme, separator, netloc = origin.partition("://")
    if not separator or scheme not in ("http", "https"):
        return False
    ours = _host_and_port(host.strip(" "))
    return ours is not None and _host_and_port(netloc) == ours


def from_own_address(request: Request) -> bool:
    """Whether the request's ``Origin`` names Hybro's own address."""
    origin = request.headers.get("origin")
    if origin is None:
        return False
    forwarded = request.headers.get("x-forwarded-host")
    # Several proxies list their hosts; the first is the browser's.
    host = forwarded.split(",")[0] if forwarded else request.headers.get("host", "")
    return same_address(origin, host)


class CrossSiteCheckMiddleware:
    def __init__(self, app: ASGIApp, *, api_prefix: str) -> None:
        self.app = app
        prefix = api_prefix.rstrip("/")
        # Checked without a cookie: these two are how the cookie is obtained.
        self._always = frozenset({f"{prefix}/auth/sign-in", f"{prefix}/auth/setup"})

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["method"] not in _SAFE_METHODS:
            request = Request(scope)
            in_scope = (
                scope["path"] in self._always or SESSION_COOKIE in request.cookies
            )
            if in_scope and not from_own_address(request):
                response = JSONResponse(
                    {
                        "detail": {
                            "code": "cross_site_request",
                            "message": (
                                "This request did not come from this Hybro address."
                            ),
                        }
                    },
                    status_code=403,
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


__all__ = ["CrossSiteCheckMiddleware", "from_own_address", "same_address"]

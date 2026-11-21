"""The check that a request names an address this Hybro answers to.

A DNS rebinding page points its own name at this machine; the browser then
sends that name as ``Host`` and, on a changing request, as ``Origin``. The two
match, so the cross-site check passes, and a request without a source token
would reach the Source API from that website. This check refuses the name.

Trusted are the loopback names Caddy's loopback listener accepts
(``localhost``, ``127.0.0.1``, ``[::1]``; not names under ``.localhost``, which
not every resolver pins to loopback), any IP literal, which cannot be rebound, and the
hosts of ``frontend_origins`` (in the image the entrypoint derives them from
the listener and ``frontend.server_url``) and of
``a2a_push_callback_base_url``, which remote Agents call back on. Any port is
allowed, so SSH and IDE port forwarding keep working. ``Host`` and every entry of
``X-Forwarded-Host`` must be trusted: a rebinding page cannot change ``Host``
but can add the other header. ``/health`` answers any name; it returns no data
and probes call it by any address.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Iterable
from urllib.parse import urlsplit

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from api_gateway.cross_site import host_and_port
from common.config.loader import Settings

_EXEMPT_PATHS = frozenset({"/health"})
_LOOPBACK_NAMES = frozenset({"localhost", "127.0.0.1", "[::1]"})


def trusted_origins(settings: Settings) -> list[str]:
    """The addresses this Hybro answers to besides loopback and IP literals."""
    origins = list(settings.frontend_origins)
    if settings.a2a_push_callback_base_url:
        origins.append(settings.a2a_push_callback_base_url)
    return origins


def trusted_names(origins: Iterable[str]) -> frozenset[str]:
    """The lower-cased host names of the configured origins."""
    names = set()
    for origin in origins:
        try:
            host = urlsplit(origin).hostname
        except ValueError:
            continue
        if not host:
            continue
        if host.isascii():
            names.add(host.lower())
            continue
        try:
            # A browser sends a Unicode name in its ASCII (IDNA) form. Python's
            # IDNA 2003 codec maps some characters (such as "ß") to another
            # name than browsers do, so a name it does not round-trip is not
            # trusted; its punycode form can be configured instead.
            ascii_name = host.encode("idna").decode("ascii")
            if ascii_name.encode("ascii").decode("idna") != host:
                continue
        except UnicodeError:
            continue
        names.add(ascii_name.lower())
    return frozenset(names)


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host[1:-1] if host.startswith("[") else host)
    except ValueError:
        return False
    return True


def trusted_host(value: str, names: frozenset[str]) -> bool:
    """Whether a ``Host`` value names this Hybro."""
    parsed = host_and_port(value.strip(" "))
    if parsed is None:
        return False
    host = parsed[0]
    return host in _LOOPBACK_NAMES or _is_ip_literal(host) or host in names


class TrustedHostMiddleware:
    def __init__(self, app: ASGIApp, *, origins: Iterable[str]) -> None:
        self.app = app
        self._names = trusted_names(origins)

    def _trusted(self, scope: Scope) -> bool:
        headers = Headers(scope=scope)
        hosts = headers.getlist("host")
        # Every header line and every comma-separated proxy entry.
        forwarded = [
            entry
            for line in headers.getlist("x-forwarded-host")
            for entry in line.split(",")
        ]
        return bool(hosts) and all(
            trusted_host(host, self._names) for host in [*hosts, *forwarded]
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] == "http"
            and scope["path"] not in _EXEMPT_PATHS
            and not self._trusted(scope)
        ):
            response = JSONResponse(
                {
                    "detail": {
                        "code": "unknown_host",
                        "message": (
                            "This Hybro does not answer to this address. To "
                            "reach it by this name, set frontend.server_url in "
                            "config.json to that address (in a checkout, add it "
                            "to backend.frontend_origins)."
                        ),
                    }
                },
                status_code=421,
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


__all__ = [
    "TrustedHostMiddleware",
    "trusted_host",
    "trusted_names",
    "trusted_origins",
]

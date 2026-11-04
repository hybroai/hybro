"""Retry loopback agent URLs through the Docker host gateway.

An agent that is reachable at localhost from the host may only be reachable at
``host.docker.internal`` from inside a container, and vice versa. Both the card
(which names the interfaces to call) and a bare URL are retried once through the
alternate host when the first attempt fails for a network reason.

A2A 1.0 cards carry their endpoints in ``supported_interfaces`` rather than a
top-level ``url``, so the rewritten card replaces the host on every advertised
interface instead of a single field.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import aclosing
from typing import Any
from urllib.parse import urlparse, urlunparse

import httpx
from a2a.client.errors import A2AClientError
from a2a.utils.errors import A2AError

from common.observability import events as E
from common.observability import get_logger
from common.url_utils import LOCAL_HOST_ALIASES

logger = get_logger(__name__)

_DOCKER_HOST = "host.docker.internal"
_NETWORK_ERROR_MARKERS = (
    "network communication",
    "connection",
    "connect",
    "all connection attempts failed",
)


async def with_docker_host_fallback[T](
    card: Any,
    operation: Callable[[Any], Awaitable[T]],
) -> T:
    try:
        return await operation(card)
    except Exception as exc:
        fallback_card = _fallback_card(card, exc)
        if fallback_card is None:
            raise
        return await operation(fallback_card)


async def with_docker_host_url_fallback[T](
    url: str,
    operation: Callable[[str], Awaitable[T]],
) -> T:
    try:
        return await operation(url)
    except Exception as exc:
        fallback_url = docker_host_fallback_url_for_error(url, exc)
        if fallback_url is None:
            raise
        logger.emit(
            E.A2A_DOCKER_HOST_FALLBACK_SELECTED,
            original_url=url,
            fallback_url=fallback_url,
        )
        return await operation(fallback_url)


async def stream_with_docker_host_url_fallback[T](
    url: str,
    operation: Callable[[str], AsyncGenerator[T, None]],
) -> AsyncGenerator[T, None]:
    yielded_any = False
    try:
        async with aclosing(operation(url)) as stream:
            async for item in stream:
                yielded_any = True
                yield item
    except Exception as exc:
        fallback_url = (
            None if yielded_any else docker_host_fallback_url_for_error(url, exc)
        )
        if fallback_url is None:
            raise
        logger.emit(
            E.A2A_DOCKER_HOST_FALLBACK_SELECTED,
            original_url=url,
            fallback_url=fallback_url,
        )
        async with aclosing(operation(fallback_url)) as stream:
            async for item in stream:
                yield item


async def stream_with_docker_host_fallback[T](
    card: Any,
    operation: Callable[[Any], AsyncGenerator[T, None]],
) -> AsyncGenerator[T, None]:
    yielded_any = False
    try:
        async with aclosing(operation(card)) as stream:
            async for item in stream:
                yielded_any = True
                yield item
    except Exception as exc:
        fallback_card = None if yielded_any else _fallback_card(card, exc)
        if fallback_card is None:
            raise
        async with aclosing(operation(fallback_card)) as stream:
            async for item in stream:
                yield item


def _fallback_card(card: Any, exc: Exception) -> Any | None:
    original_url = _card_primary_url(card)
    if not original_url:
        return None
    fallback_url = docker_host_fallback_url_for_error(original_url, exc)
    if fallback_url is None:
        return None
    logger.emit(
        E.A2A_DOCKER_HOST_FALLBACK_SELECTED,
        original_url=original_url,
        fallback_url=fallback_url,
    )
    return _copy_card_with_url(card, fallback_url)


def _card_primary_url(card: Any) -> str | None:
    for interface in getattr(card, "supported_interfaces", None) or []:
        url = getattr(interface, "url", None)
        if url:
            return str(url)
    url = getattr(card, "url", None)
    return str(url) if url else None


def docker_host_fallback_url_for_error(url: str, exc: Exception) -> str | None:
    fallback_url = docker_host_fallback_url(url)
    if fallback_url is None:
        return None
    if not _is_network_connection_error(exc):
        return None
    return fallback_url


def docker_host_fallback_url(url: str) -> str | None:
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    hostname = (parsed.hostname or "").lower()
    if hostname not in LOCAL_HOST_ALIASES:
        return None

    port = parsed.port
    netloc = _DOCKER_HOST if port is None else f"{_DOCKER_HOST}:{port}"
    return urlunparse(
        (
            parsed.scheme,
            netloc,
            parsed.path,
            parsed.params,
            parsed.query,
            parsed.fragment,
        )
    )


def _is_network_connection_error(exc: Exception) -> bool:
    if isinstance(
        exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.NetworkError, OSError)
    ):
        return True
    if getattr(exc, "status_code", None) == 503:
        message = str(exc).lower()
        return any(marker in message for marker in _NETWORK_ERROR_MARKERS)
    # A2AClientError and the adapter's own facade error both wrap a transport
    # failure into a message; match by name so this module stays importable
    # without a circular dependency on client_facade.
    if isinstance(exc, (A2AClientError, A2AError)) or type(exc).__name__ == (
        "A2AClientFacadeError"
    ):
        message = str(exc).lower()
        return any(marker in message for marker in _NETWORK_ERROR_MARKERS)
    return False


def _copy_card_with_url(card: Any, url: str) -> Any:
    """Return a copy of the card whose interfaces all point at ``url``."""
    model_dump = getattr(card, "model_dump", None)
    if callable(model_dump):
        data = model_dump(mode="json", by_alias=True)
        data["url"] = url
        for interface in data.get("supportedInterfaces") or []:
            interface["url"] = url
        return type(card)(**data)

    from a2a.client.card_resolver import parse_agent_card
    from google.protobuf.json_format import MessageToDict

    data = MessageToDict(card)
    data["supportedInterfaces"] = [
        {**interface, "url": url} for interface in data.get("supportedInterfaces") or []
    ]
    return parse_agent_card(data)


__all__ = [
    "docker_host_fallback_url",
    "docker_host_fallback_url_for_error",
    "stream_with_docker_host_fallback",
    "stream_with_docker_host_url_fallback",
    "with_docker_host_fallback",
    "with_docker_host_url_fallback",
]

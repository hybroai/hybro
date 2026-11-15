from __future__ import annotations

import inspect

import httpx

from .address_policy import AddressRefusedError, DestinationPolicy

MAX_A2A_RESPONSE_BYTES = 139_810_136 + 2 * 1024 * 1024
# Terminal SSE events may contain several files up to the aggregate response
# limit. Decoded per-file and aggregate limits remain enforced downstream.
MAX_A2A_SSE_EVENT_BYTES = MAX_A2A_RESPONSE_BYTES
# Agent cards are small JSON documents; a card larger than this is refused
# before it is parsed.
MAX_AGENT_CARD_BYTES = 1024 * 1024


class A2AResponseTooLargeError(httpx.HTTPError):
    pass


class A2AResponseEncodingError(httpx.HTTPError):
    """A compressed response where only an uncompressed one is accepted."""


class _BoundedStream(httpx.AsyncByteStream):
    def __init__(self, stream: httpx.AsyncByteStream, max_bytes: int) -> None:
        self._stream = stream
        self._max_bytes = max_bytes

    async def __aiter__(self):
        received = 0
        async for chunk in self._stream:
            received += len(chunk)
            if received > self._max_bytes:
                raise A2AResponseTooLargeError("A2A response exceeds size limit")
            yield chunk

    async def aclose(self) -> None:
        await self._stream.aclose()


class _PerEventBoundedStream(httpx.AsyncByteStream):
    def __init__(
        self,
        stream: httpx.AsyncByteStream,
        max_bytes: int,
        max_stream_bytes: int = MAX_A2A_RESPONSE_BYTES,
    ) -> None:
        self._stream = stream
        self._max_bytes = max_bytes
        self._max_stream_bytes = max_stream_bytes

    async def __aiter__(self):
        pending = bytearray()
        stream_received = 0
        async for chunk in self._stream:
            stream_received += len(chunk)
            if stream_received > self._max_stream_bytes:
                raise A2AResponseTooLargeError("A2A SSE stream exceeds size limit")
            pending.extend(chunk)
            while True:
                lf_boundary = pending.find(b"\n\n")
                crlf_boundary = pending.find(b"\r\n\r\n")
                boundaries = [
                    boundary
                    for boundary in (lf_boundary, crlf_boundary)
                    if boundary >= 0
                ]
                if not boundaries:
                    break
                boundary = min(boundaries)
                separator_size = (
                    4 if pending[boundary : boundary + 4] == b"\r\n\r\n" else 2
                )
                if boundary > self._max_bytes:
                    raise A2AResponseTooLargeError("A2A SSE event exceeds size limit")
                del pending[: boundary + separator_size]
            if len(pending) > self._max_bytes:
                raise A2AResponseTooLargeError("A2A SSE event exceeds size limit")
            yield chunk

    async def aclose(self) -> None:
        await self._stream.aclose()


def _network_transport() -> httpx.AsyncBaseTransport:
    """The transport that opens real connections; tests replace it."""
    return httpx.AsyncHTTPTransport()


class GuardedAsyncTransport(httpx.AsyncBaseTransport):
    """Dial only addresses the agent's destination policy allows.

    Each request, including every redirect hop and Docker-host retry, resolves
    its host through the policy and is sent to the checked IP address. The
    original ``Host`` header and TLS server name are kept, so virtual hosting
    and certificate checks still use the hostname. Each hostname gets its own
    connection pool: pools are keyed by the dialed IP, and a TLS connection
    verified for one hostname must not be reused for another at the same IP.
    """

    def __init__(self, policy: DestinationPolicy) -> None:
        self._policy = policy
        self._transports: dict[str, httpx.AsyncBaseTransport] = {}

    def _transport_for(self, hostname: str) -> httpx.AsyncBaseTransport:
        key = hostname.lower()
        if key not in self._transports:
            self._transports[key] = _network_transport()
        return self._transports[key]

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        port = url.port or {"http": 80, "https": 443}.get(url.scheme)
        if not url.host or port is None:
            raise AddressRefusedError("unsupported agent URL")
        try:
            addresses = await self._policy.resolve(url.host, port)
        except OSError as exc:
            raise httpx.ConnectError(
                "agent host could not be resolved", request=request
            ) from exc
        extensions = dict(request.extensions)
        if url.scheme == "https":
            extensions["sni_hostname"] = url.host
        transport = self._transport_for(url.host)
        last_error: httpx.ConnectError | None = None
        for _family, address in addresses:
            pinned = httpx.Request(
                request.method,
                url.copy_with(host=address),
                headers=request.headers,
                stream=request.stream,
                extensions=extensions,
            )
            try:
                return await transport.handle_async_request(pinned)
            except httpx.ConnectError as exc:
                last_error = exc
        if last_error is None:
            raise AddressRefusedError("host has no usable address")
        raise last_error

    async def aclose(self) -> None:
        for transport in self._transports.values():
            await transport.aclose()


class BoundedAsyncTransport(httpx.AsyncBaseTransport):
    """Reject oversized A2A responses before JSON/model materialization.

    The limit counts bytes on the wire. With ``identity_only`` a compressed
    response is refused, so the limit also bounds the decoded body.
    """

    def __init__(
        self,
        *,
        policy: DestinationPolicy,
        max_bytes: int = MAX_A2A_RESPONSE_BYTES,
        identity_only: bool = False,
    ) -> None:
        self._max_bytes = max_bytes
        self._identity_only = identity_only
        self._transport = GuardedAsyncTransport(policy)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if self._identity_only:
            request.headers["accept-encoding"] = "identity"
        response = await self._transport.handle_async_request(request)
        encoding = response.headers.get("content-encoding", "identity").strip()
        if self._identity_only and encoding.lower() not in {"", "identity"}:
            await response.aclose()
            raise A2AResponseEncodingError("compressed response refused")
        content_length = response.headers.get("content-length")
        if content_length:
            try:
                declared = int(content_length)
            except ValueError as exc:
                await response.aclose()
                raise A2AResponseTooLargeError("invalid A2A Content-Length") from exc
            if declared > self._max_bytes:
                await response.aclose()
                raise A2AResponseTooLargeError("A2A response exceeds size limit")
        return httpx.Response(
            status_code=response.status_code,
            headers=response.headers,
            stream=_BoundedStream(response.stream, self._max_bytes),
            extensions=response.extensions,
        )

    async def aclose(self) -> None:
        await self._transport.aclose()


class EventBoundedAsyncTransport(httpx.AsyncBaseTransport):
    def __init__(
        self,
        *,
        policy: DestinationPolicy,
        max_bytes: int = MAX_A2A_SSE_EVENT_BYTES,
    ) -> None:
        self._max_bytes = max_bytes
        self._transport = GuardedAsyncTransport(policy)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await self._transport.handle_async_request(request)
        return httpx.Response(
            status_code=response.status_code,
            headers=response.headers,
            stream=_PerEventBoundedStream(response.stream, self._max_bytes),
            extensions=response.extensions,
        )

    async def aclose(self) -> None:
        await self._transport.aclose()


def bounded_client(
    *,
    timeout: float,
    policy: DestinationPolicy,
    max_bytes: int = MAX_A2A_RESPONSE_BYTES,
) -> httpx.AsyncClient:
    """A client for one call to one agent, guarded by ``policy``."""
    parameters = inspect.signature(httpx.AsyncClient).parameters
    if "transport" not in parameters or "trust_env" not in parameters:
        return httpx.AsyncClient(timeout=timeout)
    return httpx.AsyncClient(
        timeout=timeout,
        transport=BoundedAsyncTransport(policy=policy, max_bytes=max_bytes),
        trust_env=False,
    )


def card_client(*, timeout: float, policy: DestinationPolicy) -> httpx.AsyncClient:
    """A client for fetching one Agent Card: 1 MiB decoded at most."""
    parameters = inspect.signature(httpx.AsyncClient).parameters
    if "transport" not in parameters or "trust_env" not in parameters:
        return httpx.AsyncClient(timeout=timeout)
    return httpx.AsyncClient(
        timeout=timeout,
        transport=BoundedAsyncTransport(
            policy=policy,
            max_bytes=MAX_AGENT_CARD_BYTES,
            identity_only=True,
        ),
        trust_env=False,
    )


def event_bounded_client(
    *, timeout: float, policy: DestinationPolicy
) -> httpx.AsyncClient:
    parameters = inspect.signature(httpx.AsyncClient).parameters
    if "transport" not in parameters or "trust_env" not in parameters:
        return httpx.AsyncClient(timeout=timeout)
    return httpx.AsyncClient(
        timeout=timeout,
        transport=EventBoundedAsyncTransport(policy=policy),
        trust_env=False,
    )

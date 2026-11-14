from __future__ import annotations

import inspect

import httpx

MAX_A2A_RESPONSE_BYTES = 139_810_136 + 2 * 1024 * 1024
# Terminal SSE events may contain several files up to the aggregate response
# limit. Decoded per-file and aggregate limits remain enforced downstream.
MAX_A2A_SSE_EVENT_BYTES = MAX_A2A_RESPONSE_BYTES
# Agent cards are small JSON documents; a card larger than this is refused
# before it is parsed.
MAX_AGENT_CARD_BYTES = 1024 * 1024


class A2AResponseTooLargeError(httpx.HTTPError):
    pass


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


class BoundedAsyncTransport(httpx.AsyncBaseTransport):
    """Reject oversized A2A responses before JSON/model materialization."""

    def __init__(self, max_bytes: int = MAX_A2A_RESPONSE_BYTES) -> None:
        self._max_bytes = max_bytes
        self._transport = _network_transport()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await self._transport.handle_async_request(request)
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
    def __init__(self, max_bytes: int = MAX_A2A_SSE_EVENT_BYTES) -> None:
        self._max_bytes = max_bytes
        self._transport = _network_transport()

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
    *, timeout: float, max_bytes: int = MAX_A2A_RESPONSE_BYTES
) -> httpx.AsyncClient:
    parameters = inspect.signature(httpx.AsyncClient).parameters
    if "transport" not in parameters or "trust_env" not in parameters:
        return httpx.AsyncClient(timeout=timeout)
    return httpx.AsyncClient(
        timeout=timeout,
        transport=BoundedAsyncTransport(max_bytes=max_bytes),
        trust_env=False,
    )


def event_bounded_client(*, timeout: float) -> httpx.AsyncClient:
    parameters = inspect.signature(httpx.AsyncClient).parameters
    if "transport" not in parameters or "trust_env" not in parameters:
        return httpx.AsyncClient(timeout=timeout)
    return httpx.AsyncClient(
        timeout=timeout,
        transport=EventBoundedAsyncTransport(),
        trust_env=False,
    )

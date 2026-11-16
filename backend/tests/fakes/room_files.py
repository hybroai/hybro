"""In-memory room file content store for room file tests."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator

from common.protocols.platform_protocols import PreparedFileStream
from room_files.content_store import normalize_file_id
from room_files.errors import FileConflictError, FileTooLargeError


class _MemoryPreparedStream:
    def __init__(self, content: bytes, chunk_size: int) -> None:
        self._content = content
        self._chunk_size = chunk_size
        self._offset = 0
        self._closed = False

    def __aiter__(self) -> _MemoryPreparedStream:
        return self

    async def __anext__(self) -> bytes:
        if self._closed or self._offset >= len(self._content):
            await self.aclose()
            raise StopAsyncIteration
        chunk = self._content[self._offset : self._offset + self._chunk_size]
        self._offset += len(chunk)
        return chunk

    async def aclose(self) -> None:
        self._closed = True


class MemoryFileContentStore:
    def __init__(self) -> None:
        self._contents: dict[str, bytes] = {}
        self._lock = asyncio.Lock()

    async def write(
        self,
        file_id: str,
        content: bytes,
        content_type: str,
    ) -> None:
        del content_type
        normalized = normalize_file_id(file_id)
        async with self._lock:
            if normalized in self._contents:
                raise FileConflictError(normalized)
            self._contents[normalized] = bytes(content)

    async def read(self, file_id: str, max_bytes: int) -> bytes | None:
        normalized = normalize_file_id(file_id)
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        async with self._lock:
            content = self._contents.get(normalized)
        if content is None:
            return None
        if len(content) > max_bytes:
            raise FileTooLargeError(normalized)
        return content

    async def stream(self, file_id: str, chunk_size: int) -> AsyncIterator[bytes]:
        prepared = await self.prepare_stream(file_id, chunk_size)
        if prepared is None:
            return
        try:
            async for chunk in prepared:
                yield chunk
        finally:
            await prepared.aclose()

    async def prepare_stream(
        self,
        file_id: str,
        chunk_size: int,
        *,
        expected_size: int | None = None,
    ) -> PreparedFileStream | None:
        normalized = normalize_file_id(file_id)
        if chunk_size <= 0:
            raise ValueError("chunk_size must be positive")
        async with self._lock:
            content = self._contents.get(normalized)
        if content is None or (
            expected_size is not None and len(content) != expected_size
        ):
            return None

        return _MemoryPreparedStream(content, chunk_size)

    async def delete(self, file_id: str) -> bool:
        normalized = normalize_file_id(file_id)
        async with self._lock:
            return self._contents.pop(normalized, None) is not None

    async def exists(
        self,
        file_id: str,
        expected_size: int | None = None,
        expected_sha256: str | None = None,
    ) -> bool:
        normalized = normalize_file_id(file_id)
        async with self._lock:
            content = self._contents.get(normalized)
        if content is None or (
            expected_size is not None and len(content) != expected_size
        ):
            return False
        return expected_sha256 is None or (
            hashlib.sha256(content).hexdigest() == expected_sha256
        )


__all__ = [
    "MemoryFileContentStore",
]

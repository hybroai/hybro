from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

from fastapi import Request
from fastapi.responses import JSONResponse

from common.dto import FileInfo


@runtime_checkable
class HealthCheck(Protocol):
    async def check(self, request: Request) -> JSONResponse: ...


@runtime_checkable
class PreparedFileStream(Protocol):
    def __aiter__(self) -> AsyncIterator[bytes]: ...
    async def __anext__(self) -> bytes: ...
    async def aclose(self) -> None: ...


@runtime_checkable
class FileStorage(Protocol):
    async def upload(
        self,
        file_bytes: bytes,
        filename: str,
        owner_id: str,
        room_id: str,
        content_type: str | None = None,
    ) -> FileInfo: ...
    async def get_url(self, file_id: str, ttl: int = 3600) -> str | None: ...
    async def delete(self, file_id: str) -> bool: ...
    async def list_for_room(self, room_id: str) -> list[FileInfo]: ...
    async def get_ready_file(
        self,
        file_id: str,
        *,
        owner_id: str | None = None,
    ) -> FileInfo | None: ...
    async def prepare_download(
        self,
        file_id: str,
        *,
        owner_id: str,
        chunk_size: int,
    ) -> tuple[FileInfo, PreparedFileStream] | None: ...
    def stream(self, file_id: str, chunk_size: int) -> AsyncIterator[bytes]: ...


@runtime_checkable
class AttachmentMetadataReader(Protocol):
    async def get_for_room_file(self, room_id: str, file_id: str) -> dict | None: ...


@runtime_checkable
class AttachmentContentReader(Protocol):
    async def get_bytes(self, file_id: str, *, max_bytes: int) -> bytes | None: ...


@runtime_checkable
class AttachmentCleanupPort(Protocol):
    async def delete_for_room(self, room_id: str) -> int: ...


__all__ = [
    "AttachmentCleanupPort",
    "AttachmentContentReader",
    "AttachmentMetadataReader",
    "FileStorage",
    "HealthCheck",
    "PreparedFileStream",
]

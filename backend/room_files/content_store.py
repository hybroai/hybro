from __future__ import annotations

import asyncio
import errno
import hashlib
import os
import stat
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Protocol, runtime_checkable
from uuid import UUID, uuid4

from common.protocols.platform_protocols import PreparedFileStream
from room_files.errors import (
    FileConflictError,
    FileOperationError,
    FileTooLargeError,
    FileUnavailableError,
)


@runtime_checkable
class FileContentStore(Protocol):
    async def write(
        self,
        file_id: str,
        content: bytes,
        content_type: str,
    ) -> None: ...

    async def read(self, file_id: str, max_bytes: int) -> bytes | None: ...

    def stream(self, file_id: str, chunk_size: int) -> AsyncIterator[bytes]: ...

    async def prepare_stream(
        self,
        file_id: str,
        chunk_size: int,
        *,
        expected_size: int | None = None,
    ) -> PreparedFileStream | None: ...

    async def delete(self, file_id: str) -> bool: ...

    async def exists(
        self,
        file_id: str,
        expected_size: int | None = None,
        expected_sha256: str | None = None,
    ) -> bool: ...


def normalize_file_id(file_id: str) -> str:
    try:
        parsed = UUID(file_id)
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError("file_id must be a UUID") from exc
    if file_id != parsed.hex:
        raise ValueError("file_id must be a lowercase UUID hex string")
    return parsed.hex


class _LocalPreparedStream:
    def __init__(self, handle, chunk_size: int) -> None:
        self._handle = handle
        self._chunk_size = chunk_size

    def __aiter__(self) -> _LocalPreparedStream:
        return self

    async def __anext__(self) -> bytes:
        handle = self._handle
        if handle is None:
            raise StopAsyncIteration
        try:
            chunk = await asyncio.to_thread(handle.read, self._chunk_size)
        except BaseException:
            await self.aclose()
            raise
        if not chunk:
            await self.aclose()
            raise StopAsyncIteration
        return chunk

    async def aclose(self) -> None:
        handle = self._handle
        if handle is None:
            return
        self._handle = None
        await asyncio.to_thread(handle.close)

    def __del__(self) -> None:
        handle = self._handle
        if handle is not None:
            handle.close()


class LocalFileContentStore:
    def __init__(self, root: str | Path) -> None:
        self._root = Path(root).expanduser().resolve()
        self._root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def _path(self, file_id: str) -> Path:
        normalized = normalize_file_id(file_id)
        return self._root / normalized[:2] / normalized

    async def write(
        self,
        file_id: str,
        content: bytes,
        content_type: str,
    ) -> None:
        del content_type
        path = self._path(file_id)
        await asyncio.to_thread(self._write_sync, path, bytes(content))

    def _write_sync(self, path: Path, content: bytes) -> None:
        self._ensure_shard_directory(path.parent)
        temp_path = path.parent / f".{path.name}.{uuid4().hex}.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_NOFOLLOW", 0)
        fd: int | None = None
        try:
            fd = os.open(temp_path, flags, 0o600)
            with os.fdopen(fd, "wb", closefd=True) as handle:
                fd = None
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(temp_path, path, follow_symlinks=False)
            except FileExistsError as exc:
                raise FileConflictError(path.name) from exc
            os.unlink(temp_path)
            self._fsync_directory(path.parent)
        except FileConflictError:
            raise
        except OSError as exc:
            if exc.errno == errno.EEXIST:
                raise FileConflictError(path.name) from exc
            raise FileOperationError(f"could not write {path.name}") from exc
        finally:
            if fd is not None:
                os.close(fd)
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass

    def _ensure_shard_directory(self, directory: Path) -> None:
        created_parent = False
        try:
            directory.mkdir(mode=0o700)
            created_parent = True
        except FileExistsError as exc:
            parent_info = directory.lstat()
            if not stat.S_ISDIR(parent_info.st_mode):
                raise FileUnavailableError(directory.name) from exc
        if directory.is_symlink():
            raise FileUnavailableError(directory.name)
        if created_parent:
            self._fsync_directory(self._root)

    async def read(self, file_id: str, max_bytes: int) -> bytes | None:
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        path = self._path(file_id)
        return await asyncio.to_thread(self._read_sync, path, max_bytes)

    def _read_sync(self, path: Path, max_bytes: int) -> bytes | None:
        try:
            fd = self._open_regular(path)
        except FileNotFoundError:
            return None
        try:
            with os.fdopen(fd, "rb", closefd=True) as handle:
                content = handle.read(max_bytes + 1)
        except OSError as exc:
            raise FileOperationError(f"could not read {path.name}") from exc
        if len(content) > max_bytes:
            raise FileTooLargeError(path.name)
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
        if chunk_size <= 0:
            raise ValueError("chunk_size must be positive")
        path = self._path(file_id)
        try:
            fd = await asyncio.to_thread(self._open_regular, path)
        except FileNotFoundError:
            return None
        if expected_size is not None and os.fstat(fd).st_size != expected_size:
            os.close(fd)
            return None
        return _LocalPreparedStream(os.fdopen(fd, "rb", closefd=True), chunk_size)

    async def delete(self, file_id: str) -> bool:
        path = self._path(file_id)
        return await asyncio.to_thread(self._delete_sync, path)

    async def exists(
        self,
        file_id: str,
        expected_size: int | None = None,
        expected_sha256: str | None = None,
    ) -> bool:
        path = self._path(file_id)
        return await asyncio.to_thread(
            self._exists_sync, path, expected_size, expected_sha256
        )

    def _exists_sync(
        self,
        path: Path,
        expected_size: int | None,
        expected_sha256: str | None,
    ) -> bool:
        try:
            fd = self._open_regular(path)
        except FileNotFoundError:
            return False
        try:
            size = os.fstat(fd).st_size
            if expected_size is not None and size != expected_size:
                return False
            if expected_sha256 is not None:
                digest = hashlib.sha256()
                while chunk := os.read(fd, 1024 * 1024):
                    digest.update(chunk)
                if digest.hexdigest() != expected_sha256:
                    return False
        finally:
            os.close(fd)
        return True

    async def list_file_ids(self) -> list[str]:
        return await asyncio.to_thread(self._list_file_ids_sync)

    def _list_file_ids_sync(self) -> list[str]:
        result: list[str] = []
        for shard in self._root.iterdir():
            try:
                if shard.is_symlink() or not stat.S_ISDIR(shard.stat().st_mode):
                    continue
            except FileNotFoundError:
                continue
            for path in shard.iterdir():
                if path.name.startswith("."):
                    continue
                try:
                    normalized = normalize_file_id(path.name)
                    if stat.S_ISREG(path.lstat().st_mode):
                        result.append(normalized)
                except (FileNotFoundError, ValueError):
                    continue
        return result

    async def cleanup_temporary_files(self) -> int:
        return await asyncio.to_thread(self._cleanup_temporary_files_sync)

    def _cleanup_temporary_files_sync(self) -> int:
        removed = 0
        for shard in self._root.iterdir():
            try:
                if shard.is_symlink() or not stat.S_ISDIR(shard.lstat().st_mode):
                    continue
            except FileNotFoundError:
                continue
            for path in shard.glob("*.tmp"):
                try:
                    if stat.S_ISREG(path.lstat().st_mode):
                        path.unlink()
                        removed += 1
                except FileNotFoundError:
                    continue
        return removed

    def _delete_sync(self, path: Path) -> bool:
        self._validate_shard(path.parent)
        try:
            info = path.lstat()
        except FileNotFoundError:
            return False
        if not stat.S_ISREG(info.st_mode):
            raise FileUnavailableError(path.name)
        try:
            path.unlink()
            self._fsync_directory(path.parent)
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise FileOperationError(f"could not delete {path.name}") from exc
        return True

    @staticmethod
    def _open_regular(path: Path) -> int:
        LocalFileContentStore._validate_shard(path.parent)
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(path, flags)
        except OSError as exc:
            if exc.errno in {errno.ELOOP, errno.EISDIR}:
                raise FileUnavailableError(path.name) from exc
            raise
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise FileUnavailableError(path.name)
        except BaseException:
            os.close(fd)
            raise
        return fd

    @staticmethod
    def _validate_shard(directory: Path) -> None:
        try:
            info = directory.lstat()
        except FileNotFoundError:
            raise
        if not stat.S_ISDIR(info.st_mode) or directory.is_symlink():
            raise FileUnavailableError(directory.name)

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

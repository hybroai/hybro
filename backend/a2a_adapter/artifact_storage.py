"""Materialize A2A file parts into durable room files."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import ipaddress
import json
import socket
from typing import Any
from urllib.parse import urljoin, urlparse

import aiohttp
from aiohttp.abc import AbstractResolver

from common.errors import FileStoragePlatformError, RetryableFileStoragePlatformError
from common.observability import events as E
from common.types import DataPart, FileContent, Part
from common.utils.artifact_delivery import (
    record_materialization_attempt,
    record_materialization_failure,
    record_materialization_success,
)
from common.utils.logger import get_logger

logger = get_logger(__name__)

MAX_FILE_PARTS = 20
MAX_FILE_RAW_BYTES = 50 * 1024 * 1024
MAX_TOTAL_RAW_BYTES = 100 * 1024 * 1024
MAX_TOTAL_ENCODED_BYTES = 139_810_136
MAX_REDIRECTS = 3
STORE_ATTEMPTS = 3
STORE_RETRY_DELAYS = (0.05, 0.15)

_room_files: Any | None = None


class ArtifactSizeLimitError(ValueError):
    """An artifact exceeded a platform-owned materialization limit."""


class InvalidBase64ArtifactError(ValueError):
    """An A2A FileWithBytes payload was not strict Base64."""


def bind_artifact_files(room_files: Any) -> None:
    global _room_files

    _room_files = room_files


def _require_room_files() -> Any:
    if _room_files is None:
        raise RuntimeError("Artifact room files dependency has not been bound")
    return _room_files


def _room_file_content_url(file_id: str) -> str:
    storage = _require_room_files()
    content_url = getattr(storage, "content_url", None)
    if callable(content_url):
        return str(content_url(file_id))
    return f"/api/v1/files/{file_id}/content"


def _declared_mime(file_content: Any) -> str | None:
    return getattr(file_content, "mime_type", None) or getattr(
        file_content, "mimeType", None
    )


def _mime(file_content: Any) -> str:
    return _declared_mime(file_content) or "application/octet-stream"


def _origin_key(
    *,
    conversation_id: str,
    message_id: str,
    artifact_slot: str,
    part_slot: int,
    content_sha256: str,
) -> str:
    payload = [
        "v1",
        conversation_id,
        message_id,
        artifact_slot,
        part_slot,
        content_sha256,
    ]
    canonical = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _decode_base64(value: str) -> bytes:
    if len(value) > 4 * ((MAX_FILE_RAW_BYTES + 2) // 3):
        raise ArtifactSizeLimitError("encoded file exceeds per-file limit")
    try:
        decoded = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise InvalidBase64ArtifactError("invalid base64 file content") from exc
    if len(decoded) > MAX_FILE_RAW_BYTES:
        raise ArtifactSizeLimitError("decoded file exceeds per-file limit")
    return decoded


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _decode_base64_with_digest(value: str) -> tuple[bytes, str]:
    data = _decode_base64(value)
    return data, _digest(data)


def _failure_code(exc: Exception, *, stage: str) -> str:
    if isinstance(exc, ArtifactSizeLimitError):
        return "size_limit"
    if isinstance(exc, InvalidBase64ArtifactError):
        return "invalid_base64"
    if stage == "store":
        return "storage_failed"
    if stage == "fetch":
        return "fetch_failed"
    if stage == "reference":
        return "invalid_reference"
    return "invalid_content"


def _public_failure_reason(code: str) -> str:
    return "size_limit" if code == "size_limit" else "invalid_content"


def _platform_failure_category(exc: Exception) -> str | None:
    if not isinstance(exc, FileStoragePlatformError):
        return None
    message = str(exc).lower()
    if "finaliz" in message:
        return "finalization_conflict"
    if "delet" in message or "unavailable" in message:
        return "room_unavailable"
    if "lease" in message:
        return "lease_conflict"
    if "origin" in message:
        return "origin_conflict"
    if "size" in message or exc.status_code == 413:
        return "size_limit"
    return "platform_conflict" if exc.status_code == 409 else "platform_error"


def _record_failure(
    report: dict[str, Any] | None,
    *,
    conversation_id: str,
    message_id: str,
    artifact_slot: str,
    part_slot: int,
    source: str,
    code: str,
    exc: Exception,
) -> None:
    artifact_ref = hashlib.sha256(artifact_slot.encode()).hexdigest()[:16]
    record_materialization_failure(
        report,
        code=code,
        artifact_ref=artifact_ref,
        part_slot=part_slot,
        source=source,
        exception_type=type(exc).__name__,
    )
    logger.emit(
        E.ARTIFACT_MATERIALIZATION_FAILED,
        exc=exc,
        conversation_id=conversation_id,
        message_id=message_id,
        artifact_ref=artifact_ref,
        part_slot=part_slot,
        content_source=source,
        failure_code=code,
        platform_status=getattr(exc, "status_code", None),
        platform_failure_category=_platform_failure_category(exc),
    )


def _unavailable_dict(file_info: dict[str, Any], reason: str) -> dict[str, Any]:
    return {
        "kind": "data",
        "data": {
            "type": "file_unavailable",
            "file_name": file_info.get("name") or "file",
            "mime_type": (
                file_info.get("mime_type")
                or file_info.get("mimeType")
                or "application/octet-stream"
            ),
            "reason": reason,
        },
    }


def _unavailable_part(file_content: Any, reason: str) -> Part:
    return Part(
        root=DataPart(
            data={
                "type": "file_unavailable",
                "file_name": getattr(file_content, "name", None) or "file",
                "mime_type": _mime(file_content),
                "reason": reason,
            }
        )
    )


def _authoritative_size(record: dict[str, Any]) -> int:
    try:
        size = int(record["size_bytes"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("invalid stored file size") from exc
    if size < 0:
        raise ValueError("invalid stored file size")
    return size


def _declared_metadata_size(metadata: dict[str, Any]) -> int:
    try:
        size = int(metadata.get("size_bytes") or 0)
    except (TypeError, ValueError):
        return 0
    return max(0, size)


def _canonical_reference_metadata(record: dict[str, Any]) -> dict[str, Any]:
    file_id = record.get("file_id")
    sha256 = record.get("sha256")
    if not isinstance(file_id, str) or not file_id:
        raise ValueError("invalid stored file id")
    if not isinstance(sha256, str) or not sha256:
        raise ValueError("invalid stored file digest")
    return {
        "file_id": file_id,
        "file_name": str(record.get("file_name") or "file"),
        "mime_type": str(record.get("mime_type") or "application/octet-stream"),
        "size_bytes": _authoritative_size(record),
        "sha256": sha256,
    }


def _canonicalize_reference_dict(
    part: dict[str, Any],
    file_info: dict[str, Any],
    record: dict[str, Any],
) -> None:
    metadata = _canonical_reference_metadata(record)
    file_info.clear()
    file_info.update(
        {
            "uri": _room_file_content_url(metadata["file_id"]),
            "mimeType": metadata["mime_type"],
            "name": metadata["file_name"],
        }
    )
    part["metadata"] = metadata


def _claim_file_attempt(budget: dict[str, Any]) -> bool:
    attempted = int(budget.get("attempted", budget.get("converted", 0)))
    if attempted >= MAX_FILE_PARTS:
        return False
    budget["attempted"] = attempted + 1
    return True


async def _store(
    *,
    data: bytes,
    conversation_id: str,
    message_id: str,
    artifact_slot: str,
    part_slot: int,
    file_name: str,
    mime_type: str,
    content_sha256: str,
) -> dict[str, Any]:
    return await _require_room_files().store_agent_artifact(
        conversation_id=conversation_id,
        source_message_id=message_id,
        origin_key=_origin_key(
            conversation_id=conversation_id,
            message_id=message_id,
            artifact_slot=artifact_slot,
            part_slot=part_slot,
            content_sha256=content_sha256,
        ),
        content=data,
        content_sha256=content_sha256,
        file_name=file_name,
        mime_type=mime_type,
        max_bytes=MAX_FILE_RAW_BYTES,
    )


def _is_retryable_store_error(exc: Exception) -> bool:
    if isinstance(exc, RetryableFileStoragePlatformError):
        return True
    if not isinstance(exc, FileStoragePlatformError):
        return False
    status_code = exc.status_code
    if status_code == 409:
        return _platform_failure_category(exc) in {
            "finalization_conflict",
            "lease_conflict",
        }
    return status_code == 429 or status_code >= 500


async def _store_with_retry(**kwargs: Any) -> dict[str, Any]:
    for attempt in range(1, STORE_ATTEMPTS + 1):
        try:
            return await _store(**kwargs)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if attempt >= STORE_ATTEMPTS or not _is_retryable_store_error(exc):
                raise
            logger.emit(
                E.ARTIFACT_MATERIALIZATION_STORE_RETRIED,
                exc=exc,
                conversation_id=kwargs.get("conversation_id"),
                message_id=kwargs.get("message_id"),
                attempt=attempt,
                max_attempts=STORE_ATTEMPTS,
                platform_status=getattr(exc, "status_code", None),
                platform_failure_category=_platform_failure_category(exc),
            )
            await asyncio.sleep(STORE_RETRY_DELAYS[attempt - 1])
    raise RuntimeError("artifact store retry loop exited unexpectedly")


async def _consume_precounted_reference(
    *,
    part: dict[str, Any],
    file_info: dict[str, Any],
    conversation_id: str,
    message_id: str,
    budget: dict[str, Any],
) -> bool:
    metadata = part.get("metadata") or {}
    file_id = str(metadata.get("file_id") or "")
    precounted = budget.get("precounted_file_ids")
    if not isinstance(precounted, dict):
        return False
    remaining = int(precounted.get(file_id) or 0)
    if remaining <= 0:
        return False
    uri = file_info.get("uri")
    if uri != _room_file_content_url(file_id):
        return False
    if remaining == 1:
        precounted.pop(file_id, None)
    else:
        precounted[file_id] = remaining - 1
    valid = await _require_room_files().validate_agent_reference(
        conversation_id=conversation_id,
        source_message_id=message_id,
        file_id=file_id,
        sha256=metadata.get("sha256"),
    )
    if not valid:
        raise ValueError("untrusted internal file reference")
    authoritative_size = _authoritative_size(valid)
    budget["raw"] = (
        max(0, int(budget["raw"]) - _declared_metadata_size(metadata))
        + authoritative_size
    )
    if budget["raw"] > MAX_TOTAL_RAW_BYTES:
        raise ArtifactSizeLimitError("aggregate raw limit exceeded")
    _canonicalize_reference_dict(part, file_info, valid)
    return True


async def materialize_inline_file_parts(
    parts: list[dict],
    conversation_id: str,
    message_id: str,
    *,
    converted_so_far: int = 0,
    budget: dict[str, Any] | None = None,
    artifact_slot: str | None = None,
    report: dict[str, Any] | None = None,
) -> int:
    budget = (
        budget
        if budget is not None
        else {
            "converted": converted_so_far,
            "attempted": converted_so_far,
            "raw": 0,
            "encoded": 0,
        }
    )
    budget.setdefault("attempted", int(budget.get("converted", 0)))
    resolved_slot = artifact_slot or "message"

    for part_slot, part in enumerate(parts):
        if part.get("kind") != "file":
            continue
        record_materialization_attempt(report)
        file_info = part.get("file")
        if isinstance(file_info, dict):
            try:
                if await _consume_precounted_reference(
                    part=part,
                    file_info=file_info,
                    conversation_id=conversation_id,
                    message_id=message_id,
                    budget=budget,
                ):
                    record_materialization_success(report)
                    continue
            except Exception as exc:
                code = _failure_code(exc, stage="reference")
                _record_failure(
                    report,
                    conversation_id=conversation_id,
                    message_id=message_id,
                    artifact_slot=resolved_slot,
                    part_slot=part_slot,
                    source="reference",
                    code=code,
                    exc=exc,
                )
                parts[part_slot] = _unavailable_dict(
                    file_info, _public_failure_reason(code)
                )
                continue
        if not _claim_file_attempt(budget):
            exc = ArtifactSizeLimitError("file part count limit exceeded")
            _record_failure(
                report,
                conversation_id=conversation_id,
                message_id=message_id,
                artifact_slot=resolved_slot,
                part_slot=part_slot,
                source="unknown",
                code="size_limit",
                exc=exc,
            )
            parts[part_slot] = _unavailable_dict(
                file_info if isinstance(file_info, dict) else {},
                "size_limit",
            )
            continue
        if not isinstance(file_info, dict):
            exc = ValueError("missing file descriptor")
            _record_failure(
                report,
                conversation_id=conversation_id,
                message_id=message_id,
                artifact_slot=resolved_slot,
                part_slot=part_slot,
                source="unknown",
                code="invalid_content",
                exc=exc,
            )
            parts[part_slot] = _unavailable_dict({}, "invalid_content")
            continue
        stage = "content"
        source = "unknown"
        try:
            encoded = file_info.get("bytes")
            if isinstance(encoded, str) and encoded:
                source = "bytes"
                stage = "decode"
                budget["encoded"] += len(encoded)
                if budget["encoded"] > MAX_TOTAL_ENCODED_BYTES:
                    raise ArtifactSizeLimitError("aggregate encoded limit exceeded")
                data, content_sha256 = await asyncio.to_thread(
                    _decode_base64_with_digest, encoded
                )
            else:
                source = "uri"
                stage = "fetch"
                uri = file_info.get("uri")
                if not isinstance(uri, str) or not uri:
                    raise ValueError("missing file content")
                room_files = _require_room_files()
                if uri == _room_file_content_url(
                    str((part.get("metadata") or {}).get("file_id") or "")
                ):
                    stage = "reference"
                    metadata = part.get("metadata") or {}
                    file_id = str(metadata.get("file_id") or "")
                    valid = await room_files.validate_agent_reference(
                        conversation_id=conversation_id,
                        source_message_id=message_id,
                        file_id=file_id,
                        sha256=metadata.get("sha256"),
                    )
                    if valid:
                        authoritative_size = _authoritative_size(valid)
                        budget["converted"] += 1
                        budget["raw"] += authoritative_size
                        if budget["raw"] > MAX_TOTAL_RAW_BYTES:
                            raise ArtifactSizeLimitError("aggregate raw limit exceeded")
                        _canonicalize_reference_dict(part, file_info, valid)
                        record_materialization_success(report)
                        continue
                    raise ValueError("untrusted internal file reference")
                data, detected_mime = await fetch_remote_file(uri)
                content_sha256 = await asyncio.to_thread(_digest, data)
                file_info.setdefault("mimeType", detected_mime)
            budget["raw"] += len(data)
            if budget["raw"] > MAX_TOTAL_RAW_BYTES:
                raise ArtifactSizeLimitError("aggregate raw limit exceeded")
            stage = "store"
            stored = await _store_with_retry(
                data=data,
                conversation_id=conversation_id,
                message_id=message_id,
                artifact_slot=artifact_slot or f"message:{budget['converted']}",
                part_slot=part_slot,
                file_name=file_info.get("name") or f"artifact-{budget['converted']}",
                mime_type=(
                    file_info.get("mime_type")
                    or file_info.get("mimeType")
                    or "application/octet-stream"
                ),
                content_sha256=content_sha256,
            )
            stored_metadata = _canonical_reference_metadata(stored)
            file_info.clear()
            file_info.update(
                {
                    "uri": _room_file_content_url(stored_metadata["file_id"]),
                    "mimeType": stored_metadata["mime_type"],
                    "name": stored_metadata["file_name"],
                }
            )
            part["metadata"] = stored_metadata
        except Exception as exc:
            code = _failure_code(exc, stage=stage)
            _record_failure(
                report,
                conversation_id=conversation_id,
                message_id=message_id,
                artifact_slot=resolved_slot,
                part_slot=part_slot,
                source=source,
                code=code,
                exc=exc,
            )
            parts[part_slot] = _unavailable_dict(
                file_info, _public_failure_reason(code)
            )
            continue

        record_materialization_success(report)
        budget["converted"] += 1
    return budget["converted"]


async def delete_superseded_agent_artifacts(
    *,
    conversation_id: str,
    message_id: str,
    file_ids: set[str],
) -> int:
    return await _require_room_files().delete_superseded_agent_artifacts(
        conversation_id=conversation_id,
        source_message_id=message_id,
        file_ids=file_ids,
    )


async def materialize_artifacts(
    artifacts: list,
    conversation_id: str,
    message_id: str,
    *,
    converted_so_far: int = 0,
    report: dict[str, Any] | None = None,
) -> int:
    converted = converted_so_far
    attempted = converted_so_far
    total_raw = 0
    total_encoded = 0

    for artifact_position, artifact in enumerate(artifacts):
        artifact_id = getattr(artifact, "artifact_id", None)
        explicit_index = getattr(artifact, "index", None)
        artifact_slot = (
            f"id:{artifact_id}"
            if artifact_id
            else f"index:{explicit_index}"
            if explicit_index is not None
            else f"slot:{artifact_position}"
        )
        for part_slot, part in enumerate(list(getattr(artifact, "parts", []) or [])):
            root = getattr(part, "root", part)
            if getattr(root, "kind", None) != "file":
                continue
            record_materialization_attempt(report)
            file_content = getattr(root, "file", None)
            if attempted >= MAX_FILE_PARTS:
                exc = ArtifactSizeLimitError("file part count limit exceeded")
                _record_failure(
                    report,
                    conversation_id=conversation_id,
                    message_id=message_id,
                    artifact_slot=artifact_slot,
                    part_slot=part_slot,
                    source="unknown",
                    code="size_limit",
                    exc=exc,
                )
                artifact.parts[part_slot] = _unavailable_part(
                    file_content, "size_limit"
                )
                continue
            attempted += 1
            if file_content is None:
                exc = ValueError("missing file descriptor")
                _record_failure(
                    report,
                    conversation_id=conversation_id,
                    message_id=message_id,
                    artifact_slot=artifact_slot,
                    part_slot=part_slot,
                    source="unknown",
                    code="invalid_content",
                    exc=exc,
                )
                artifact.parts[part_slot] = _unavailable_part(
                    file_content, "invalid_content"
                )
                continue
            stage = "content"
            source = "unknown"
            try:
                encoded = getattr(file_content, "bytes", None)
                if encoded:
                    source = "bytes"
                    stage = "decode"
                    total_encoded += len(encoded)
                    if total_encoded > MAX_TOTAL_ENCODED_BYTES:
                        raise ArtifactSizeLimitError("aggregate encoded limit exceeded")
                    data, content_sha256 = await asyncio.to_thread(
                        _decode_base64_with_digest, encoded
                    )
                else:
                    source = "uri"
                    stage = "fetch"
                    uri = getattr(file_content, "uri", None)
                    if not uri:
                        raise ValueError("missing file content")
                    room_files = _require_room_files()
                    metadata = getattr(root, "metadata", None) or {}
                    file_id = str(metadata.get("file_id") or "")
                    if str(uri) == _room_file_content_url(file_id):
                        stage = "reference"
                        valid = await room_files.validate_agent_reference(
                            conversation_id=conversation_id,
                            source_message_id=message_id,
                            file_id=file_id,
                            sha256=metadata.get("sha256"),
                        )
                        if valid:
                            total_raw += _authoritative_size(valid)
                            if total_raw > MAX_TOTAL_RAW_BYTES:
                                raise ArtifactSizeLimitError(
                                    "aggregate raw limit exceeded"
                                )
                            canonical = _canonical_reference_metadata(valid)
                            root.file = FileContent(
                                uri=_room_file_content_url(canonical["file_id"]),
                                mimeType=canonical["mime_type"],
                                name=canonical["file_name"],
                            )
                            root.metadata = canonical
                            converted += 1
                            record_materialization_success(report)
                            continue
                        raise ValueError("untrusted internal file reference")
                    data, detected_mime = await fetch_remote_file(str(uri))
                    content_sha256 = await asyncio.to_thread(_digest, data)
                    if not _declared_mime(file_content):
                        file_content.mime_type = detected_mime
                total_raw += len(data)
                if total_raw > MAX_TOTAL_RAW_BYTES:
                    raise ArtifactSizeLimitError("aggregate raw limit exceeded")
                stage = "store"
                stored = await _store_with_retry(
                    data=data,
                    conversation_id=conversation_id,
                    message_id=message_id,
                    artifact_slot=artifact_slot,
                    part_slot=part_slot,
                    file_name=getattr(file_content, "name", None)
                    or f"artifact-{artifact_position}-{part_slot}",
                    mime_type=_mime(file_content),
                    content_sha256=content_sha256,
                )
                stored_metadata = _canonical_reference_metadata(stored)
                root.file = FileContent(
                    uri=_room_file_content_url(stored_metadata["file_id"]),
                    mimeType=stored_metadata["mime_type"],
                    name=stored_metadata["file_name"],
                )
                root.metadata = stored_metadata
            except Exception as exc:
                code = _failure_code(exc, stage=stage)
                _record_failure(
                    report,
                    conversation_id=conversation_id,
                    message_id=message_id,
                    artifact_slot=artifact_slot,
                    part_slot=part_slot,
                    source=source,
                    code=code,
                    exc=exc,
                )
                artifact.parts[part_slot] = _unavailable_part(
                    file_content, _public_failure_reason(code)
                )
                continue

            record_materialization_success(report)
            converted += 1
    return converted


class _PinnedResolver(AbstractResolver):
    def __init__(self, hostname: str, addresses: list[tuple[int, str]]) -> None:
        self._hostname = hostname
        self._addresses = addresses

    async def resolve(
        self,
        host: str,
        port: int = 0,
        family: socket.AddressFamily = socket.AF_INET,
    ) -> list[dict[str, Any]]:
        if host != self._hostname:
            raise OSError("unexpected hostname")
        return [
            {
                "hostname": host,
                "host": address,
                "port": port,
                "family": address_family,
                "proto": 0,
                "flags": socket.AI_NUMERICHOST,
            }
            for address_family, address in self._addresses
        ]

    async def close(self) -> None:
        return None


def _public_address(value: str) -> bool:
    return ipaddress.ip_address(value).is_global


async def _resolve_public(hostname: str, port: int) -> list[tuple[int, str]]:
    infos = await asyncio.get_running_loop().getaddrinfo(
        hostname,
        port,
        type=socket.SOCK_STREAM,
    )
    addresses: list[tuple[int, str]] = []
    for family, _type, _proto, _canonname, sockaddr in infos:
        address = sockaddr[0]
        if not _public_address(address):
            raise ValueError("remote URI resolves to a non-public address")
        pair = (family, address)
        if pair not in addresses:
            addresses.append(pair)
    if not addresses:
        raise ValueError("remote URI has no usable address")
    return addresses


async def fetch_remote_file(uri: str) -> tuple[bytes, str]:
    current = uri
    timeout = aiohttp.ClientTimeout(total=60, connect=5, sock_read=30)
    for redirect_count in range(MAX_REDIRECTS + 1):
        parsed = urlparse(current)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
        ):
            raise ValueError("unsupported remote URI")
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        addresses = await _resolve_public(parsed.hostname, port)
        connector = aiohttp.TCPConnector(
            resolver=_PinnedResolver(parsed.hostname, addresses),
            use_dns_cache=False,
        )
        async with aiohttp.ClientSession(
            connector=connector,
            timeout=timeout,
            trust_env=False,
        ) as session:
            async with session.get(current, allow_redirects=False) as response:
                connection = response.connection
                transport = connection.transport if connection is not None else None
                peer = transport.get_extra_info("peername") if transport else None
                peer_address = peer[0] if isinstance(peer, tuple) and peer else None
                if (
                    not isinstance(peer_address, str)
                    or not _public_address(peer_address)
                    or peer_address not in {address for _, address in addresses}
                ):
                    raise ValueError("remote URI peer did not match pinned DNS")
                if response.status in {301, 302, 303, 307, 308}:
                    if redirect_count >= MAX_REDIRECTS:
                        raise ValueError("too many redirects")
                    location = response.headers.get("location")
                    if not location:
                        raise ValueError("redirect missing location")
                    current = urljoin(current, location)
                    continue
                if response.status != 200:
                    raise ValueError(f"remote URI returned {response.status}")
                declared = response.content_length
                if declared is not None and declared > MAX_FILE_RAW_BYTES:
                    raise ArtifactSizeLimitError("remote file exceeds size limit")
                chunks: list[bytes] = []
                total = 0
                async for chunk in response.content.iter_chunked(64 * 1024):
                    total += len(chunk)
                    if total > MAX_FILE_RAW_BYTES:
                        raise ArtifactSizeLimitError("remote file exceeds size limit")
                    chunks.append(chunk)
                mime_type = (
                    response.headers.get("content-type", "application/octet-stream")
                    .split(";", 1)[0]
                    .strip()
                    or "application/octet-stream"
                )
                return b"".join(chunks), mime_type
    raise ValueError("remote URI could not be fetched")


__all__ = [
    "bind_artifact_files",
    "delete_superseded_agent_artifacts",
    "fetch_remote_file",
    "materialize_artifacts",
    "materialize_inline_file_parts",
]

import socket
from unittest.mock import AsyncMock, MagicMock

import pytest

from a2a_adapter.artifact_storage import (
    MAX_FILE_PARTS,
    MAX_TOTAL_RAW_BYTES,
    bind_artifact_files,
    fetch_remote_file,
    materialize_artifacts,
    materialize_inline_file_parts,
)
from common.types import Artifact, FileContent, FilePart, Part


async def test_remote_file_allows_public_uri_with_internal_looking_path(monkeypatch):
    resolver = AsyncMock(side_effect=RuntimeError("dns reached"))
    monkeypatch.setattr("a2a_adapter.artifact_storage._resolve_public", resolver)

    with pytest.raises(RuntimeError, match="dns reached"):
        await fetch_remote_file("https://files.example.com/api/v1/files/a/content")
    resolver.assert_awaited_once_with("files.example.com", 443)


async def test_remote_file_still_rejects_relative_internal_content_endpoint(
    monkeypatch,
):
    resolver = AsyncMock()
    monkeypatch.setattr("a2a_adapter.artifact_storage._resolve_public", resolver)

    with pytest.raises(ValueError, match="unsupported remote URI"):
        await fetch_remote_file("/api/v1/files/a/content")
    resolver.assert_not_awaited()


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.0.0.1",
        "100.64.0.1",
        "169.254.1.1",
        "::1",
        "fc00::1",
        "fe80::1",
    ],
)
async def test_remote_file_rejects_non_public_dns_results(monkeypatch, address):
    family = socket.AF_INET6 if ":" in address else socket.AF_INET

    async def getaddrinfo(*args, **kwargs):
        del args, kwargs
        sockaddr = (address, 443, 0, 0) if family == socket.AF_INET6 else (address, 443)
        return [(family, socket.SOCK_STREAM, 0, "", sockaddr)]

    monkeypatch.setattr("a2a_adapter.address_policy._getaddrinfo", getaddrinfo)

    with pytest.raises(ValueError, match="non-public"):
        await fetch_remote_file("https://example.com/file")


async def test_invalid_precounted_references_still_consume_file_part_limit():
    storage = MagicMock()
    storage.content_url.return_value = "/api/v1/files/file-1/content"
    storage.validate_agent_reference = AsyncMock(return_value=None)
    bind_artifact_files(storage)
    parts = [
        {
            "kind": "file",
            "file": {
                "uri": "/api/v1/files/file-1/content",
                "name": f"forged-{index}.bin",
            },
            "metadata": {
                "file_id": "file-1",
                "size_bytes": 1,
                "sha256": "forged-sha",
            },
        }
        for index in range(MAX_FILE_PARTS + 1)
    ]
    budget = {
        "converted": 1,
        "attempted": 1,
        "raw": 1,
        "encoded": 0,
        "precounted_file_ids": {"file-1": 1},
    }

    await materialize_inline_file_parts(
        parts,
        "room-1",
        "message-1",
        budget=budget,
    )

    assert storage.validate_agent_reference.await_count == MAX_FILE_PARTS
    assert parts[-1]["kind"] == "data"
    assert parts[-1]["data"]["reason"] == "size_limit"
    assert budget["attempted"] == MAX_FILE_PARTS
    assert budget["precounted_file_ids"] == {}


async def test_internal_reference_uses_authoritative_size_for_limit():
    storage = MagicMock()
    storage.content_url.return_value = "/api/v1/files/file-1/content"
    storage.validate_agent_reference = AsyncMock(
        return_value={
            "file_id": "file-1",
            "file_name": "trusted.bin",
            "mime_type": "application/octet-stream",
            "size_bytes": 2,
            "sha256": "trusted-sha",
            "status": "ready",
        }
    )
    bind_artifact_files(storage)
    parts = [
        {
            "kind": "file",
            "file": {
                "uri": "/api/v1/files/file-1/content",
                "name": "forged.bin",
                "mimeType": "text/plain",
            },
            "metadata": {
                "file_id": "file-1",
                "size_bytes": -100,
                "sha256": "trusted-sha",
            },
        }
    ]

    await materialize_inline_file_parts(
        parts,
        "room-1",
        "message-1",
        budget={
            "converted": 0,
            "raw": MAX_TOTAL_RAW_BYTES - 1,
            "encoded": 0,
        },
    )

    assert parts[0]["kind"] == "data"
    assert parts[0]["data"]["reason"] == "size_limit"


async def test_inline_decode_and_digest_are_offloaded(monkeypatch):
    storage = MagicMock()
    storage.content_url.return_value = "/api/v1/files/file-1/content"
    storage.store_agent_artifact = AsyncMock(
        return_value={
            "file_id": "file-1",
            "file_name": "file.bin",
            "mime_type": "application/octet-stream",
            "size_bytes": 3,
            "sha256": "11507a0e2f5e69d5dfa40a62a1bd7b6ee57e6bcd85c67c9b8431b36fff21c437",
        }
    )
    bind_artifact_files(storage)
    offloaded = []

    async def to_thread(function, *args):
        offloaded.append(function.__name__)
        return function(*args)

    monkeypatch.setattr(
        "a2a_adapter.artifact_storage.asyncio.to_thread",
        to_thread,
    )
    parts = [
        {
            "kind": "file",
            "file": {
                "bytes": "bmV3",
                "name": "file.bin",
                "mimeType": "application/octet-stream",
            },
        }
    ]

    await materialize_inline_file_parts(parts, "room-1", "message-1")

    assert "_decode_base64_with_digest" in offloaded
    assert storage.store_agent_artifact.await_args.kwargs["content_sha256"] == (
        "11507a0e2f5e69d5dfa40a62a1bd7b6ee57e6bcd85c67c9b8431b36fff21c437"
    )


async def test_internal_reference_canonicalizes_authoritative_metadata():
    storage = MagicMock()
    storage.content_url.return_value = "/api/v1/files/file-1/content"
    storage.validate_agent_reference = AsyncMock(
        return_value={
            "file_id": "file-1",
            "file_name": "trusted.bin",
            "mime_type": "application/octet-stream",
            "size_bytes": 4,
            "sha256": "trusted-sha",
            "status": "ready",
        }
    )
    bind_artifact_files(storage)
    parts = [
        {
            "kind": "file",
            "file": {
                "uri": "/api/v1/files/file-1/content",
                "name": "forged.bin",
                "mimeType": "text/plain",
            },
            "metadata": {
                "file_id": "file-1",
                "size_bytes": 0,
                "sha256": "trusted-sha",
            },
        }
    ]
    budget = {"converted": 0, "raw": 0, "encoded": 0}

    await materialize_inline_file_parts(
        parts,
        "room-1",
        "message-1",
        budget=budget,
    )

    assert parts == [
        {
            "kind": "file",
            "file": {
                "uri": "/api/v1/files/file-1/content",
                "name": "trusted.bin",
                "mimeType": "application/octet-stream",
            },
            "metadata": {
                "file_id": "file-1",
                "file_name": "trusted.bin",
                "mime_type": "application/octet-stream",
                "size_bytes": 4,
                "sha256": "trusted-sha",
            },
        }
    ]
    assert budget["converted"] == 1
    assert budget["raw"] == 4


async def test_failed_inline_file_attempts_still_consume_file_part_limit(monkeypatch):
    storage = MagicMock()
    storage.content_url.side_effect = lambda file_id: f"/api/v1/files/{file_id}/content"
    bind_artifact_files(storage)
    remote_fetch = AsyncMock(side_effect=ValueError("download failed"))
    monkeypatch.setattr(
        "a2a_adapter.artifact_storage.fetch_remote_file",
        remote_fetch,
    )
    parts = [
        {
            "kind": "file",
            "file": {
                "uri": f"https://files.example.com/{index}.bin",
                "name": f"{index}.bin",
            },
        }
        for index in range(MAX_FILE_PARTS + 1)
    ]
    budget = {"converted": 0, "raw": 0, "encoded": 0}

    await materialize_inline_file_parts(
        parts,
        "room-1",
        "message-1",
        budget=budget,
    )

    assert remote_fetch.await_count == MAX_FILE_PARTS
    assert parts[-1]["kind"] == "data"
    assert parts[-1]["data"]["reason"] == "size_limit"
    assert budget["attempted"] == MAX_FILE_PARTS


async def test_failed_sdk_file_attempts_still_consume_file_part_limit(monkeypatch):
    storage = MagicMock()
    storage.content_url.side_effect = lambda file_id: f"/api/v1/files/{file_id}/content"
    bind_artifact_files(storage)
    remote_fetch = AsyncMock(side_effect=ValueError("download failed"))
    monkeypatch.setattr(
        "a2a_adapter.artifact_storage.fetch_remote_file",
        remote_fetch,
    )
    artifact = Artifact(
        artifactId="artifact-1",
        parts=[
            Part(
                root=FilePart(
                    file=FileContent(
                        uri=f"https://files.example.com/{index}.bin",
                        name=f"{index}.bin",
                    )
                )
            )
            for index in range(MAX_FILE_PARTS + 1)
        ],
    )

    await materialize_artifacts([artifact], "room-1", "message-1")

    assert remote_fetch.await_count == MAX_FILE_PARTS
    assert artifact.parts[-1].root.kind == "data"
    assert artifact.parts[-1].root.data["reason"] == "size_limit"

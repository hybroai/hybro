from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
from hashlib import sha256

import pytest

from common.utils.a2a_artifacts import canonical_data_part_bytes
from execution.adapters.resources import RoomFilesResourceMaterializer
from execution.orchestrator.a2a_runtime.models import (
    A2AObservationInboxRecord,
    FrozenCallResourceManifest,
    FrozenCallResourceRef,
    InlineDataArtifact,
    NormalizedA2AObservation,
)
from execution.orchestrator.a2a_runtime.resources import (
    BoundedResourceMaterializer,
    ResourceSelectionError,
)
from execution.orchestrator.models import DataPart
from tests.orchestrator_harness.a2a_in_memory import InMemoryObservationInboxStore


def _future_deadline() -> datetime:
    return datetime.now(UTC) + timedelta(minutes=1)


class FakeRoomFiles:
    def __init__(self, files, *, conversation_id="room-1"):
        self.files = files
        self.conversation_id = conversation_id

    async def get_bytes(self, file_id, *, max_bytes):
        return self.files.get(file_id)

    async def get_for_room_file(self, conversation_id, file_id):
        if file_id not in self.files or conversation_id != self.conversation_id:
            return None
        return {"room_id": conversation_id, "file_id": file_id}


class FakeWriter:
    def __init__(self):
        self.calls = []

    async def __call__(self, call, artifact_ref, observation_id):
        self.calls.append((call, artifact_ref, observation_id))
        return f"durable:{artifact_ref}"


def _ref(ref_id, kind, content_digest=None, mime_type=None):
    return FrozenCallResourceRef(
        ref_id=ref_id,
        kind=kind,
        conversation_id="room-1",
        room_epoch=1,
        source_message_id="message-1",
        mime_type=mime_type,
        size_bytes=0,
        content_digest=content_digest or "",
    )


async def test_context_ref_materializes_to_text_part():
    content = b"hello world"
    digest = sha256(content).hexdigest()
    ref = _ref(
        "ctx:message:message-1",
        "context",
        content_digest=digest,
        mime_type="text/plain",
    )
    manifest = FrozenCallResourceManifest(
        manifest_id="m", refs=[ref], content_digest="manifest-digest"
    )

    async def context_text_reader(message_id):
        assert message_id == "message-1"
        return "hello world"

    adapter = RoomFilesResourceMaterializer(
        room_files=FakeRoomFiles({}),
        artifact_writer=FakeWriter(),
        context_text_reader=context_text_reader,
    )
    parts = await adapter.materialize(
        manifest,
        conversation_id="room-1",
        room_epoch=1,
        allowed_input_modes=["text"],
        deadline_at=_future_deadline(),
    )
    assert len(parts) == 1
    assert parts[0].kind == "text"
    assert parts[0].payload == "hello world"
    assert parts[0].content_digest == digest


async def test_attachment_ref_materializes_to_file_part():
    content = b"file-bytes"
    digest = sha256(content).hexdigest()
    ref = _ref(
        "file-1", "attachment", content_digest=digest, mime_type="application/pdf"
    )
    manifest = FrozenCallResourceManifest(
        manifest_id="m", refs=[ref], content_digest="manifest-digest"
    )
    adapter = RoomFilesResourceMaterializer(
        room_files=FakeRoomFiles({"file-1": content}),
        artifact_writer=FakeWriter(),
    )
    parts = await adapter.materialize(
        manifest,
        conversation_id="room-1",
        room_epoch=1,
        allowed_input_modes=["application/pdf"],
        deadline_at=_future_deadline(),
    )
    assert parts[0].kind == "file"
    assert parts[0].payload["bytes"] == base64.b64encode(content).decode("ascii")
    assert parts[0].mime_type == "application/pdf"


async def test_changed_content_is_rejected():
    ref = _ref("file-1", "attachment", content_digest="expected-digest")
    manifest = FrozenCallResourceManifest(
        manifest_id="m", refs=[ref], content_digest="manifest-digest"
    )
    adapter = RoomFilesResourceMaterializer(
        room_files=FakeRoomFiles({"file-1": b"actual"}),
        artifact_writer=FakeWriter(),
    )
    with pytest.raises(ResourceSelectionError, match="changed"):
        await adapter.materialize(
            manifest,
            conversation_id="room-1",
            room_epoch=1,
            allowed_input_modes=["file"],
            deadline_at=_future_deadline(),
        )


async def test_inline_data_artifact_is_described_and_materialized_from_observation():
    data = {"value": 42, "status": "ready"}
    metadata = {"mime_type": "application/vnd.hybro.result+json", "schema": "v1"}
    raw = canonical_data_part_bytes(
        data, mime_type="application/vnd.hybro.result+json", metadata=metadata
    )
    digest = sha256(raw).hexdigest()
    descriptor = InlineDataArtifact(
        ref_id="art_inline",
        artifact_id="artifact-1",
        artifact_name="result",
        artifact_index=0,
        part_index=0,
        content_index=0,
        mime_type="application/vnd.hybro.result+json",
        size_bytes=len(raw),
        content_digest=digest,
    )
    observation = NormalizedA2AObservation(
        observation_id="observation-1",
        call_record_id="call-1",
        source_kind="direct",
        source_identity="direct:scope:task:terminal",
        binding_scope="scope",
        event_kind="terminal",
        observed_at=datetime.now(UTC),
        task_id="task-1",
        status="completed",
        content=[
            DataPart(
                data=data,
                mime_type="application/vnd.hybro.result+json",
                metadata=metadata,
            )
        ],
        inline_artifacts=[descriptor],
        artifact_refs=[descriptor.ref_id],
    )
    inbox = InMemoryObservationInboxStore()
    await inbox.insert(
        A2AObservationInboxRecord(
            observation_id=observation.observation_id,
            source_kind="direct",
            source_identity=observation.source_identity,
            payload_digest="payload-digest",
            received_at=datetime.now(UTC),
            binding_scope="scope",
            conversation_id="room-1",
            room_epoch=1,
            call_record_id="call-1",
            task_id="task-1",
            event_kind="terminal",
            observation=observation,
        )
    )
    adapter = RoomFilesResourceMaterializer(
        room_files=FakeRoomFiles({}),
        artifact_writer=FakeWriter(),
        inline_artifact_reader=inbox,
    )

    prepared = await adapter.describe_artifact(
        descriptor.ref_id, conversation_id="room-1", room_epoch=1
    )
    assert prepared is not None
    assert prepared.mime_type == "application/vnd.hybro.result+json"
    assert prepared.content_digest == digest

    ref = FrozenCallResourceRef(
        ref_id=prepared.ref_id,
        kind="artifact",
        conversation_id="room-1",
        room_epoch=1,
        source_message_id=prepared.source_message_id,
        mime_type=prepared.mime_type,
        size_bytes=prepared.size_bytes,
        content_digest=prepared.content_digest,
    )
    parts = await adapter.materialize(
        FrozenCallResourceManifest(
            manifest_id="m", refs=[ref], content_digest="manifest-digest"
        ),
        conversation_id="room-1",
        room_epoch=1,
        allowed_input_modes=["application/vnd.hybro.result+json"],
        deadline_at=_future_deadline(),
    )
    assert len(parts) == 1
    assert parts[0].kind == "data"
    assert parts[0].payload == data
    assert parts[0].metadata == metadata
    assert parts[0].content_digest == digest


async def test_inbound_artifacts_delegate_to_writer():
    writer = FakeWriter()
    adapter = RoomFilesResourceMaterializer(
        room_files=FakeRoomFiles({}),
        artifact_writer=writer,
    )
    refs = await adapter.materialize_inbound_artifacts(
        call=object(),
        artifact_refs=["https://example/file.bin"],
        observation_id="observation-1",
    )
    assert refs == ["durable:https://example/file.bin"]
    assert writer.calls[0][1] == "https://example/file.bin"


async def test_owned_room_file_url_rejects_cross_room_file():
    writer = FakeWriter()
    adapter = RoomFilesResourceMaterializer(
        room_files=FakeRoomFiles(
            {"stolen-file": b"secret"}, conversation_id="other-room"
        ),
        artifact_writer=writer,
    )

    class Call:
        conversation_id = "room-1"

    with pytest.raises(ResourceSelectionError, match="not owned by the room"):
        await adapter.materialize_inbound_artifacts(
            call=Call(),
            artifact_refs=["/api/v1/files/stolen-file/content"],
            observation_id="observation-1",
        )
    assert writer.calls == []


async def test_owned_room_file_url_requires_ownership_verifier():
    materializer = BoundedResourceMaterializer(
        outbound_loader=lambda *a: None,
        inbound_writer=lambda *a: None,
    )
    with pytest.raises(ResourceSelectionError, match="ownership verification"):
        await materializer.materialize_inbound_artifacts(
            call=object(),
            artifact_refs=["/api/v1/files/file-1/content"],
            observation_id="obs-1",
        )

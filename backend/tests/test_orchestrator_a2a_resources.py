from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from execution.orchestrator.a2a_runtime.errors import StaleRoomEpochError
from execution.orchestrator.a2a_runtime.models import (
    FrozenCallResourceManifest,
    FrozenCallResourceRef,
    MaterializedResourcePart,
)
from execution.orchestrator.a2a_runtime.resources import (
    BoundedResourceMaterializer,
    ResourceSelectionError,
    freeze_call_manifest,
    verify_materialized_digests,
)
from execution.orchestrator.models import (
    PreparedResourceRef,
    RunResourceManifestSnapshot,
)
from execution.repository.orchestrator.artifacts import GuardedRoomFileArtifactWriter
from room_files import RoomFiles
from tests.fakes.room_files import MemoryFileContentStore
from tests.orchestrator_harness.a2a_in_memory import InMemoryRoomEpochStore

from ._orchestrator_a2a_helpers import binding, ledger_record
from .test_room_files import AlwaysValidLeases, InMemoryCollection, room_lifecycle


def manifest():
    return RunResourceManifestSnapshot(
        manifest_id="run-resources",
        refs=[
            PreparedResourceRef(
                ref_id="attachment-1",
                kind="attachment",
                source_message_id="message-1",
                mime_type="application/pdf",
                size_bytes=100,
                content_digest="digest-1",
            )
        ],
        content_digest="run-digest",
    )


def test_selected_refs_are_frozen_with_room_epoch_and_digest():
    bound = binding().model_copy(update={"input_modes": ["application/pdf"]})
    frozen = freeze_call_manifest(
        arguments={"task": "review", "attachment_refs": ["attachment-1"]},
        run_manifest=manifest(),
        binding=bound,
        source_conversation_id="room-1",
        source_room_epoch=1,
    )
    assert frozen.refs[0].conversation_id == "room-1"
    assert frozen.refs[0].room_epoch == 1
    assert frozen.refs[0].content_digest == "digest-1"


def test_omitted_context_refs_freeze_only_compatible_root_context_deterministically():
    run_manifest = RunResourceManifestSnapshot(
        manifest_id="run-resources",
        refs=[
            PreparedResourceRef(
                ref_id="ctx:message:root",
                kind="context",
                source_message_id="root-message",
                mime_type="text/plain",
                size_bytes=12,
                content_digest="root-digest",
            ),
            PreparedResourceRef(
                ref_id="ctx:message:other",
                kind="context",
                source_message_id="other-message",
                mime_type="text/plain",
                size_bytes=12,
                content_digest="other-digest",
            ),
            PreparedResourceRef(
                ref_id="ctx:message:not-authorized",
                kind="context",
                source_message_id="root-message",
                mime_type="text/plain",
                size_bytes=12,
                content_digest="unauthorized-digest",
            ),
            PreparedResourceRef(
                ref_id="artifact-1",
                kind="artifact",
                source_message_id="observation-1",
                mime_type="application/json",
                size_bytes=20,
                content_digest="artifact-digest",
            ),
            PreparedResourceRef(
                ref_id="attachment-1",
                kind="attachment",
                source_message_id="root-message",
                mime_type="application/pdf",
                size_bytes=100,
                content_digest="attachment-digest",
            ),
        ],
        content_digest="run-digest",
    )
    bound = binding().model_copy(
        update={
            "input_modes": ["text", "application/pdf"],
            "compatible_resource_refs": [
                "ctx:message:root",
                "ctx:message:other",
                "attachment-1",
            ],
        }
    )
    kwargs = {
        "arguments": {"task": "review"},
        "run_manifest": run_manifest,
        "binding": bound,
        "source_conversation_id": "room-1",
        "source_room_epoch": 1,
        "root_context_source_message_id": "root-message",
    }

    frozen = freeze_call_manifest(**kwargs)
    replay = freeze_call_manifest(**kwargs)

    assert [ref.ref_id for ref in frozen.refs] == ["ctx:message:root"]
    assert frozen == replay
    assert frozen.manifest_id == f"call-resources-{frozen.content_digest}"


def test_explicit_context_refs_remain_exact_without_implicit_root_context():
    run_manifest = RunResourceManifestSnapshot(
        manifest_id="run-resources",
        refs=[
            PreparedResourceRef(
                ref_id="ctx:message:root",
                kind="context",
                source_message_id="root-message",
                mime_type="text/plain",
                content_digest="root-digest",
            ),
            PreparedResourceRef(
                ref_id="ctx:message:selected",
                kind="context",
                source_message_id="selected-message",
                mime_type="text/plain",
                content_digest="selected-digest",
            ),
        ],
        content_digest="run-digest",
    )
    frozen = freeze_call_manifest(
        arguments={"task": "review", "context_refs": ["ctx:message:selected"]},
        run_manifest=run_manifest,
        binding=binding().model_copy(update={"input_modes": ["text"]}),
        source_conversation_id="room-1",
        source_room_epoch=1,
        root_context_source_message_id="root-message",
    )

    assert [ref.ref_id for ref in frozen.refs] == ["ctx:message:selected"]


def test_unknown_or_wrong_kind_resource_ref_is_rejected():
    bound = binding().model_copy(update={"input_modes": ["application/pdf"]})
    with pytest.raises(ResourceSelectionError, match="not allowed"):
        freeze_call_manifest(
            arguments={"task": "review", "attachment_refs": ["missing"]},
            run_manifest=manifest(),
            binding=bound,
            source_conversation_id="room-1",
            source_room_epoch=1,
        )
    with pytest.raises(ResourceSelectionError, match="wrong kind"):
        freeze_call_manifest(
            arguments={"task": "review", "artifact_refs": ["attachment-1"]},
            run_manifest=manifest(),
            binding=bound,
            source_conversation_id="room-1",
            source_room_epoch=1,
        )


async def test_inbound_adapter_rejects_raw_uri_and_path_refs():
    async def outbound_loader(*args):
        raise AssertionError("not used")

    async def inbound_writer(call, artifact_ref, observation_id):
        return f"durable:{artifact_ref}:{observation_id}"

    adapter = BoundedResourceMaterializer(
        outbound_loader=outbound_loader,
        inbound_writer=inbound_writer,
    )
    with pytest.raises(ResourceSelectionError, match="URI/path"):
        await adapter.materialize_inbound_artifacts(
            call=object(),
            artifact_refs=["https://untrusted.example/file"],
            observation_id="observation-1",
        )


async def test_guarded_remote_fetch_and_room_file_owner_run_through_plan3_adapter():
    class Owner:
        def __init__(self):
            self.calls = []

        @asynccontextmanager
        async def write_lease(self, conversation_id, owner):
            yield "lease-1"

        async def store_agent_artifact(self, **kwargs):
            self.calls.append(kwargs)
            return {"file_id": "durable-file-1"}

        def content_url(self, file_id):
            return f"/api/v1/files/{file_id}/content"

    async def guarded_fetch(uri):
        assert uri == "https://files.example/report.pdf"
        return b"safe-content", "application/pdf"

    owner = Owner()
    epochs = InMemoryRoomEpochStore()
    await epochs.activate("room-1", "creation-1", activated_at=datetime.now(UTC))
    writer = GuardedRoomFileArtifactWriter(
        room_files=owner, room_epochs=epochs, guarded_fetcher=guarded_fetch
    )

    async def unused(*args):
        raise AssertionError("not used")

    adapter = BoundedResourceMaterializer(
        outbound_loader=unused,
        inbound_writer=writer,
        allow_guarded_remote_artifact_refs=True,
    )
    durable = await adapter.materialize_inbound_artifacts(
        call=ledger_record(),
        artifact_refs=["https://files.example/report.pdf"],
        observation_id="observation-1",
    )
    assert durable == ["/api/v1/files/durable-file-1/content"]
    assert owner.calls[0]["conversation_id"] == "room-1"
    assert owner.calls[0]["source_message_id"] == "observation-1"
    assert owner.calls[0]["content"] == b"safe-content"


async def test_guarded_plan3_adapter_persists_through_real_room_file_owner():
    file_id = uuid4().hex
    metadata = InMemoryCollection()
    rooms = InMemoryCollection()
    rooms.docs.append(
        {
            "room_id": "room-1",
            "room_owner_id": "user-1",
            "lifecycle_state": "active",
        }
    )
    content = MemoryFileContentStore()
    owner = RoomFiles(
        metadata=metadata,
        content=content,
        room_lifecycle=room_lifecycle(rooms),
        file_id_factory=lambda: file_id,
    )
    owner._leases = AlwaysValidLeases()

    async def guarded_fetch(uri):
        assert uri == "https://files.example/evidence.txt"
        return b"owner-guarded", "text/plain"

    epochs = InMemoryRoomEpochStore()
    await epochs.activate("room-1", "creation-1", activated_at=datetime.now(UTC))
    writer = GuardedRoomFileArtifactWriter(
        room_files=owner, room_epochs=epochs, guarded_fetcher=guarded_fetch
    )
    durable_url = await writer(
        ledger_record(), "https://files.example/evidence.txt", "observation-owner"
    )
    assert durable_url == f"/api/v1/files/{file_id}/content"
    stored = await metadata.find_one({"file_id": file_id})
    assert stored["room_id"] == "room-1" and stored["status"] == "ready"
    assert await content.read(file_id, 1024) == b"owner-guarded"


async def test_guarded_artifact_commit_rejects_fetch_time_epoch_recreation():
    epochs = InMemoryRoomEpochStore()
    _, first = await epochs.activate(
        "room-1", "creation-1", activated_at=datetime.now(UTC)
    )

    class Owner:
        def __init__(self):
            self.writes = 0

        @asynccontextmanager
        async def write_lease(self, conversation_id, owner):
            yield "recreated-room-lease"

        async def store_agent_artifact(self, **kwargs):
            self.writes += 1
            return {"file_id": "must-not-exist"}

        def content_url(self, file_id):
            return f"/files/{file_id}"

    async def guarded_fetch(uri):
        await epochs.deactivate(
            "room-1",
            first.epoch,
            "deletion-1",
            deactivated_at=datetime.now(UTC),
        )
        outcome, recreated = await epochs.activate(
            "room-1", "creation-2", activated_at=datetime.now(UTC)
        )
        assert outcome == "accepted" and recreated.epoch == first.epoch + 1
        return b"old-epoch-content", "text/plain"

    owner = Owner()
    writer = GuardedRoomFileArtifactWriter(
        room_files=owner,
        room_epochs=epochs,
        guarded_fetcher=guarded_fetch,
    )
    with pytest.raises(StaleRoomEpochError, match="epoch is no longer active"):
        await writer(
            ledger_record(), "https://files.example/old.txt", "observation-race"
        )
    assert owner.writes == 0
    assert not await epochs.verify_active("room-1", first.epoch)


async def test_default_guarded_fetch_primitive_rejects_path_before_owner_write():
    class Owner:
        @asynccontextmanager
        async def write_lease(self, conversation_id, owner):
            yield "lease-1"

        async def store_agent_artifact(self, **kwargs):
            raise AssertionError("unsafe path reached Room file owner")

        def content_url(self, file_id):
            return file_id

    writer = GuardedRoomFileArtifactWriter(
        room_files=Owner(), room_epochs=InMemoryRoomEpochStore()
    )
    with pytest.raises(ValueError, match="unsupported remote URI"):
        await writer(ledger_record(), "/etc/passwd", "observation-1")


async def test_raw_and_encoded_limits_are_enforced_separately():
    ref = FrozenCallResourceRef(
        ref_id="attachment-1",
        kind="attachment",
        conversation_id="room-1",
        room_epoch=1,
        source_message_id="message-1",
        size_bytes=2,
        content_digest="digest-1",
    )
    frozen = FrozenCallResourceManifest(
        manifest_id="manifest", refs=[ref], content_digest="manifest-digest"
    )

    async def loader(*args):
        return MaterializedResourcePart(
            ref_id="attachment-1",
            kind="file",
            content_digest="digest-1",
            payload="encoded-payload",
        )

    async def writer(call, artifact_ref, observation_id):
        return artifact_ref

    raw_limited = BoundedResourceMaterializer(
        outbound_loader=loader,
        inbound_writer=writer,
        max_outbound_bytes=1,
    )
    with pytest.raises(ResourceSelectionError, match="outbound resource bytes"):
        await raw_limited.materialize(
            frozen,
            conversation_id="room-1",
            room_epoch=1,
            allowed_input_modes=["file"],
            deadline_at=datetime.now(UTC) + timedelta(minutes=1),
        )

    adapter = BoundedResourceMaterializer(
        outbound_loader=loader,
        inbound_writer=writer,
        max_outbound_bytes=2,
        max_outbound_encoded_bytes=4,
        max_inbound_encoded_bytes=4,
    )
    with pytest.raises(ResourceSelectionError, match="encoded"):
        await adapter.materialize(
            frozen,
            conversation_id="room-1",
            room_epoch=1,
            allowed_input_modes=["file"],
            deadline_at=datetime.now(UTC) + timedelta(minutes=1),
        )
    with pytest.raises(ResourceSelectionError, match="inbound encoded"):
        await adapter.materialize_inbound_artifacts(
            call=object(), artifact_refs=["12345"], observation_id="observation"
        )


def test_materialized_content_must_match_frozen_digest_exactly():
    bound = binding().model_copy(update={"input_modes": ["application/pdf"]})
    frozen = freeze_call_manifest(
        arguments={"task": "review", "attachment_refs": ["attachment-1"]},
        run_manifest=manifest(),
        binding=bound,
        source_conversation_id="room-1",
        source_room_epoch=1,
    )
    verify_materialized_digests(
        frozen,
        [
            MaterializedResourcePart(
                ref_id="attachment-1",
                kind="file",
                content_digest="digest-1",
                payload="opaque-owner-ref",
                mime_type="application/pdf",
            )
        ],
    )
    with pytest.raises(ResourceSelectionError, match="changed"):
        verify_materialized_digests(
            frozen,
            [
                MaterializedResourcePart(
                    ref_id="attachment-1",
                    kind="file",
                    content_digest="changed",
                    payload="opaque-owner-ref",
                )
            ],
        )

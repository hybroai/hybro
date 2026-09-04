from __future__ import annotations

import asyncio
import base64
from datetime import UTC, datetime, timedelta

import pytest

from a2a_adapter.orchestrator_direct_client import (
    _failed_materialization_observation_kwargs,
    _materialize_task_artifacts_epoch_fenced,
    _task_to_observation_kwargs,
)
from common.dto.hitl import (
    A2AInteractionSpec,
    HITLQuestionAnswer,
)
from common.types import Artifact, FileContent, FilePart, Part, Task, TaskStatus
from execution.orchestrator.a2a_runtime.hitl import (
    A2AContinuationCoordinator,
    InMemoryHITLApplicationPort,
)
from execution.orchestrator.a2a_runtime.in_memory import (
    InMemoryAgentCallLedgerStore,
    InMemoryAgentToolBindingStore,
    InMemoryObservationConflictStore,
    InMemoryObservationInboxStore,
    InMemoryPreparedInvocationSnapshotReader,
    InMemoryRoomEpochStore,
)
from execution.orchestrator.a2a_runtime.ingress import (
    A2AObservationIngress,
    RejectExternalIngressAuthenticator,
)
from execution.orchestrator.a2a_runtime.ledger import (
    ownership_alias_keys,
    transition_call,
)
from execution.orchestrator.a2a_runtime.models import (
    A2ADispatchReceipt,
    A2AOwnershipAlias,
    A2ARuntimePolicy,
    NormalizedA2AObservation,
)
from execution.orchestrator.a2a_runtime.resources import (
    BoundedResourceMaterializer,
    ResourceSelectionError,
)
from execution.orchestrator.a2a_runtime.runtime import A2AAgentToolRuntime
from execution.orchestrator.a2a_runtime.terminal_interactions import (
    TerminalInteractionFinalizer,
)
from execution.orchestrator.control import ClientCancellationRequested
from execution.orchestrator.models import TextPart, ToolResult, ToolSuspension

from ._orchestrator_a2a_helpers import (
    binding,
    invocation,
    ledger_record,
    prepared,
)
from ._orchestrator_helpers import NOW, NeverCancelled


class SimpleAuthorization:
    async def authorize(self, **kwargs):
        return "authorized"


class SimpleCheckpoints:
    async def is_acceptance_checkpointed(self, *args):
        return True

    async def is_suspension_checkpointed(self, *args):
        return False


class SimpleResources:
    async def materialize(self, *args, **kwargs):
        return []


class MockEpochOwner:
    def __init__(self):
        self.commits: list[dict] = []

    async def commit(
        self,
        *,
        room_id: str,
        room_epoch: int,
        source_message_id: str,
        origin_key: str,
        content: bytes,
        content_sha256: str,
        file_name: str,
        mime_type: str,
        max_bytes: int,
    ) -> str:
        file_id = f"file-{len(self.commits) + 1}"
        self.commits.append(
            {
                "room_id": room_id,
                "room_epoch": room_epoch,
                "source_message_id": source_message_id,
                "origin_key": origin_key,
                "content": content,
                "content_sha256": content_sha256,
                "file_name": file_name,
                "mime_type": mime_type,
            }
        )
        return f"/api/v1/files/{file_id}/content"


@pytest.mark.asyncio
async def test_sync_dispatch_with_fenced_heartbeat_succeeds_past_lease_ttl():
    """Test that a long-running sync dispatch (> 3x lease TTL) renews its lease via heartbeat and finishes successfully."""
    ledger = InMemoryAgentCallLedgerStore()
    epochs = InMemoryRoomEpochStore()
    await epochs.activate("room-1", "creation-1", activated_at=NOW)
    inbox = InMemoryObservationInboxStore()
    conflicts = InMemoryObservationConflictStore()
    ingress = A2AObservationIngress(
        inbox=inbox,
        conflicts=conflicts,
        ledger=ledger,
        authenticator=RejectExternalIngressAuthenticator(),
    )
    hitl = InMemoryHITLApplicationPort()
    finalizer = TerminalInteractionFinalizer(hitl)
    prep = prepared()
    prep_reader = InMemoryPreparedInvocationSnapshotReader()
    prep_reader.put(prep)

    # Claim lease is 0.15s, renew heartbeat every 0.04s, dispatch sleeps 0.45s (> 3x lease TTL)
    policy = A2ARuntimePolicy(
        claim_lease_seconds=0.15,
        claim_renew_interval_seconds=0.04,
    )

    class SlowDispatch:
        async def dispatch(self, command):
            await asyncio.sleep(0.45)
            obs = NormalizedA2AObservation(
                observation_id="obs-slow-1",
                source_kind="direct",
                source_identity=f"direct:{prep.binding.endpoint_scope_digest}:task-1:terminal:",
                binding_scope=prep.binding.endpoint_scope_digest,
                call_record_id=command.call_record_id,
                event_kind="terminal",
                observed_at=NOW,
                task_id="task-1",
                context_id="ctx-1",
                agent_id="agent-1",
                status="completed",
                content=[{"kind": "text", "text": "generated-output"}],
                artifact_refs=[],
            )
            return A2ADispatchReceipt(
                outcome="terminal",
                task_id="task-1",
                context_id="ctx-1",
                terminal_observation=obs,
            )

    runtime = A2AAgentToolRuntime(
        ledger=ledger,
        prepared_reader=prep_reader,
        checkpoint_reader=SimpleCheckpoints(),
        authorization=SimpleAuthorization(),
        room_epochs=epochs,
        resources=SimpleResources(),
        dispatch=SlowDispatch(),
        observations=ingress,
        terminal_finalizer=finalizer,
        policy=policy,
    )

    inv = invocation()
    accepted = await runtime.accept(inv)
    assert accepted.acceptance_id

    result = await runtime.execute(inv, accepted, signal=NeverCancelled())
    assert isinstance(result, ToolResult)
    assert result.content == [TextPart(text="generated-output")]

    record = await ledger.load(inv.run_id, inv.invocation_id)
    assert record is not None
    assert record.state == "completed"
    # State version must have incremented multiple times (at least 5 renewals during the 0.45s run)
    assert record.state_version >= 5


@pytest.mark.asyncio
async def test_sync_dispatch_epoch_loss_cancels_and_suspends():
    """Test that if the room epoch is invalidated during in-flight dispatch, the dispatch task is cancelled and suspends."""
    ledger = InMemoryAgentCallLedgerStore()
    epochs = InMemoryRoomEpochStore()
    await epochs.activate("room-1", "creation-1", activated_at=NOW)
    inbox = InMemoryObservationInboxStore()
    conflicts = InMemoryObservationConflictStore()
    ingress = A2AObservationIngress(
        inbox=inbox,
        conflicts=conflicts,
        ledger=ledger,
        authenticator=RejectExternalIngressAuthenticator(),
    )
    hitl = InMemoryHITLApplicationPort()
    finalizer = TerminalInteractionFinalizer(hitl)
    prep = prepared()
    prep_reader = InMemoryPreparedInvocationSnapshotReader()
    prep_reader.put(prep)

    policy = A2ARuntimePolicy(
        claim_lease_seconds=5.0,
        claim_renew_interval_seconds=0.04,
    )

    cancelled_event = asyncio.Event()

    class LongDispatch:
        async def dispatch(self, command):
            try:
                await asyncio.sleep(5.0)
            except asyncio.CancelledError:
                cancelled_event.set()
                raise

    runtime = A2AAgentToolRuntime(
        ledger=ledger,
        prepared_reader=prep_reader,
        checkpoint_reader=SimpleCheckpoints(),
        authorization=SimpleAuthorization(),
        room_epochs=epochs,
        resources=SimpleResources(),
        dispatch=LongDispatch(),
        observations=ingress,
        terminal_finalizer=finalizer,
        policy=policy,
    )

    inv = invocation()
    accepted = await runtime.accept(inv)

    async def _bump_epoch_soon():
        await asyncio.sleep(0.1)
        # Deactivate room epoch 1
        await epochs.deactivate("room-1", 1, "deletion-1", deactivated_at=NOW)

    bump_task = asyncio.create_task(_bump_epoch_soon())

    suspension = await runtime.execute(inv, accepted, signal=NeverCancelled())
    await bump_task
    assert isinstance(suspension, ToolSuspension)
    assert cancelled_event.is_set()


@pytest.mark.asyncio
async def test_client_cancel_signal_stops_fenced_dispatch_and_propagates():
    """Client abort during slow dispatch must propagate cancellation control."""
    from execution.orchestrator.session import EventCancellationSignal

    renew_count = {"n": 0}

    class CountingLedger(InMemoryAgentCallLedgerStore):
        async def renew(self, *args, **kwargs):
            renew_count["n"] += 1
            return await super().renew(*args, **kwargs)

    ledger = CountingLedger()
    epochs = InMemoryRoomEpochStore()
    await epochs.activate("room-1", "creation-1", activated_at=NOW)
    inbox = InMemoryObservationInboxStore()
    conflicts = InMemoryObservationConflictStore()
    ingress = A2AObservationIngress(
        inbox=inbox,
        conflicts=conflicts,
        ledger=ledger,
        authenticator=RejectExternalIngressAuthenticator(),
    )
    hitl = InMemoryHITLApplicationPort()
    finalizer = TerminalInteractionFinalizer(hitl)
    prep = prepared()
    prep_reader = InMemoryPreparedInvocationSnapshotReader()
    prep_reader.put(prep)

    policy = A2ARuntimePolicy(
        claim_lease_seconds=5.0,
        claim_renew_interval_seconds=0.05,
    )

    cancelled_event = asyncio.Event()

    class LongDispatch:
        async def dispatch(self, command):
            try:
                await asyncio.sleep(5.0)
            except asyncio.CancelledError:
                cancelled_event.set()
                raise

    runtime = A2AAgentToolRuntime(
        ledger=ledger,
        prepared_reader=prep_reader,
        checkpoint_reader=SimpleCheckpoints(),
        authorization=SimpleAuthorization(),
        room_epochs=epochs,
        resources=SimpleResources(),
        dispatch=LongDispatch(),
        observations=ingress,
        terminal_finalizer=finalizer,
        policy=policy,
    )

    inv = invocation()
    accepted = await runtime.accept(inv)
    signal = EventCancellationSignal()

    async def _cancel_soon():
        await asyncio.sleep(0.12)
        signal.cancel()

    cancel_task = asyncio.create_task(_cancel_soon())
    with pytest.raises(ClientCancellationRequested):
        await runtime.execute(inv, accepted, signal=signal)
    await cancel_task

    assert cancelled_event.is_set()
    # Heartbeat must not keep renewing after cancel won the race.
    assert renew_count["n"] < 20


@pytest.mark.asyncio
async def test_evidence_preservation_when_terminal_observation_returned_on_lost_lease():
    """Test that if a dispatch returns a terminal observation, it is durably recorded before lease loss causes suspension."""
    ledger = InMemoryAgentCallLedgerStore()
    epochs = InMemoryRoomEpochStore()
    await epochs.activate("room-1", "creation-1", activated_at=NOW)
    inbox = InMemoryObservationInboxStore()
    conflicts = InMemoryObservationConflictStore()
    ingress = A2AObservationIngress(
        inbox=inbox,
        conflicts=conflicts,
        ledger=ledger,
        authenticator=RejectExternalIngressAuthenticator(),
    )
    hitl = InMemoryHITLApplicationPort()
    finalizer = TerminalInteractionFinalizer(hitl)
    prep = prepared()
    prep_reader = InMemoryPreparedInvocationSnapshotReader()
    prep_reader.put(prep)

    class FastDispatchWithEpochLossOnReturn:
        async def dispatch(self, command):
            obs = NormalizedA2AObservation(
                observation_id="obs-evidence-1",
                source_kind="direct",
                source_identity=f"direct:{prep.binding.endpoint_scope_digest}:task-1:terminal:",
                binding_scope=prep.binding.endpoint_scope_digest,
                call_record_id=command.call_record_id,
                event_kind="terminal",
                observed_at=NOW,
                task_id="task-1",
                context_id="ctx-1",
                agent_id="agent-1",
                status="completed",
                content=[{"kind": "text", "text": "valuable-output"}],
                artifact_refs=[],
            )
            # Deactivate epoch right before returning, simulating epoch loss during post-dispatch renew
            await epochs.deactivate("room-1", 1, "deletion-1", deactivated_at=NOW)
            return A2ADispatchReceipt(
                outcome="terminal",
                task_id="task-1",
                context_id="ctx-1",
                terminal_observation=obs,
            )

    runtime = A2AAgentToolRuntime(
        ledger=ledger,
        prepared_reader=prep_reader,
        checkpoint_reader=SimpleCheckpoints(),
        authorization=SimpleAuthorization(),
        room_epochs=epochs,
        resources=SimpleResources(),
        dispatch=FastDispatchWithEpochLossOnReturn(),
        observations=ingress,
        terminal_finalizer=finalizer,
    )

    inv = invocation()
    accepted = await runtime.accept(inv)

    # Execute should return ToolSuspension because renew failed due to deactivated epoch
    outcome = await runtime.execute(inv, accepted, signal=NeverCancelled())
    assert isinstance(outcome, ToolSuspension)

    # BUT the observation must already be preserved in inbox!
    inbox_record = await inbox.load("obs-evidence-1")
    assert inbox_record is not None
    assert inbox_record.observation.content[0].text == "valuable-output"


@pytest.mark.asyncio
async def test_epoch_fenced_file_with_bytes_materialization():
    """Test that inline FileWithBytes is materialized through the epoch owner before observation construction."""
    epoch_owner = MockEpochOwner()
    image_bytes = b"fake-png-image-binary-data"
    encoded_b64 = base64.b64encode(image_bytes).decode()

    task = Task(
        id="task-img-1",
        context_id="ctx-1",
        status=TaskStatus(state="completed"),
        artifacts=[
            Artifact(
                artifact_id="art-1",
                parts=[
                    Part(
                        root=FilePart(
                            file=FileContent(
                                name="story_cover.png",
                                mime_type="image/png",
                                bytes=encoded_b64,
                            )
                        )
                    )
                ],
            )
        ],
    )

    await _materialize_task_artifacts_epoch_fenced(
        task,
        epoch_owner=epoch_owner,
        room_id="room-1",
        room_epoch=1,
        call_record_id="call-1",
        message_id="msg-1",
    )

    # Check that epoch owner committed the file under the epoch fence
    assert len(epoch_owner.commits) == 1
    commit = epoch_owner.commits[0]
    assert commit["room_id"] == "room-1"
    assert commit["room_epoch"] == 1
    assert commit["file_name"] == "story_cover.png"
    assert commit["content"] == image_bytes
    assert commit["mime_type"] == "image/png"

    # Check that the task artifact was mutated to point to the room URL with bytes cleared
    part = task.artifacts[0].parts[0]
    root = getattr(part, "root", part)
    assert root.file.uri == "/api/v1/files/file-1/content"
    assert root.file.bytes is None

    # Check observation kwargs
    kwargs = _task_to_observation_kwargs(
        task,
        source_kind="direct",
        call_record_id="call-1",
        binding_scope="scope",
        agent_id="img-agent",
        task_id="task-img-1",
        context_id="ctx-1",
    )
    assert kwargs["artifact_refs"] == ["/api/v1/files/file-1/content"]
    assert (
        "[Generated file: story_cover.png (image/png)]" in kwargs["content"][0]["text"]
    )

    # Verify size is tiny (under 1KB)
    obs = NormalizedA2AObservation(**kwargs)
    json_bytes = len(obs.model_dump_json().encode())
    assert json_bytes < 1024


@pytest.mark.asyncio
async def test_materialization_fails_closed_when_epoch_owner_missing():
    """Test that materialization fails closed with RuntimeError when epoch_owner is missing."""
    image_bytes = b"fake-png-image-binary-data"
    encoded_b64 = base64.b64encode(image_bytes).decode()

    task = Task(
        id="task-img-2",
        context_id="ctx-2",
        status=TaskStatus(state="completed"),
        artifacts=[
            Artifact(
                artifact_id="art-2",
                parts=[
                    Part(
                        root=FilePart(
                            file=FileContent(
                                name="story_cover.png",
                                mime_type="image/png",
                                bytes=encoded_b64,
                            )
                        )
                    )
                ],
            )
        ],
    )

    with pytest.raises(RuntimeError, match="epoch_owner"):
        await _materialize_task_artifacts_epoch_fenced(
            task,
            epoch_owner=None,
            room_id="room-1",
            room_epoch=1,
            call_record_id="call-1",
            message_id="msg-1",
        )


@pytest.mark.asyncio
async def test_materialization_invalid_base64_raises():
    """Test that invalid base64 in FileWithBytes raises ValueError."""
    epoch_owner = MockEpochOwner()
    task = Task(
        id="task-img-3",
        context_id="ctx-3",
        status=TaskStatus(state="completed"),
        artifacts=[
            Artifact(
                artifact_id="art-3",
                parts=[
                    Part(
                        root=FilePart(
                            file=FileContent(
                                name="broken.png",
                                mime_type="image/png",
                                bytes="not-valid-base64@@@!!!",
                            )
                        )
                    )
                ],
            )
        ],
    )

    with pytest.raises(ValueError, match="invalid base64"):
        await _materialize_task_artifacts_epoch_fenced(
            task,
            epoch_owner=epoch_owner,
            room_id="room-1",
            room_epoch=1,
            call_record_id="call-1",
            message_id="msg-1",
        )


@pytest.mark.asyncio
async def test_failed_materialization_observation_kwargs_creates_valid_normalized_observation():
    """Test that _failed_materialization_observation_kwargs produces all required NormalizedA2AObservation fields."""
    kwargs = _failed_materialization_observation_kwargs(
        source_kind="direct",
        call_record_id="call-123",
        binding_scope="scope-abc",
        agent_id="img-agent",
        task_id="task-456",
        context_id="ctx-789",
        error_message="Storage cluster offline",
        cursor="1",
    )
    obs = NormalizedA2AObservation(**kwargs)
    assert obs.observation_id == "direct-call-123-task-456-materialization-failed-1"
    assert obs.status == "failed"
    assert obs.event_kind == "terminal"
    assert obs.observed_at is not None
    assert obs.source_identity == "direct:scope-abc:task-456:terminal:1"
    assert obs.content[0].text == "Storage cluster offline"
    assert obs.error_code == "artifact_materialization_failed"


@pytest.mark.asyncio
async def test_bounded_resource_materializer_passes_owned_content_url_and_rejects_malformed():
    """Test that BoundedResourceMaterializer accepts /api/v1/files/... durable room content URLs and rejects malformed."""

    async def verify_owned(room_id: str, file_id: str) -> None:
        assert room_id == ""
        assert file_id == "durable-123"

    materializer = BoundedResourceMaterializer(
        outbound_loader=lambda *a: None,
        inbound_writer=lambda *a: None,
        verify_room_file_ownership=verify_owned,
    )
    # Valid format
    refs = await materializer.materialize_inbound_artifacts(
        call=object(),
        artifact_refs=["/api/v1/files/durable-123/content"],
        observation_id="obs-1",
    )
    assert refs == ["/api/v1/files/durable-123/content"]

    # Malformed format
    with pytest.raises(ResourceSelectionError):
        await materializer.materialize_inbound_artifacts(
            call=object(),
            artifact_refs=["/api/v1/files/"],
            observation_id="obs-2",
        )

    with pytest.raises(ResourceSelectionError):
        await materializer.materialize_inbound_artifacts(
            call=object(),
            artifact_refs=["/api/v1/files/bad/extra/content"],
            observation_id="obs-3",
        )


@pytest.mark.asyncio
async def test_same_owner_renew_succeeds_even_if_expired_when_state_version_matches():
    """Test that same-owner renew succeeds when state_version and claim_owner match, even if claim_expires_at was reached."""
    ledger = InMemoryAgentCallLedgerStore()
    now = datetime.now(UTC)
    base = ledger_record(call_id="call-renew-1", state="dispatching")
    record = base.model_copy(
        update={
            "claim_owner": "worker-1",
            "claim_expires_at": now - timedelta(seconds=5),  # Expired 5 seconds ago
            "state_version": 3,
            "updated_at": now - timedelta(seconds=5),
        }
    )
    await ledger.insert(record)

    renewed = await ledger.renew(
        record.call_record_id,
        expected_state_version=3,
        owner_id="worker-1",
        lease_expires_at=now + timedelta(seconds=30),
        renewed_at=now,
    )
    assert renewed is not None
    assert renewed.claim_owner == "worker-1"
    assert renewed.state_version == 4
    assert renewed.claim_expires_at == now + timedelta(seconds=30)


@pytest.mark.asyncio
async def test_competitor_claimed_renew_fails_on_expired_record():
    """Test that if another worker claimed the record (bumping state_version / owner), renew fails."""
    ledger = InMemoryAgentCallLedgerStore()
    now = datetime.now(UTC)
    base = ledger_record(call_id="call-renew-2", state="dispatching")
    record = base.model_copy(
        update={
            "claim_owner": "recovery-worker-9",
            "claim_expires_at": now + timedelta(seconds=30),
            "state_version": 4,
            "updated_at": now,
        }
    )
    await ledger.insert(record)

    # Worker-1 tries to renew with old state_version 3
    renewed = await ledger.renew(
        record.call_record_id,
        expected_state_version=3,
        owner_id="worker-1",
        lease_expires_at=now + timedelta(seconds=30),
        renewed_at=now,
    )
    assert renewed is None


@pytest.mark.asyncio
async def test_hitl_continuation_with_fenced_heartbeat_succeeds_past_lease_ttl():
    """Test that a long-running continuation dispatch (> 3x lease TTL) renews its lease via heartbeat and finishes successfully."""
    ledger = InMemoryAgentCallLedgerStore()
    hitl = InMemoryHITLApplicationPort()
    call = ledger_record()
    call = transition_call(call, to_state="ready_to_dispatch", updated_at=NOW)
    call = transition_call(call, to_state="dispatching", updated_at=NOW)
    aliases = [A2AOwnershipAlias(kind="task", value="task-1", binding_scope="endpoint")]
    call = transition_call(
        call,
        to_state="working",
        updated_at=NOW,
        a2a_task_id="task-1",
        a2a_context_id="context-1",
        ownership_aliases=aliases,
        ownership_alias_keys=ownership_alias_keys(aliases),
    )
    call = transition_call(call, to_state="continuation_pending", updated_at=NOW)
    spec = A2AInteractionSpec.model_validate(
        {
            "schema_version": 1,
            "interaction_id": "interaction-1",
            "questions": [
                {
                    "question_id": "q1",
                    "interaction_kind": "questionnaire",
                    "prompt": "Choose",
                    "answer_kind": "single_choice",
                    "choices": ["a", "b"],
                }
            ],
        }
    )
    fingerprint = "question-fingerprint"
    call = transition_call(
        call,
        to_state="input_required",
        updated_at=NOW,
        pending_interaction_id=spec.interaction_id,
        interaction_revision=1,
        interaction_fingerprint=fingerprint,
    )
    await ledger.insert(call)
    await hitl.create_or_replay(
        call=call, interaction=spec, interaction_fingerprint=fingerprint
    )
    _, route, _ = hitl.read_interaction_for_test(spec.interaction_id)
    bindings = InMemoryAgentToolBindingStore()
    await bindings.insert(binding())
    epochs = InMemoryRoomEpochStore()
    await epochs.activate("room-1", "create-1", activated_at=NOW)
    observations = A2AObservationIngress(
        inbox=InMemoryObservationInboxStore(),
        conflicts=InMemoryObservationConflictStore(),
        ledger=ledger,
        authenticator=RejectExternalIngressAuthenticator(),
    )

    class SlowContinuationDispatch:
        async def continue_task(self, command):
            await asyncio.sleep(0.45)
            obs = NormalizedA2AObservation(
                observation_id="obs-continuation-1",
                source_kind="direct",
                source_identity=f"direct:{command.binding_id}:task-1:terminal:",
                binding_scope="endpoint",
                call_record_id=command.call_record_id,
                event_kind="terminal",
                observed_at=NOW,
                task_id="task-1",
                context_id="context-1",
                status="completed",
                content=[{"kind": "text", "text": "continued-output"}],
                artifact_refs=[],
            )
            return A2ADispatchReceipt(
                outcome="terminal",
                task_id="task-1",
                context_id="context-1",
                terminal_observation=obs,
            )

        async def inspect_continuation(self, command):
            return await self.continue_task(command)

    class SimpleAuthRefPort:
        async def consume(self, *args, **kwargs):
            return None

    policy = A2ARuntimePolicy(
        claim_lease_seconds=0.15,
        claim_renew_interval_seconds=0.04,
    )

    coordinator = A2AContinuationCoordinator(
        ledger=ledger,
        bindings=bindings,
        hitl=hitl,
        room_epochs=epochs,
        authorization=SimpleAuthorization(),
        auth_references=SimpleAuthRefPort(),
        dispatch=SlowContinuationDispatch(),
        observations=observations,
        policy=policy,
    )

    answers = [
        HITLQuestionAnswer.model_validate(
            {
                "question_id": "q1",
                "answer": {"kind": "single_choice", "choice": "a"},
            }
        )
    ]
    outcome = await coordinator.resume(
        call_record_id=call.call_record_id,
        interaction_id="interaction-1",
        interaction_revision=1,
        route_fingerprint=route.fingerprint,
        answers=answers,
        authenticated_answerer_id="user-1",
    )
    assert outcome == "completed"

    persisted = await ledger.load_by_record_id(call.call_record_id)
    assert persisted is not None
    assert persisted.state == "completed"
    assert persisted.state_version >= 5

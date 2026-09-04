from __future__ import annotations

import asyncio
from datetime import timedelta
from hashlib import sha256

import pytest

from common.dto.hitl import HITLQuestionAnswer
from execution.orchestrator.a2a_runtime.cancellation import A2ACancellationCoordinator
from execution.orchestrator.a2a_runtime.errors import AmbiguousRemoteEffectError
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
    A2AObservationProcessor,
    RejectExternalIngressAuthenticator,
)
from execution.orchestrator.a2a_runtime.ledger import (
    ownership_alias_keys,
    transition_call,
)
from execution.orchestrator.a2a_runtime.models import (
    A2ADispatchReceipt,
    A2AObservationInboxRecord,
    A2AOwnershipAlias,
    NormalizedA2AObservation,
)
from execution.orchestrator.a2a_runtime.recovery import (
    A2ACallRecoveryService,
    A2AInboxRecoveryService,
)
from execution.orchestrator.a2a_runtime.runtime import A2AAgentToolRuntime
from execution.orchestrator.a2a_runtime.terminal_interactions import (
    TerminalInteractionFinalizer,
)
from execution.orchestrator.models import TextPart, ToolResult, ToolSuspension

from ._orchestrator_a2a_helpers import (
    binding,
    invocation,
    ledger_record,
    prepared,
)
from ._orchestrator_helpers import NOW, NeverCancelled


class Authorization:
    async def authorize(self, **kwargs):
        return "authorized"


class AuthReferences:
    async def verify(self, authorization_reference, **kwargs):
        return sha256(
            f"{authorization_reference}:{kwargs['call_record_id']}:{kwargs['binding_digest']}:{kwargs['room_epoch']}".encode()
        ).hexdigest()


class Checkpoints:
    def __init__(self, *, accepted=True, suspended=True):
        self.accepted = accepted
        self.suspended = suspended

    async def is_acceptance_checkpointed(self, *args):
        return self.accepted

    async def is_suspension_checkpointed(self, *args):
        return self.suspended


class Outcomes:
    async def is_run_terminal(self, *args):
        return False

    async def has_processed_observation(self, *args):
        return True

    async def is_outcome_checkpointed(self, *args):
        return True


class Resources:
    def __init__(self, hook=None):
        self.hook = hook

    async def materialize(self, manifest, **kwargs):
        if self.hook is not None:
            await self.hook()
        return []

    async def materialize_inbound_artifacts(self, **kwargs):
        return kwargs["artifact_refs"]


class Dispatch:
    def __init__(self, receipt=None):
        self.receipt = receipt or A2ADispatchReceipt(
            outcome="accepted", task_id="task-1", context_id="context-1"
        )
        self.dispatches = []
        self.continuations = []
        self.cancellations = []
        self.fail_continuation = False
        self.fail_cancellation = False

    async def dispatch(self, command):
        self.dispatches.append(command)
        return self.receipt

    async def inspect(self, command):
        return self.receipt

    async def continue_task(self, command):
        self.continuations.append(command)
        if self.fail_continuation:
            raise AmbiguousRemoteEffectError("lost continuation receipt")
        return self.receipt

    async def inspect_continuation(self, command):
        return A2ADispatchReceipt(outcome="accepted")

    async def cancel(self, command):
        self.cancellations.append(command)
        if self.fail_cancellation:
            raise AmbiguousRemoteEffectError("lost cancellation receipt")
        return self.receipt

    async def inspect_cancellation(self, command):
        return A2ADispatchReceipt(outcome="accepted")

    def is_command_retry_safe(self, transport_kind):
        return True


class Sink:
    def __init__(self):
        self.values = []

    async def deliver(self, run_id, observation):
        self.values.append((run_id, observation))


class TerminalRaceLedger(InMemoryAgentCallLedgerStore):
    def __init__(self):
        super().__init__()
        self.inject_terminal_race = False

    async def cas(self, record, *, expected_state_version):
        if self.inject_terminal_race and record.terminal_result is not None:
            self.inject_terminal_race = False
            current = await self.load_by_record_id(record.call_record_id)
            assert current is not None
            result = ToolResult(
                call_id=current.invocation_id,
                tool_name=current.tool_name,
                status="canceled",
                content=[TextPart(text="cancel won")],
                artifact_refs=[],
                error_code="canceled",
                error_message="cancel won",
            )
            winner = transition_call(
                current,
                to_state="canceled",
                updated_at=NOW,
                terminal_result=result,
                terminal_result_digest=sha256(
                    result.model_dump_json().encode()
                ).hexdigest(),
            )
            await super().cas(winner, expected_state_version=current.state_version)
            return "conflict"
        return await super().cas(record, expected_state_version=expected_state_version)


async def make_runtime(*, ledger=None, resources=None, dispatch=None):
    ledger = ledger or InMemoryAgentCallLedgerStore()
    snapshots = InMemoryPreparedInvocationSnapshotReader()
    snapshots.put(prepared())
    epochs = InMemoryRoomEpochStore()
    await epochs.activate("room-1", "create-1", activated_at=NOW)
    inbox = InMemoryObservationInboxStore()
    conflicts = InMemoryObservationConflictStore()
    ingress = A2AObservationIngress(
        inbox=inbox,
        conflicts=conflicts,
        ledger=ledger,
        authenticator=RejectExternalIngressAuthenticator(),
    )
    transport = dispatch or Dispatch()
    runtime = A2AAgentToolRuntime(
        ledger=ledger,
        prepared_reader=snapshots,
        checkpoint_reader=Checkpoints(),
        authorization=Authorization(),
        room_epochs=epochs,
        resources=resources or Resources(),
        dispatch=transport,
        observations=ingress,
        terminal_finalizer=TerminalInteractionFinalizer(InMemoryHITLApplicationPort()),
    )
    return runtime, ledger, epochs, ingress, conflicts, transport


@pytest.mark.parametrize(
    "state",
    [
        "delivery_uncertain",
        "working",
        "continuation_pending",
        "input_required",
        "auth_required",
        "resuming",
        "cancel_pending",
    ],
)
async def test_non_dispatchable_call_states_never_reenter_transport(state):
    ledger = InMemoryAgentCallLedgerStore()
    record = ledger_record().model_copy(update={"state": state})
    await ledger.insert(record)
    runtime, _, _, _, _, dispatch = await make_runtime(ledger=ledger)
    outcome = await runtime.execute(
        invocation(), record.acceptance, signal=NeverCancelled()
    )
    assert isinstance(outcome, ToolSuspension)
    assert dispatch.dispatches == []
    assert (await ledger.load("run-1", "call-1")).state == state


async def test_expired_dispatching_state_becomes_uncertain_without_redispatch():
    ledger = InMemoryAgentCallLedgerStore()
    record = ledger_record().model_copy(update={"state": "dispatching"})
    await ledger.insert(record)
    runtime, _, _, _, _, dispatch = await make_runtime(ledger=ledger)
    outcome = await runtime.execute(
        invocation(), record.acceptance, signal=NeverCancelled()
    )
    assert isinstance(outcome, ToolSuspension)
    assert dispatch.dispatches == []
    assert (await ledger.load("run-1", "call-1")).state == "delivery_uncertain"


async def test_reexecute_working_call_after_due_never_redispatches():
    runtime, ledger, _, _, _, dispatch = await make_runtime()
    acceptance = await runtime.accept(invocation())
    assert isinstance(
        await runtime.execute(invocation(), acceptance, signal=NeverCancelled()),
        ToolSuspension,
    )
    working = await ledger.load("run-1", "call-1")
    due = working.model_copy(
        update={"next_attempt_at": None, "state_version": working.state_version + 1}
    )
    assert (
        await ledger.cas(due, expected_state_version=working.state_version)
        == "accepted"
    )
    assert isinstance(
        await runtime.execute(invocation(), acceptance, signal=NeverCancelled()),
        ToolSuspension,
    )
    assert len(dispatch.dispatches) == 1
    assert (await ledger.load("run-1", "call-1")).state == "working"


async def test_ordinary_dispatch_receipt_binds_callback_ownership_end_to_end():
    runtime, ledger, epochs, ingress, conflicts, _ = await make_runtime()
    acceptance = await runtime.accept(invocation())
    await runtime.execute(invocation(), acceptance, signal=NeverCancelled())
    persisted = await ledger.load("run-1", "call-1")
    assert persisted.ownership_alias_keys
    item = NormalizedA2AObservation(
        observation_id="callback-1",
        source_kind="webhook",
        source_identity="webhook:callback-1",
        binding_scope="endpoint",
        event_kind="terminal",
        status="completed",
        observed_at=NOW,
        task_id="task-1",
        context_id="context-1",
    )
    await ingress.record(item)
    sink = Sink()
    processor = A2AObservationProcessor(
        inbox=ingress.inbox,
        conflicts=conflicts,
        ledger=ledger,
        room_epochs=epochs,
        artifacts=Resources(),
        hitl=InMemoryHITLApplicationPort(),
        sink=sink,
        checkpoint_reader=Checkpoints(),
        outcome_reader=Outcomes(),
    )
    assert await processor.process(item.observation_id) == "accepted"
    assert (await ledger.load("run-1", "call-1")).state == "completed"
    assert len(sink.values) == 1


async def test_losing_local_terminal_cas_returns_persisted_winner():
    ledger = TerminalRaceLedger()
    dispatch = Dispatch(A2ADispatchReceipt(outcome="rejected"))
    runtime, _, _, _, _, _ = await make_runtime(ledger=ledger, dispatch=dispatch)
    acceptance = await runtime.accept(invocation())
    ledger.inject_terminal_race = True
    outcome = await runtime.execute(invocation(), acceptance, signal=NeverCancelled())
    assert isinstance(outcome, ToolResult)
    assert outcome.status == "canceled"
    assert (await ledger.load("run-1", "call-1")).state == "canceled"


async def test_epoch_loss_during_materialization_prevents_dispatch():
    holder = {}

    async def deactivate():
        await holder["epochs"].deactivate("room-1", 1, "delete-1", deactivated_at=NOW)

    runtime, ledger, epochs, _, _, dispatch = await make_runtime(
        resources=Resources(deactivate)
    )
    holder["epochs"] = epochs
    acceptance = await runtime.accept(invocation())
    outcome = await runtime.execute(invocation(), acceptance, signal=NeverCancelled())
    assert isinstance(outcome, ToolSuspension)
    assert dispatch.dispatches == []
    assert (await ledger.load("run-1", "call-1")).state == "accepted"


async def test_repeated_conflicting_callback_is_deterministic():
    runtime, ledger, _, ingress, conflicts, _ = await make_runtime()
    await runtime.accept(invocation())
    call = await ledger.load("run-1", "call-1")
    first = NormalizedA2AObservation(
        observation_id="obs-1",
        call_record_id=call.call_record_id,
        source_kind="webhook",
        source_identity="stable-source",
        binding_scope="endpoint",
        event_kind="terminal",
        status="completed",
        observed_at=NOW,
    )
    await ingress.record(first)
    changed = first.model_copy(update={"status": "failed"})
    assert (await ingress.record(changed))[0] == "conflict"
    assert (await ingress.record(changed))[0] == "conflict"
    assert len(await conflicts.list_for_source("stable-source")) == 1


async def test_expired_inbox_claim_takeover_rejects_stale_owner_commit():
    inbox = InMemoryObservationInboxStore()
    observation = NormalizedA2AObservation(
        observation_id="obs-claim",
        source_kind="webhook",
        source_identity="source-claim",
        binding_scope="endpoint",
        event_kind="working",
        observed_at=NOW,
    )
    record = A2AObservationInboxRecord(
        observation_id=observation.observation_id,
        source_kind=observation.source_kind,
        source_identity=observation.source_identity,
        payload_digest="digest",
        received_at=NOW,
        binding_scope=observation.binding_scope,
        room_id="room-1",
        room_epoch=1,
        event_kind=observation.event_kind,
        observation=observation,
    )
    await inbox.insert(record)
    first = await inbox.claim(
        record.observation_id,
        expected_state_version=0,
        owner_id="owner-a",
        claim_token="token-a",
        lease_expires_at=NOW + timedelta(seconds=1),
        claimed_at=NOW,
    )
    second = await inbox.claim(
        record.observation_id,
        expected_state_version=first.state_version,
        owner_id="owner-b",
        claim_token="token-b",
        lease_expires_at=NOW + timedelta(seconds=5),
        claimed_at=NOW + timedelta(seconds=2),
    )
    assert second is not None
    stale = first.model_copy(
        update={"state": "completed", "state_version": first.state_version + 1}
    )
    assert (
        await inbox.cas(
            stale,
            expected_state_version=first.state_version,
            owner_id="owner-a",
            claim_token="token-a",
        )
        == "conflict"
    )


async def test_invalid_hitl_metadata_fails_call_instead_of_stranding():
    runtime, ledger, epochs, ingress, conflicts, _ = await make_runtime()
    acceptance = await runtime.accept(invocation())
    await runtime.execute(invocation(), acceptance, signal=NeverCancelled())
    item = NormalizedA2AObservation(
        observation_id="bad-hitl",
        source_kind="webhook",
        source_identity="webhook:bad-hitl",
        binding_scope="endpoint",
        event_kind="input_required",
        observed_at=NOW,
        task_id="task-1",
        interaction_spec={"unsupported": True},
    )
    await ingress.record(item)
    processor = A2AObservationProcessor(
        inbox=ingress.inbox,
        conflicts=conflicts,
        ledger=ledger,
        room_epochs=epochs,
        artifacts=Resources(),
        hitl=InMemoryHITLApplicationPort(),
        sink=Sink(),
        checkpoint_reader=Checkpoints(),
        outcome_reader=Outcomes(),
    )
    await processor.process(item.observation_id)
    persisted = await ledger.load("run-1", "call-1")
    assert persisted.state == "failed"
    assert persisted.error_code == "invalid_interaction_metadata"


async def _waiting_hitl_call():
    store = InMemoryAgentCallLedgerStore()
    call = transition_call(
        ledger_record(), to_state="ready_to_dispatch", updated_at=NOW
    )
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
    call = transition_call(
        call,
        to_state="input_required",
        updated_at=NOW,
        pending_interaction_id="interaction-1",
        interaction_revision=1,
        interaction_fingerprint="interaction-fingerprint",
    )
    await store.insert(call)
    return store, call


async def test_continuation_command_survives_ambiguous_send_and_recovers():
    ledger, call = await _waiting_hitl_call()
    bindings = InMemoryAgentToolBindingStore()
    await bindings.insert(binding())
    hitl = InMemoryHITLApplicationPort()
    spec = {
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
    from common.dto.hitl import A2AInteractionSpec

    await hitl.create_or_replay(
        call=call,
        interaction=A2AInteractionSpec.model_validate(spec),
        interaction_fingerprint="interaction-fingerprint",
    )
    _, route, _ = hitl.read_interaction_for_test("interaction-1")
    epochs = InMemoryRoomEpochStore()
    await epochs.activate("room-1", "create-1", activated_at=NOW)
    ingress = A2AObservationIngress(
        inbox=InMemoryObservationInboxStore(),
        conflicts=InMemoryObservationConflictStore(),
        ledger=ledger,
        authenticator=RejectExternalIngressAuthenticator(),
    )
    dispatch = Dispatch()
    dispatch.fail_continuation = True
    coordinator = A2AContinuationCoordinator(
        ledger=ledger,
        bindings=bindings,
        hitl=hitl,
        room_epochs=epochs,
        authorization=Authorization(),
        auth_references=AuthReferences(),
        dispatch=dispatch,
        observations=ingress,
    )
    answers = [
        HITLQuestionAnswer.model_validate(
            {
                "question_id": "q1",
                "answer": {"kind": "single_choice", "choice": "a"},
            }
        )
    ]
    assert (
        await coordinator.resume(
            call_record_id=call.call_record_id,
            interaction_id="interaction-1",
            interaction_revision=1,
            route_fingerprint=route.fingerprint,
            answers=answers,
            authenticated_answerer_id="user-1",
        )
        == "delivery_uncertain"
    )
    persisted = await ledger.load_by_record_id(call.call_record_id)
    assert persisted.continuation_command is not None
    due = persisted.model_copy(
        update={"next_attempt_at": None, "state_version": persisted.state_version + 1}
    )
    assert (
        await ledger.cas(due, expected_state_version=persisted.state_version)
        == "accepted"
    )
    dispatch.fail_continuation = False
    assert (
        await coordinator.recover_call(call_record_id=call.call_record_id) == "working"
    )
    assert len(dispatch.continuations) == 1


async def test_cancellation_is_terminal_without_remote_transport():
    ledger = InMemoryAgentCallLedgerStore()
    call = transition_call(
        ledger_record(), to_state="ready_to_dispatch", updated_at=NOW
    )
    call = transition_call(call, to_state="dispatching", updated_at=NOW)
    call = transition_call(call, to_state="working", updated_at=NOW)
    await ledger.insert(call)
    epochs = InMemoryRoomEpochStore()
    await epochs.activate("room-1", "create-1", activated_at=NOW)
    inbox = InMemoryObservationInboxStore()
    ingress = A2AObservationIngress(
        inbox=inbox,
        conflicts=InMemoryObservationConflictStore(),
        ledger=ledger,
        authenticator=RejectExternalIngressAuthenticator(),
    )
    dispatch = Dispatch()
    dispatch.fail_cancellation = True
    coordinator = A2ACancellationCoordinator(
        ledger=ledger,
        room_epochs=epochs,
        dispatch=dispatch,
        observations=ingress,
        hitl=InMemoryHITLApplicationPort(),
    )
    assert (
        await coordinator.cancel_call(
            call_record_id=call.call_record_id, reason="cancel"
        )
        == "canceled"
    )
    persisted = await ledger.load_by_record_id(call.call_record_id)
    assert persisted is not None
    assert persisted.cancellation_command is not None
    assert persisted.state == "canceled"
    assert dispatch.cancellations == []
    assert (
        await inbox.load(f"cancel-observation-{persisted.cancellation_command_id}")
        is None
    )


async def test_alias_collision_is_rejected_by_store_cas():
    store = InMemoryAgentCallLedgerStore()
    first = ledger_record(run_id="run-1", call_id="call-1")
    second = ledger_record(run_id="run-2", call_id="call-2")
    alias = A2AOwnershipAlias(
        kind="task", value="task-shared", binding_scope="endpoint"
    )
    first = first.model_copy(
        update={
            "ownership_aliases": [alias],
            "ownership_alias_keys": ownership_alias_keys([alias]),
        }
    )
    second = second.model_copy(
        update={
            "ownership_aliases": [alias],
            "ownership_alias_keys": ownership_alias_keys([alias]),
        }
    )
    assert await store.insert(first) == "accepted"
    assert await store.insert(second) == "conflict"


async def test_overlapping_call_recovery_has_one_leased_owner():
    ledger = InMemoryAgentCallLedgerStore()
    record = ledger_record()
    await ledger.insert(record)
    epochs = InMemoryRoomEpochStore()
    await epochs.activate("room-1", "create-1", activated_at=NOW)
    ingress = A2AObservationIngress(
        inbox=InMemoryObservationInboxStore(),
        conflicts=InMemoryObservationConflictStore(),
        ledger=ledger,
        authenticator=RejectExternalIngressAuthenticator(),
    )
    calls = []
    gate = asyncio.Event()

    async def recover_dispatch(value):
        calls.append(value.call_record_id)
        gate.set()

    first = A2ACallRecoveryService(
        ledger=ledger,
        checkpoints=Checkpoints(),
        room_epochs=epochs,
        dispatch=Dispatch(),
        observations=ingress,
        recover_dispatch=recover_dispatch,
        worker_id="worker-a",
    )
    second = A2ACallRecoveryService(
        ledger=ledger,
        checkpoints=Checkpoints(),
        room_epochs=epochs,
        dispatch=Dispatch(),
        observations=ingress,
        recover_dispatch=recover_dispatch,
        worker_id="worker-b",
    )
    await asyncio.gather(
        first.recover_call(record, now=NOW),
        second.recover_call(record, now=NOW),
    )
    assert gate.is_set()
    assert calls == [record.call_record_id]


async def test_opposing_terminal_observation_is_audited_without_redelivery():
    runtime, ledger, epochs, ingress, conflicts, _ = await make_runtime()
    acceptance = await runtime.accept(invocation())
    await runtime.execute(invocation(), acceptance, signal=NeverCancelled())
    sink = Sink()
    processor = A2AObservationProcessor(
        inbox=ingress.inbox,
        conflicts=conflicts,
        ledger=ledger,
        room_epochs=epochs,
        artifacts=Resources(),
        hitl=InMemoryHITLApplicationPort(),
        sink=sink,
        checkpoint_reader=Checkpoints(),
        outcome_reader=Outcomes(),
    )
    completed = NormalizedA2AObservation(
        observation_id="terminal-winner",
        source_kind="webhook",
        source_identity="source:winner",
        binding_scope="endpoint",
        event_kind="terminal",
        status="completed",
        observed_at=NOW,
        task_id="task-1",
    )
    await ingress.record(completed)
    await processor.process(completed.observation_id)
    losing = completed.model_copy(
        update={
            "observation_id": "terminal-loser",
            "source_identity": "source:loser",
            "status": "failed",
        }
    )
    await ingress.record(losing)
    await processor.process(losing.observation_id)
    assert (await ledger.load("run-1", "call-1")).state == "completed"
    assert len(sink.values) == 1
    assert len(await conflicts.list_for_source("source:loser")) == 1


async def test_poison_inbox_row_backs_off_without_starving_later_record():
    runtime, ledger, epochs, ingress, conflicts, _ = await make_runtime()
    acceptance = await runtime.accept(invocation())
    await runtime.execute(invocation(), acceptance, signal=NeverCancelled())

    class PoisonArtifacts(Resources):
        async def materialize_inbound_artifacts(self, **kwargs):
            if kwargs["observation_id"] == "poison":
                raise ValueError("invalid artifact")
            return kwargs["artifact_refs"]

    processor = A2AObservationProcessor(
        inbox=ingress.inbox,
        conflicts=conflicts,
        ledger=ledger,
        room_epochs=epochs,
        artifacts=PoisonArtifacts(),
        hitl=InMemoryHITLApplicationPort(),
        sink=Sink(),
        checkpoint_reader=Checkpoints(),
        outcome_reader=Outcomes(),
    )
    poison = NormalizedA2AObservation(
        observation_id="poison",
        source_kind="webhook",
        source_identity="source:poison",
        binding_scope="endpoint",
        event_kind="artifact",
        observed_at=NOW,
        task_id="task-1",
        artifact_refs=["bad-ref"],
    )
    healthy = NormalizedA2AObservation(
        observation_id="healthy",
        source_kind="webhook",
        source_identity="source:healthy",
        binding_scope="endpoint",
        event_kind="working",
        observed_at=NOW,
        task_id="task-1",
    )
    await ingress.record(poison)
    await ingress.record(healthy)
    recovery = A2AInboxRecoveryService(processor=processor, inbox=ingress.inbox)
    assert await recovery.recover_due(due_at=NOW) == 1
    poison_row = await ingress.inbox.load("poison")
    healthy_row = await ingress.inbox.load("healthy")
    assert poison_row.state == "pending"
    assert poison_row.next_attempt_at is not None
    assert healthy_row.state == "completed"


async def test_working_poll_does_not_consume_uncertainty_budget():
    ledger = InMemoryAgentCallLedgerStore()
    record = transition_call(
        ledger_record(), to_state="ready_to_dispatch", updated_at=NOW
    )
    record = transition_call(record, to_state="dispatching", updated_at=NOW)
    record = transition_call(record, to_state="working", updated_at=NOW)
    await ledger.insert(record)
    epochs = InMemoryRoomEpochStore()
    await epochs.activate("room-1", "create-1", activated_at=NOW)
    ingress = A2AObservationIngress(
        inbox=InMemoryObservationInboxStore(),
        conflicts=InMemoryObservationConflictStore(),
        ledger=ledger,
        authenticator=RejectExternalIngressAuthenticator(),
    )
    service = A2ACallRecoveryService(
        ledger=ledger,
        checkpoints=Checkpoints(),
        room_epochs=epochs,
        dispatch=Dispatch(),
        observations=ingress,
        recover_dispatch=lambda _: None,
    )
    await service.recover_call(record, now=NOW)
    persisted = await ledger.load_by_record_id(record.call_record_id)
    assert persisted.state == "working"
    assert persisted.inspection_attempts == 0

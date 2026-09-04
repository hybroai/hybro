from __future__ import annotations

from datetime import UTC, datetime
from hashlib import sha256
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from a2a_adapter.client_facade import A2AClientFacadeError
from a2a_adapter.orchestrator_direct_client import (
    OrchestratorDirectA2AClient,
    endpoint_scope_digest,
)
from a2a_adapter.task_status import build_completed_text_task
from common.dto.hitl import A2AInteractionSpec
from execution.orchestrator.a2a_runtime.dispatch import DirectA2ADispatchAdapter
from execution.orchestrator.a2a_runtime.errors import (
    AgentCardContractError,
    AmbiguousRemoteEffectError,
    RecoverableAuthorizationError,
    RecoverableCheckpointError,
    RecoverableEpochError,
    RecoverableResourceError,
    RecoverableTransportError,
)
from execution.orchestrator.a2a_runtime.hitl import InMemoryHITLApplicationPort
from execution.orchestrator.a2a_runtime.in_memory import (
    InMemoryAgentCallLedgerStore,
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
    A2AJoinBinding,
    A2AOwnershipAlias,
    MaterializedResourcePart,
    NormalizedA2AObservation,
)
from execution.orchestrator.a2a_runtime.preparation import RunBackedDispatchRecovery
from execution.orchestrator.a2a_runtime.recovery import (
    A2ACallRecoveryService,
    A2ARecoveryCycle,
)
from execution.orchestrator.a2a_runtime.runtime import (
    A2AAcceptanceConflict,
    A2AAcceptanceDenied,
    A2AAgentToolRuntime,
)
from execution.orchestrator.a2a_runtime.terminal_interactions import (
    TerminalInteractionFinalizer,
)
from execution.orchestrator.models import TextPart, ToolResult, ToolSuspension

from ._orchestrator_a2a_helpers import invocation, ledger_record, prepared
from ._orchestrator_helpers import NOW, NeverCancelled


class Authorization:
    def __init__(self, outcome="authorized"):
        self.outcome = outcome
        self.calls = 0

    async def authorize(self, **kwargs):
        self.calls += 1
        return self.outcome


class Checkpoints:
    def __init__(self, accepted=True):
        self.accepted = accepted

    async def is_acceptance_checkpointed(self, *args):
        return self.accepted

    async def is_suspension_checkpointed(self, *args):
        return False


class Resources:
    async def materialize(self, manifest, **kwargs):
        return [
            MaterializedResourcePart(
                ref_id=ref.ref_id,
                kind="text",
                content_digest=ref.content_digest,
                payload="content",
            )
            for ref in manifest.refs
        ]

    async def materialize_inbound_artifacts(self, **kwargs):
        return kwargs["artifact_refs"]


class InteractionCasLoserLedger(InMemoryAgentCallLedgerStore):
    def __init__(self):
        super().__init__()
        self.lost_interaction_cas = False

    async def cas(self, record, *, expected_state_version):
        if record.state == "continuation_pending" and not self.lost_interaction_cas:
            self.lost_interaction_cas = True
            return "conflict"
        return await super().cas(record, expected_state_version=expected_state_version)


class FlakyCardSdk:
    def __init__(self, *, failures: list[A2AClientFacadeError]):
        self.failures = list(failures)
        self.card_fetches = 0
        self.sent_message_ids: list[str] = []

    async def fetch_agent_card(self, url, **kwargs):
        self.card_fetches += 1
        if self.failures:
            raise self.failures.pop(0)
        return {
            "name": "Agent",
            "url": url,
            "version": "1.0.0",
            "capabilities": {},
        }

    async def send_message(self, card, message, **kwargs):
        self.sent_message_ids.append(message.message_id)
        task = build_completed_text_task(
            task_id="task-1", text="done", context_id="context-1"
        )
        return {
            "kind": "task",
            "result": task.model_dump(mode="json", by_alias=True),
        }

    async def fetch_remote_task(self, card, task_id, **kwargs):
        return None

    async def cancel_remote_task(self, card, task_id, **kwargs):
        return False

    def stream_message(self, card, message, **kwargs):
        async def empty_stream():
            if False:  # pragma: no cover - structural async generator
                yield None

        return empty_stream()


class RecordingDirectDispatch(DirectA2ADispatchAdapter):
    def __init__(self, client):
        super().__init__(client)
        self.commands = []

    async def dispatch(self, command):
        self.commands.append(command)
        return await super().dispatch(command)


class Dispatch:
    def __init__(
        self,
        receipt=None,
        error=False,
        model_reply_error=None,
        model_reply_receipt=None,
    ):
        self.receipt = receipt or A2ADispatchReceipt(
            outcome="accepted", task_id="task-1", context_id="context-1"
        )
        self.error = error
        self.model_reply_error = model_reply_error
        self.model_reply_receipt = model_reply_receipt
        self.commands = []

    async def dispatch(self, command):
        self.commands.append(command)
        if self.error:
            raise AmbiguousRemoteEffectError("ambiguous")
        return self.receipt

    async def inspect(self, command):
        return self.receipt

    async def continue_task(self, command):
        return self.receipt

    async def dispatch_model_reply(self, command):
        self.commands.append(command)
        if self.model_reply_error is not None:
            raise self.model_reply_error
        return self.model_reply_receipt or self.receipt

    async def inspect_continuation(self, command):
        return self.receipt

    async def cancel(self, command):
        return self.receipt

    async def inspect_cancellation(self, command):
        return self.receipt

    def is_command_retry_safe(self, transport_kind):
        return True


async def setup(
    *,
    checkpointed=True,
    auth="authorized",
    dispatch=None,
    direct_capabilities=None,
    binding_endpoint_scope_digest=None,
    ledger=None,
    hitl=None,
    run_store=None,
):
    ledger = ledger or InMemoryAgentCallLedgerStore()
    snapshots = InMemoryPreparedInvocationSnapshotReader()
    snapshot = prepared()
    binding_updates = {}
    if direct_capabilities is not None:
        binding_updates["direct_capabilities"] = direct_capabilities
    if binding_endpoint_scope_digest is not None:
        binding_updates["endpoint_scope_digest"] = binding_endpoint_scope_digest
    if binding_updates:
        snapshot = snapshot.model_copy(
            update={"binding": snapshot.binding.model_copy(update=binding_updates)}
        )
    snapshots.put(snapshot)
    epochs = InMemoryRoomEpochStore()
    await epochs.activate("room-1", "create-1", activated_at=NOW)
    authorization = Authorization(auth)
    transport = dispatch or Dispatch()
    ingress = A2AObservationIngress(
        inbox=InMemoryObservationInboxStore(),
        conflicts=InMemoryObservationConflictStore(),
        ledger=ledger,
        authenticator=RejectExternalIngressAuthenticator(),
    )
    hitl = hitl or InMemoryHITLApplicationPort()
    runtime = A2AAgentToolRuntime(
        ledger=ledger,
        prepared_reader=snapshots,
        checkpoint_reader=Checkpoints(checkpointed),
        authorization=authorization,
        room_epochs=epochs,
        resources=Resources(),
        dispatch=transport,
        observations=ingress,
        terminal_finalizer=TerminalInteractionFinalizer(hitl),
        hitl=hitl,
        run_store=run_store,
    )
    return runtime, ledger, authorization, transport, ingress


async def make_call_due(ledger: InMemoryAgentCallLedgerStore) -> None:
    record = await ledger.load("run-1", "call-1")
    assert record is not None
    due = record.model_copy(
        update={
            "next_attempt_at": datetime.now(UTC),
            "updated_at": datetime.now(UTC),
            "state_version": record.state_version + 1,
        }
    )
    assert await ledger.cas(due, expected_state_version=record.state_version) in {
        "accepted",
        "replayed",
    }


def flaky_card_dispatch(
    failures: list[A2AClientFacadeError],
) -> tuple[RecordingDirectDispatch, FlakyCardSdk]:
    sdk = FlakyCardSdk(failures=failures)
    client = OrchestratorDirectA2AClient(
        send_message=sdk.send_message,
        stream_message=sdk.stream_message,
        cancel_remote_task=sdk.cancel_remote_task,
        fetch_remote_task=sdk.fetch_remote_task,
        fetch_agent_card=sdk.fetch_agent_card,
        receipt_factory=A2ADispatchReceipt,
        observation_factory=NormalizedA2AObservation,
        recoverable_transport_error_factory=RecoverableTransportError,
        agent_card_contract_error_factory=AgentCardContractError,
    )
    return RecordingDirectDispatch(client), sdk


async def test_transient_card_fetch_retries_same_frozen_dispatch_without_false_failure():
    dispatch, sdk = flaky_card_dispatch(
        [A2AClientFacadeError("host gateway unavailable", status_code=503)]
    )
    runtime, ledger, _, _, _ = await setup(
        dispatch=dispatch,
        binding_endpoint_scope_digest=endpoint_scope_digest(
            "https://agent.example/a2a"
        ),
    )
    tool_invocation = invocation()
    accepted = await runtime.accept(tool_invocation)

    first = await runtime.execute(tool_invocation, accepted, signal=NeverCancelled())
    after_first = await ledger.load("run-1", "call-1")

    assert isinstance(first, ToolSuspension)
    assert after_first is not None
    assert after_first.state == "ready_to_dispatch"
    assert after_first.terminal_result is None
    assert after_first.transport_attempts == 1
    assert sdk.sent_message_ids == []

    await make_call_due(ledger)
    completed = await runtime.execute(
        tool_invocation, accepted, signal=NeverCancelled()
    )
    terminal = await ledger.load("run-1", "call-1")

    assert isinstance(completed, ToolResult)
    assert completed.status == "completed"
    assert terminal is not None
    assert terminal.state == "completed"
    assert terminal.terminal_result == completed
    assert terminal.terminal_result_digest is not None
    assert len(dispatch.commands) == 2
    assert dispatch.commands[0].command_id == dispatch.commands[1].command_id
    assert dispatch.commands[0].message_id == dispatch.commands[1].message_id
    assert sdk.sent_message_ids == [dispatch.commands[0].message_id]
    assert sdk.card_fetches == 2


async def test_repeated_card_fetch_503_exhausts_transport_bound_without_hot_loop():
    dispatch, sdk = flaky_card_dispatch(
        [
            A2AClientFacadeError("unavailable", status_code=503),
            A2AClientFacadeError("unavailable", status_code=503),
            A2AClientFacadeError("unavailable", status_code=503),
            A2AClientFacadeError("must not be reached", status_code=503),
        ]
    )
    runtime, ledger, _, _, _ = await setup(
        dispatch=dispatch,
        binding_endpoint_scope_digest=endpoint_scope_digest(
            "https://agent.example/a2a"
        ),
    )
    tool_invocation = invocation()
    accepted = await runtime.accept(tool_invocation)

    first = await runtime.execute(tool_invocation, accepted, signal=NeverCancelled())
    await make_call_due(ledger)
    second = await runtime.execute(tool_invocation, accepted, signal=NeverCancelled())
    await make_call_due(ledger)
    exhausted = await runtime.execute(
        tool_invocation, accepted, signal=NeverCancelled()
    )
    replay = await runtime.execute(tool_invocation, accepted, signal=NeverCancelled())
    terminal = await ledger.load("run-1", "call-1")

    assert isinstance(first, ToolSuspension)
    assert isinstance(second, ToolSuspension)
    assert isinstance(exhausted, ToolResult)
    assert exhausted.status == "failed"
    assert exhausted.error_code == "agent_card_transport_unavailable"
    assert replay == exhausted
    assert terminal is not None
    assert terminal.state == "failed"
    assert terminal.terminal_result == exhausted
    assert terminal.transport_attempts == terminal.runtime_policy.max_transport_attempts
    assert sdk.card_fetches == terminal.runtime_policy.max_transport_attempts
    assert sdk.sent_message_ids == []
    assert len(dispatch.commands) == terminal.runtime_policy.max_transport_attempts


async def test_card_fetch_404_is_terminal_nonretryable_and_ledger_agrees():
    dispatch, sdk = flaky_card_dispatch(
        [A2AClientFacadeError("not found", status_code=404)]
    )
    runtime, ledger, _, _, _ = await setup(
        dispatch=dispatch,
        binding_endpoint_scope_digest=endpoint_scope_digest(
            "https://agent.example/a2a"
        ),
    )
    tool_invocation = invocation()
    accepted = await runtime.accept(tool_invocation)

    result = await runtime.execute(tool_invocation, accepted, signal=NeverCancelled())
    terminal = await ledger.load("run-1", "call-1")

    assert isinstance(result, ToolResult)
    assert result.status == "failed"
    assert result.error_code == "agent_card_contract_error"
    assert terminal is not None
    assert terminal.state == "failed"
    assert terminal.terminal_result == result
    assert terminal.transport_attempts == 1
    assert sdk.card_fetches == 1
    assert sdk.sent_message_ids == []


async def _run_production_recovery_cycle(
    runtime: A2AAgentToolRuntime,
    ledger: InMemoryAgentCallLedgerStore,
    tool_invocation,
    *,
    due_at: datetime,
) -> None:
    class InvocationReader:
        async def read_invocation(self, *, run_id, invocation_id):
            if (
                run_id == tool_invocation.run_id
                and invocation_id == tool_invocation.invocation_id
            ):
                return tool_invocation
            return None

    recover_dispatch = RunBackedDispatchRecovery(
        prepared_reader=InvocationReader(),
        runtime=runtime,
    )
    call_recovery = A2ACallRecoveryService(
        ledger=ledger,
        checkpoints=runtime.checkpoint_reader,
        room_epochs=runtime.room_epochs,
        dispatch=runtime.dispatch,
        observations=runtime.observations,
        recover_dispatch=recover_dispatch,
    )

    async def noop():
        return None

    async def recover_calls():
        await call_recovery.recover_due(due_at=due_at)

    cycle = A2ARecoveryCycle(
        cancellation=noop,
        continuation=noop,
        observations=noop,
        calls=recover_calls,
        artifacts=noop,
        generic_runs=noop,
        projection=noop,
        watchdog=noop,
    )
    await cycle.run_once()


async def test_production_recovery_cycle_retries_card_fetch_and_never_resends_after_success():
    dispatch, sdk = flaky_card_dispatch(
        [
            A2AClientFacadeError("foreground unavailable", status_code=503),
            A2AClientFacadeError("recovery unavailable", status_code=503),
        ]
    )
    runtime, ledger, _, _, _ = await setup(
        dispatch=dispatch,
        binding_endpoint_scope_digest=endpoint_scope_digest(
            "https://agent.example/a2a"
        ),
    )
    tool_invocation = invocation()
    accepted = await runtime.accept(tool_invocation)
    first = await runtime.execute(tool_invocation, accepted, signal=NeverCancelled())
    assert isinstance(first, ToolSuspension)

    await make_call_due(ledger)
    await _run_production_recovery_cycle(
        runtime, ledger, tool_invocation, due_at=datetime.now(UTC)
    )
    after_second = await ledger.load("run-1", "call-1")
    assert after_second is not None
    assert after_second.state == "ready_to_dispatch"
    assert after_second.transport_attempts == 2
    assert after_second.terminal_result is None
    assert sdk.sent_message_ids == []

    await make_call_due(ledger)
    await _run_production_recovery_cycle(
        runtime, ledger, tool_invocation, due_at=datetime.now(UTC)
    )
    terminal = await ledger.load("run-1", "call-1")
    assert terminal is not None
    assert terminal.state == "completed"
    assert terminal.transport_attempts == 3
    assert terminal.terminal_result is not None
    assert terminal.terminal_result.status == "completed"
    assert sdk.card_fetches == 3
    assert sdk.sent_message_ids == [dispatch.commands[0].message_id]
    assert len({command.command_id for command in dispatch.commands}) == 1
    assert len({command.message_id for command in dispatch.commands}) == 1

    await _run_production_recovery_cycle(
        runtime, ledger, tool_invocation, due_at=datetime.now(UTC)
    )
    assert sdk.card_fetches == 3
    assert len(sdk.sent_message_ids) == 1
    assert (await ledger.load("run-1", "call-1")).terminal_result == (
        terminal.terminal_result
    )


async def test_production_recovery_cycle_bounds_repeated_card_503():
    dispatch, sdk = flaky_card_dispatch(
        [A2AClientFacadeError("unavailable", status_code=503)] * 4
    )
    runtime, ledger, _, _, _ = await setup(
        dispatch=dispatch,
        binding_endpoint_scope_digest=endpoint_scope_digest(
            "https://agent.example/a2a"
        ),
    )
    tool_invocation = invocation()
    accepted = await runtime.accept(tool_invocation)
    await runtime.execute(tool_invocation, accepted, signal=NeverCancelled())

    await make_call_due(ledger)
    await _run_production_recovery_cycle(
        runtime, ledger, tool_invocation, due_at=datetime.now(UTC)
    )
    await make_call_due(ledger)
    await _run_production_recovery_cycle(
        runtime, ledger, tool_invocation, due_at=datetime.now(UTC)
    )
    terminal = await ledger.load("run-1", "call-1")
    assert terminal is not None
    assert terminal.state == "failed"
    assert terminal.transport_attempts == terminal.runtime_policy.max_transport_attempts
    assert terminal.terminal_result is not None
    assert terminal.terminal_result.error_code == "agent_card_transport_unavailable"
    assert sdk.card_fetches == terminal.runtime_policy.max_transport_attempts
    assert sdk.sent_message_ids == []

    await _run_production_recovery_cycle(
        runtime, ledger, tool_invocation, due_at=datetime.now(UTC)
    )
    assert sdk.card_fetches == terminal.runtime_policy.max_transport_attempts


async def test_production_recovery_cycle_terminalizes_card_404_once():
    dispatch, sdk = flaky_card_dispatch(
        [
            A2AClientFacadeError("foreground unavailable", status_code=503),
            A2AClientFacadeError("not found", status_code=404),
        ]
    )
    runtime, ledger, _, _, _ = await setup(
        dispatch=dispatch,
        binding_endpoint_scope_digest=endpoint_scope_digest(
            "https://agent.example/a2a"
        ),
    )
    tool_invocation = invocation()
    accepted = await runtime.accept(tool_invocation)
    await runtime.execute(tool_invocation, accepted, signal=NeverCancelled())

    await make_call_due(ledger)
    await _run_production_recovery_cycle(
        runtime, ledger, tool_invocation, due_at=datetime.now(UTC)
    )
    terminal = await ledger.load("run-1", "call-1")
    assert terminal is not None
    assert terminal.state == "failed"
    assert terminal.transport_attempts == 2
    assert terminal.terminal_result is not None
    assert terminal.terminal_result.error_code == "agent_card_contract_error"
    assert sdk.card_fetches == 2
    assert sdk.sent_message_ids == []

    await _run_production_recovery_cycle(
        runtime, ledger, tool_invocation, due_at=datetime.now(UTC)
    )
    assert sdk.card_fetches == 2
    assert (await ledger.load("run-1", "call-1")).terminal_result == (
        terminal.terminal_result
    )


async def test_accept_is_durable_idempotent_and_has_no_transport_effect():
    runtime, ledger, authorization, dispatch, _ = await setup()
    accepted = await runtime.accept(invocation())
    runtime.prepared_reader = InMemoryPreparedInvocationSnapshotReader()
    replay = await runtime.accept(invocation())
    assert accepted == replay
    assert authorization.calls == 1
    assert dispatch.commands == []
    assert (await ledger.load("run-1", "call-1")) is not None


async def test_accept_allows_only_live_resource_schema_to_differ_from_binding():
    runtime, ledger, authorization, dispatch, _ = await setup()
    original = invocation()
    live_definition = original.tool.definition.model_copy(
        update={
            "input_schema": {
                "type": "object",
                "additionalProperties": False,
                "required": ["task"],
                "properties": {
                    "task": {"type": "string"},
                    "artifact_refs": {
                        "type": "array",
                        "items": {"type": "string", "enum": ["art_live"]},
                    },
                },
            }
        }
    )
    live_invocation = original.model_copy(
        update={
            "tool": original.tool.model_copy(update={"definition": live_definition})
        }
    )

    accepted = await runtime.accept(live_invocation)

    assert accepted.invocation_id == live_invocation.invocation_id
    assert authorization.calls == 1
    assert dispatch.commands == []
    assert (await ledger.load("run-1", "call-1")) is not None


async def test_accept_rejects_execution_semantic_drift_on_fresh_and_replay():
    changed = invocation()
    changed = changed.model_copy(
        update={
            "tool": changed.tool.model_copy(
                update={
                    "definition": changed.tool.definition.model_copy(
                        update={"execution_mode": "sequential"}
                    )
                }
            )
        }
    )
    fresh_runtime, fresh_ledger, _, _, _ = await setup()
    with pytest.raises(A2AAcceptanceConflict, match="does not correlate"):
        await fresh_runtime.accept(changed)
    assert await fresh_ledger.load("run-1", "call-1") is None

    replay_runtime, replay_ledger, _, _, _ = await setup()
    await replay_runtime.accept(invocation())
    with pytest.raises(A2AAcceptanceConflict, match="does not match ledger"):
        await replay_runtime.accept(changed)
    assert (await replay_ledger.load("run-1", "call-1")).execution_mode == ("parallel")


async def test_accept_rejects_terminal_run_before_ledger_insert():
    run_store = SimpleNamespace(
        load=AsyncMock(return_value=SimpleNamespace(status="canceled"))
    )
    runtime, ledger, _, _, _ = await setup(run_store=run_store)

    with pytest.raises(A2AAcceptanceDenied, match="not accepting"):
        await runtime.accept(invocation())

    assert await ledger.load("run-1", "call-1") is None


async def test_acceptance_race_terminalizes_inserted_call_when_run_cancels():
    run_store = SimpleNamespace(
        load=AsyncMock(
            side_effect=[
                SimpleNamespace(status="running"),
                SimpleNamespace(status="canceled"),
            ]
        )
    )
    runtime, ledger, _, _, _ = await setup(run_store=run_store)

    with pytest.raises(A2AAcceptanceDenied, match="became canceled"):
        await runtime.accept(invocation())

    persisted = await ledger.load("run-1", "call-1")
    assert persisted is not None
    assert persisted.state == "canceled"


async def test_accept_denial_happens_before_any_ledger_record():
    runtime, ledger, _, _, _ = await setup(auth="denied")
    try:
        await runtime.accept(invocation())
    except A2AAcceptanceDenied:
        pass
    else:
        raise AssertionError("authorization denial was accepted")
    assert await ledger.load("run-1", "call-1") is None


async def test_execute_never_dispatches_without_generic_acceptance_receipt():
    runtime, ledger, _, dispatch, _ = await setup(checkpointed=False)
    accepted = await runtime.accept(invocation())
    outcome = await runtime.execute(invocation(), accepted, signal=NeverCancelled())
    assert isinstance(outcome, ToolSuspension)
    assert dispatch.commands == []
    assert (await ledger.load("run-1", "call-1")).state == "accepted"


async def test_execute_dispatches_stable_command_and_suspends_for_remote_work():
    runtime, ledger, _, dispatch, _ = await setup()
    accepted = await runtime.accept(invocation())
    outcome = await runtime.execute(invocation(), accepted, signal=NeverCancelled())
    assert isinstance(outcome, ToolSuspension)
    assert len(dispatch.commands) == 1
    persisted = await ledger.load("run-1", "call-1")
    assert persisted.state == "working"
    assert dispatch.commands[0].command_id == persisted.dispatch_command_id


async def test_execute_input_required_returns_request_as_tool_result():
    """The Agent's request for input must reach the kernel as a durable tool
    result (not be polled away as a still-working task), so the kernel's next
    model turn can satisfy it from context or ask the user."""
    runtime, ledger, _, dispatch, ingress = await setup()
    accepted = await runtime.accept(invocation())
    record = await ledger.load("run-1", "call-1")
    observation = NormalizedA2AObservation(
        observation_id="obs-interaction-1",
        call_record_id=record.call_record_id,
        source_kind="direct",
        source_identity="direct:endpoint:task-1:input_required:",
        binding_scope=record.endpoint_scope_digest,
        event_kind="input_required",
        observed_at=NOW,
        task_id="task-1",
        context_id="context-1",
        agent_id="agent-1",
        status=None,
        content=[TextPart(text="Send me the client name and coverage limit.")],
        artifact_refs=[],
        interaction_spec=None,
        error_code=None,
        error_message=None,
        cursor=None,
    )
    receipt = A2ADispatchReceipt(
        outcome="interaction",
        task_id="task-1",
        context_id="context-1",
        interaction_observation=observation,
    )
    dispatch.receipt = receipt

    outcome = await runtime.execute(invocation(), accepted, signal=NeverCancelled())

    assert isinstance(outcome, ToolResult)
    assert outcome.status == "completed"
    assert outcome.content[0].text == "Send me the client name and coverage limit."
    persisted = await ledger.load("run-1", "call-1")
    assert persisted.state == "completed"
    assert persisted.terminal_result == outcome
    # The request is durably recorded and executor-checkpointed so the inbox
    # processor completes it from the kernel's buffered outcome digest.
    inbox_row = await ingress.inbox.load("obs-interaction-1")
    assert inbox_row is not None
    assert inbox_row.state == "outcome_pending"
    assert inbox_row.delivery_route == "executor"
    assert inbox_row.outcome_digest == persisted.terminal_result_digest


async def test_execute_typed_input_required_suspends_and_activates_hitl():
    runtime, ledger, _, dispatch, ingress = await setup()
    accepted = await runtime.accept(invocation())
    record = await ledger.load("run-1", "call-1")
    interaction = {
        "schema_version": 1,
        "interaction_id": "travel-planner:clarify-1",
        "questions": [
            {
                "question_id": "travel-details:clarify-1",
                "interaction_kind": "questionnaire",
                "prompt": "Which city should I plan for?",
                "answer_kind": "text",
                "required": True,
            }
        ],
    }
    observation = NormalizedA2AObservation(
        observation_id="obs-typed-1",
        call_record_id=record.call_record_id,
        source_kind="direct",
        source_identity="direct:endpoint:task-1:input_required:typed",
        binding_scope=record.endpoint_scope_digest,
        event_kind="input_required",
        observed_at=NOW,
        task_id="task-1",
        context_id="context-1",
        agent_id="agent-1",
        status=None,
        content=[TextPart(text="Which city should I plan for?")],
        artifact_refs=[],
        interaction_spec=interaction,
        error_code=None,
        error_message=None,
        cursor=None,
    )
    dispatch.receipt = A2ADispatchReceipt(
        outcome="interaction",
        task_id="task-1",
        context_id="context-1",
        interaction_observation=observation,
    )

    outcome = await runtime.execute(invocation(), accepted, signal=NeverCancelled())

    assert isinstance(outcome, ToolSuspension)
    persisted = await ledger.load("run-1", "call-1")
    assert persisted.state == "input_required"
    assert persisted.pending_interaction_id == "travel-planner:clarify-1"
    assert persisted.interaction_revision == 1
    assert persisted.interaction_fingerprint is not None
    stored = await runtime.hitl.read_interaction("travel-planner:clarify-1")
    assert stored is not None
    spec, route, fingerprint = stored
    assert fingerprint == persisted.interaction_fingerprint
    assert route.call_record_id == persisted.call_record_id
    assert A2AInteractionSpec.model_validate(interaction) == spec
    inbox = await ingress.inbox.load(observation.observation_id)
    assert inbox.state == "ledger_applied"
    assert inbox.delivery_route == "executor"
    assert inbox.delivery_state == "checkpointed"


async def test_execute_does_not_checkpoint_losing_typed_interaction():
    ledger = InteractionCasLoserLedger()
    runtime, ledger, _, dispatch, ingress = await setup(ledger=ledger)
    accepted = await runtime.accept(invocation())
    record = await ledger.load("run-1", "call-1")
    interaction = {
        "schema_version": 1,
        "interaction_id": "interaction-loser",
        "questions": [
            {
                "question_id": "question-loser",
                "interaction_kind": "questionnaire",
                "prompt": "Losing question?",
                "answer_kind": "text",
                "required": True,
            }
        ],
    }
    observation = NormalizedA2AObservation(
        observation_id="obs-typed-loser",
        call_record_id=record.call_record_id,
        source_kind="direct",
        source_identity="direct:endpoint:task-1:input_required:loser",
        binding_scope=record.endpoint_scope_digest,
        event_kind="input_required",
        observed_at=NOW,
        task_id="task-1",
        context_id="context-1",
        agent_id="agent-1",
        content=[TextPart(text="Losing question?")],
        interaction_spec=interaction,
    )
    dispatch.receipt = A2ADispatchReceipt(
        outcome="interaction",
        task_id="task-1",
        context_id="context-1",
        interaction_observation=observation,
    )

    outcome = await runtime.execute(invocation(), accepted, signal=NeverCancelled())

    assert isinstance(outcome, ToolSuspension)
    persisted = await ledger.load("run-1", "call-1")
    assert persisted.state == "dispatching"
    assert persisted.pending_interaction_id is None
    assert await runtime.hitl.read_interaction("interaction-loser") is None
    inbox = await ingress.inbox.load(observation.observation_id)
    assert inbox.state == "pending"
    assert inbox.delivery_route == "unresolved"


async def test_execute_invalid_interaction_spec_fails_closed():
    runtime, ledger, _, dispatch, _ = await setup()
    accepted = await runtime.accept(invocation())
    record = await ledger.load("run-1", "call-1")
    observation = NormalizedA2AObservation(
        observation_id="obs-invalid-1",
        call_record_id=record.call_record_id,
        source_kind="direct",
        source_identity="direct:endpoint:task-1:input_required:invalid",
        binding_scope=record.endpoint_scope_digest,
        event_kind="input_required",
        observed_at=NOW,
        task_id="task-1",
        context_id="context-1",
        agent_id="agent-1",
        status=None,
        content=[TextPart(text="broken")],
        artifact_refs=[],
        interaction_spec={"unsupported": True},
        error_code=None,
        error_message=None,
        cursor=None,
    )
    dispatch.receipt = A2ADispatchReceipt(
        outcome="interaction",
        task_id="task-1",
        context_id="context-1",
        interaction_observation=observation,
    )

    outcome = await runtime.execute(invocation(), accepted, signal=NeverCancelled())

    assert isinstance(outcome, ToolResult)
    assert outcome.status == "failed"
    assert outcome.error_code == "invalid_interaction_metadata"
    persisted = await ledger.load("run-1", "call-1")
    assert persisted.state == "failed"


async def test_dispatch_alias_conflict_fails_closed_without_divergent_identity():
    runtime, ledger, _, dispatch, _ = await setup()
    accepted = await runtime.accept(invocation())
    record = await ledger.load("run-1", "call-1")
    stolen = A2AOwnershipAlias(
        kind="task",
        value="task-stolen",
        binding_scope=record.endpoint_scope_digest,
    )
    poisoned = record.model_copy(
        update={
            "ownership_aliases": [stolen],
            "ownership_alias_keys": ownership_alias_keys([stolen]),
            "a2a_task_id": "task-stolen",
            "state_version": record.state_version + 1,
        }
    )
    assert (
        await ledger.cas(poisoned, expected_state_version=record.state_version)
        == "accepted"
    )

    outcome = await runtime.execute(invocation(), accepted, signal=NeverCancelled())

    assert isinstance(outcome, ToolSuspension)
    assert len(dispatch.commands) == 1
    persisted = await ledger.load("run-1", "call-1")
    assert persisted.state == "delivery_uncertain"
    assert persisted.error_code == "authoritative_alias_conflict"
    assert persisted.a2a_task_id == "task-stolen"
    assert persisted.ownership_aliases == [stolen]


async def test_frozen_binding_selects_stream_then_sync_then_poll_capability():
    for capabilities, expected in (
        (["sync", "stream", "poll"], "stream"),
        (["sync", "poll"], "sync"),
        (["poll"], "poll"),
    ):
        runtime, ledger, _, _, _ = await setup(direct_capabilities=capabilities)
        await runtime.accept(invocation())
        persisted = await ledger.load("run-1", "call-1")
        assert persisted.dispatch_snapshot.direct_mode == expected


class BoundaryFailure:
    def __init__(self, error):
        self.error = error

    async def is_acceptance_checkpointed(self, *args):
        raise self.error

    async def authorize(self, **kwargs):
        raise self.error

    async def verify_active(self, *args):
        raise self.error

    async def materialize(self, *args, **kwargs):
        raise self.error

    async def dispatch(self, command):
        raise self.error


@pytest.mark.parametrize(
    "boundary,error",
    [
        ("checkpoint", ValueError("checkpoint contract defect")),
        ("authorization", TypeError("authorization programming defect")),
        ("epoch", AssertionError("epoch invariant defect")),
        ("resource", ValueError("resource contract defect")),
        ("dispatch", RuntimeError("dispatch programming defect")),
    ],
)
async def test_programming_defects_surface_from_execute_boundaries(boundary, error):
    runtime, ledger, _, _, _ = await setup()
    accepted = await runtime.accept(invocation())
    failure = BoundaryFailure(error)
    if boundary == "checkpoint":
        runtime.checkpoint_reader = failure
    elif boundary == "authorization":
        runtime.authorization = failure
    elif boundary == "epoch":
        runtime.room_epochs = failure
    elif boundary == "resource":
        runtime.resources = failure
    else:
        runtime.dispatch = failure
    with pytest.raises(type(error), match=str(error)):
        await runtime.execute(invocation(), accepted, signal=NeverCancelled())
    if boundary == "dispatch":
        assert (await ledger.load("run-1", "call-1")).state == "dispatching"


@pytest.mark.parametrize(
    "boundary,error",
    [
        ("checkpoint", RecoverableCheckpointError("checkpoint unavailable")),
        ("authorization", RecoverableAuthorizationError("auth unavailable")),
        ("epoch", RecoverableEpochError("epoch unavailable")),
        ("resource", RecoverableResourceError("resource unavailable")),
        ("dispatch", AmbiguousRemoteEffectError("dispatch ambiguous")),
    ],
)
async def test_typed_recoverable_boundary_failures_suspend(boundary, error):
    runtime, ledger, _, _, _ = await setup()
    accepted = await runtime.accept(invocation())
    failure = BoundaryFailure(error)
    if boundary == "checkpoint":
        runtime.checkpoint_reader = failure
    elif boundary == "authorization":
        runtime.authorization = failure
    elif boundary == "epoch":
        runtime.room_epochs = failure
    elif boundary == "resource":
        runtime.resources = failure
    else:
        runtime.dispatch = failure
    assert isinstance(
        await runtime.execute(invocation(), accepted, signal=NeverCancelled()),
        ToolSuspension,
    )
    if boundary == "dispatch":
        assert (await ledger.load("run-1", "call-1")).state == ("delivery_uncertain")


async def test_transport_exception_becomes_delivery_uncertain_suspension():
    runtime, ledger, _, _, _ = await setup(dispatch=Dispatch(error=True))
    accepted = await runtime.accept(invocation())
    outcome = await runtime.execute(invocation(), accepted, signal=NeverCancelled())
    assert isinstance(outcome, ToolSuspension)
    assert (await ledger.load("run-1", "call-1")).state == "delivery_uncertain"


class RuntimeFinalizerFaultHITL(InMemoryHITLApplicationPort):
    def __init__(self, mode):
        super().__init__()
        self.mode = mode
        self.abandon_calls = 0
        self.effects = 0
        self.failed = False

    async def abandon(self, interaction_id, *, call_record_id, reason):
        self.abandon_calls += 1
        if self.mode == "absent" and not self.failed:
            self.failed = True
            self._interactions.pop(interaction_id, None)
            self._eligible_interactions.discard(interaction_id)
            return "absent"
        if self.mode == "replayed" and not self.failed:
            self.failed = True
            assert (
                await super().abandon(
                    interaction_id,
                    call_record_id=call_record_id,
                    reason=reason,
                )
                == "accepted"
            )
            self.effects += 1
            return await super().abandon(
                interaction_id,
                call_record_id=call_record_id,
                reason=reason,
            )
        if not self.failed and self.mode in {
            "conflict",
            "error",
            "outage",
            "ack_loss",
        }:
            self.failed = True
            if self.mode == "conflict":
                return "conflict"
            if self.mode == "error":
                return "error"
            if self.mode == "outage":
                raise RecoverableCheckpointError("HITL owner unavailable")
            outcome = await super().abandon(
                interaction_id,
                call_record_id=call_record_id,
                reason=reason,
            )
            assert outcome == "accepted"
            self.effects += 1
            raise RecoverableCheckpointError("HITL acknowledgement lost")
        outcome = await super().abandon(
            interaction_id,
            call_record_id=call_record_id,
            reason=reason,
        )
        if outcome == "accepted":
            self.effects += 1
        return outcome


def _interaction(event_kind):
    return A2AInteractionSpec.model_validate(
        {
            "schema_version": 1,
            "interaction_id": "interaction-1",
            "questions": [
                {
                    "question_id": "question-1",
                    "interaction_kind": (
                        "questionnaire"
                        if event_kind == "input_required"
                        else "auth_challenge"
                    ),
                    "prompt": "Continue?",
                    "answer_kind": (
                        "confirmation"
                        if event_kind == "input_required"
                        else "authorization_result"
                    ),
                }
            ],
        }
    )


def _terminal_result(record, status):
    return ToolResult(
        call_id=record.invocation_id,
        tool_name=record.tool_name,
        status=status,
        content=[TextPart(text="terminal winner")],
        artifact_refs=[],
        error_code=status,
        error_message=status,
    )


async def _persist_attached_terminal(
    ledger, owner, record, *, event_kind, terminal_status
):
    while record.state != "working":
        next_state = {
            "accepted": "ready_to_dispatch",
            "ready_to_dispatch": "dispatching",
            "dispatching": "working",
        }[record.state]
        candidate = transition_call(record, to_state=next_state, updated_at=NOW)
        assert (
            await ledger.cas(candidate, expected_state_version=record.state_version)
            == "accepted"
        )
        record = candidate
    pending = transition_call(record, to_state="continuation_pending", updated_at=NOW)
    assert (
        await ledger.cas(pending, expected_state_version=record.state_version)
        == "accepted"
    )
    attached = transition_call(
        pending,
        to_state=event_kind,
        updated_at=NOW,
        pending_interaction_id="interaction-1",
        interaction_revision=1,
        interaction_fingerprint="fingerprint-1",
    )
    assert (
        await ledger.cas(attached, expected_state_version=pending.state_version)
        == "accepted"
    )
    await owner.create_or_replay(
        call=attached,
        interaction=_interaction(event_kind),
        interaction_fingerprint="fingerprint-1",
    )
    result = _terminal_result(attached, terminal_status)
    terminal = transition_call(
        attached,
        to_state=terminal_status,
        updated_at=NOW,
        terminal_result=result,
        terminal_result_digest=sha256(result.model_dump_json().encode()).hexdigest(),
    )
    assert (
        await ledger.cas(terminal, expected_state_version=attached.state_version)
        == "accepted"
    )
    return terminal


class RuntimeTerminalWinnerLedger(InMemoryAgentCallLedgerStore):
    def __init__(self, owner, *, event_kind, terminal_status):
        super().__init__()
        self.owner = owner
        self.event_kind = event_kind
        self.terminal_status = terminal_status
        self.raced = False
        self.durable_winner = None

    async def cas(self, record, *, expected_state_version):
        if not self.raced and record.state == "working":
            self.raced = True
            assert (
                await super().cas(record, expected_state_version=expected_state_version)
                == "accepted"
            )
            self.durable_winner = await _persist_attached_terminal(
                self,
                self.owner,
                record,
                event_kind=self.event_kind,
                terminal_status=self.terminal_status,
            )
            return "conflict"
        return await super().cas(record, expected_state_version=expected_state_version)


LEGAL_ATTACHED_TERMINALS = [
    ("input_required", "canceled"),
    ("input_required", "expired"),
    ("auth_required", "canceled"),
    ("auth_required", "rejected"),
    ("auth_required", "expired"),
]
FINALIZER_OUTCOMES = [
    "accepted",
    "replayed",
    "absent",
    "conflict",
    "error",
    "outage",
    "ack_loss",
]


async def _assert_closed_and_unanswerable(owner):
    assert owner.read_interaction_for_test("interaction-1") is None
    with pytest.raises(KeyError):
        await owner.answer(
            interaction_id="interaction-1",
            interaction_revision=1,
            route_fingerprint="fingerprint-1",
            answers=[],
            authenticated_answerer_id="user-1",
            verified_auth_reference_digests=[],
            verified_auth_references=[],
        )


@pytest.mark.parametrize("event_kind,terminal_status", LEGAL_ATTACHED_TERMINALS)
@pytest.mark.parametrize("close_mode", FINALIZER_OUTCOMES)
async def test_runtime_terminal_replay_requires_exact_hitl_finalization(
    event_kind, terminal_status, close_mode
):
    owner = RuntimeFinalizerFaultHITL(close_mode)
    runtime, ledger, _, dispatch, _ = await setup(hitl=owner)
    acceptance = await runtime.accept(invocation())
    accepted = await ledger.load("run-1", "call-1")
    winner = await _persist_attached_terminal(
        ledger,
        owner,
        accepted,
        event_kind=event_kind,
        terminal_status=terminal_status,
    )
    assert owner.read_interaction_for_test("interaction-1") is not None

    first = await runtime.execute(invocation(), acceptance, signal=NeverCancelled())
    if close_mode in {"conflict", "error", "outage", "ack_loss"}:
        assert isinstance(first, ToolSuspension)
        if close_mode != "ack_loss":
            assert owner.read_interaction_for_test("interaction-1") is not None
    else:
        assert isinstance(first, ToolResult)
        assert first.status == terminal_status
        await _assert_closed_and_unanswerable(owner)
    assert await ledger.load("run-1", "call-1") == winner

    retry = await runtime.execute(invocation(), acceptance, signal=NeverCancelled())
    assert isinstance(retry, ToolResult)
    assert retry.status == terminal_status
    assert await ledger.load("run-1", "call-1") == winner
    await _assert_closed_and_unanswerable(owner)
    assert owner.effects <= 1
    assert dispatch.commands == []


@pytest.mark.parametrize("event_kind,terminal_status", LEGAL_ATTACHED_TERMINALS)
@pytest.mark.parametrize("close_mode", FINALIZER_OUTCOMES)
async def test_runtime_competing_terminal_cas_winner_requires_hitl_finalization(
    event_kind, terminal_status, close_mode
):
    owner = RuntimeFinalizerFaultHITL(close_mode)
    ledger = RuntimeTerminalWinnerLedger(
        owner, event_kind=event_kind, terminal_status=terminal_status
    )
    runtime, ledger, _, dispatch, _ = await setup(ledger=ledger, hitl=owner)
    acceptance = await runtime.accept(invocation())

    first = await runtime.execute(invocation(), acceptance, signal=NeverCancelled())
    if close_mode in {"conflict", "error", "outage", "ack_loss"}:
        assert isinstance(first, ToolSuspension)
        if close_mode != "ack_loss":
            assert owner.read_interaction_for_test("interaction-1") is not None
    else:
        assert isinstance(first, ToolResult)
        assert first.status == terminal_status
        await _assert_closed_and_unanswerable(owner)
    winner = ledger.durable_winner
    assert winner is not None
    assert await ledger.load("run-1", "call-1") == winner

    retry = await runtime.execute(invocation(), acceptance, signal=NeverCancelled())
    assert isinstance(retry, ToolResult)
    assert retry.status == terminal_status
    assert await ledger.load("run-1", "call-1") == winner
    await _assert_closed_and_unanswerable(owner)
    assert owner.effects <= 1
    assert len(dispatch.commands) == 1


async def test_runtime_parked_abandonment_propagates_recoverable_store_failure():
    class FailingHITL(InMemoryHITLApplicationPort):
        async def abandon(self, *args, **kwargs):
            raise RecoverableCheckpointError("HITL store unavailable")

    runtime, _, _, _, _ = await setup(hitl=FailingHITL())

    with pytest.raises(RecoverableCheckpointError, match="HITL store unavailable"):
        await runtime.abandon_parked_interaction(
            call_record_id="call-record-1",
            interaction_id="interaction-1",
            terminal_state="failed",
        )


async def test_runtime_terminal_finalizer_programming_defect_surfaces():
    class ProgrammingDefectHITL(InMemoryHITLApplicationPort):
        async def abandon(self, *args, **kwargs):
            raise ValueError("HITL owner contract defect")

    owner = ProgrammingDefectHITL()
    runtime, ledger, _, _, _ = await setup(hitl=owner)
    acceptance = await runtime.accept(invocation())
    accepted = await ledger.load("run-1", "call-1")
    await _persist_attached_terminal(
        ledger,
        owner,
        accepted,
        event_kind="input_required",
        terminal_status="canceled",
    )

    with pytest.raises(ValueError, match="HITL owner contract defect"):
        await runtime.execute(invocation(), acceptance, signal=NeverCancelled())


async def test_inline_terminal_evidence_is_inboxed_before_result():
    observation = NormalizedA2AObservation(
        observation_id="observation-1",
        source_kind="direct",
        source_identity="direct:event-1",
        binding_scope="endpoint",
        event_kind="terminal",
        status="completed",
        observed_at=NOW,
        task_id="task-1",
        context_id="context-1",
    )
    runtime, ledger, _, _, ingress = await setup(
        dispatch=Dispatch(
            A2ADispatchReceipt(outcome="terminal", terminal_observation=observation)
        )
    )
    accepted = await runtime.accept(invocation())
    outcome = await runtime.execute(invocation(), accepted, signal=NeverCancelled())
    assert isinstance(outcome, ToolResult)
    assert outcome.status == "completed"
    inbox = await ingress.inbox.load("observation-1")
    assert inbox is not None
    assert inbox.delivery_route == "executor"
    assert (await ledger.load("run-1", "call-1")).state == "completed"


async def test_dispatch_model_reply_bounds_fingerprint_none_and_dedupes_replay():
    runtime, ledger, _, dispatch, _ = await setup()
    parked = ledger_record(state="input_required").model_copy(
        update={
            "a2a_task_id": "task-1",
            "a2a_context_id": "context-1",
            "pending_interaction_id": "interaction-1",
            "interaction_revision": 1,
            "interaction_fingerprint": None,
        }
    )
    assert await ledger.insert(parked) == "accepted"
    inv1 = invocation(call_id="join-1")
    inv2 = invocation(call_id="join-2")
    inv3 = invocation(call_id="join-3")

    first = await runtime.dispatch_model_reply(
        inv1,
        parent_call_record_id=parked.call_record_id,
        interaction_fingerprint=None,
        signal=NeverCancelled(),
    )
    assert isinstance(first, ToolSuspension)
    assert dispatch.commands
    persisted = await ledger.load_by_record_id(parked.call_record_id)
    assert persisted.model_reply_rounds == {"": 1}
    assert len(persisted.model_reply_joins) == 1

    second = await runtime.dispatch_model_reply(
        inv2,
        parent_call_record_id=parked.call_record_id,
        interaction_fingerprint=None,
        signal=NeverCancelled(),
    )
    assert isinstance(second, ToolSuspension)
    persisted = await ledger.load_by_record_id(parked.call_record_id)
    assert persisted.model_reply_rounds == {"": 2}

    third = await runtime.dispatch_model_reply(
        inv3,
        parent_call_record_id=parked.call_record_id,
        interaction_fingerprint=None,
        signal=NeverCancelled(),
    )
    assert isinstance(third, ToolResult)
    assert third.error_code == "auto_reply_limit_reached"

    # Replaying a prior invocation re-dispatches idempotently without
    # re-checking the bound or double-counting the join.
    before = await ledger.load_by_record_id(parked.call_record_id)
    commands_before = len(dispatch.commands)
    replay = await runtime.dispatch_model_reply(
        inv2,
        parent_call_record_id=parked.call_record_id,
        interaction_fingerprint=None,
        signal=NeverCancelled(),
    )
    assert isinstance(replay, ToolSuspension)
    assert len(dispatch.commands) == commands_before + 1
    after = await ledger.load_by_record_id(parked.call_record_id)
    assert after.model_reply_rounds == before.model_reply_rounds
    assert len(after.model_reply_joins) == len(before.model_reply_joins)


async def test_model_reply_transport_suspension_carries_metadata_and_redispatch():
    observation = NormalizedA2AObservation(
        observation_id="observation-join",
        source_kind="direct",
        source_identity="direct:event-join",
        binding_scope="endpoint",
        event_kind="terminal",
        status="completed",
        observed_at=NOW,
        task_id="task-1",
        context_id="context-1",
    )
    dispatch = Dispatch(
        model_reply_error=RecoverableTransportError("temporarily unavailable"),
        model_reply_receipt=A2ADispatchReceipt(
            outcome="terminal", terminal_observation=observation
        ),
    )
    runtime, ledger, _, dispatch, _ = await setup(dispatch=dispatch)
    parked = ledger_record(state="input_required").model_copy(
        update={
            "a2a_task_id": "task-1",
            "a2a_context_id": "context-1",
            "pending_interaction_id": "interaction-1",
            "interaction_revision": 1,
            "interaction_fingerprint": "fp-1",
        }
    )
    assert await ledger.insert(parked) == "accepted"
    inv = invocation(call_id="join-1")

    # Recoverable transport failure → suspension keeps the parent call identity
    # and parked-interaction metadata so the kernel can re-dispatch.
    suspension = await runtime.dispatch_model_reply(
        inv,
        parent_call_record_id=parked.call_record_id,
        interaction_fingerprint=None,
        signal=NeverCancelled(),
    )
    assert isinstance(suspension, ToolSuspension)
    assert suspension.status == "waiting_external"
    assert suspension.delivery_state == "transport_uncertain"
    assert suspension.call_record_id == parked.call_record_id
    assert suspension.interaction_id == "interaction-1"
    assert suspension.interaction_fingerprint == "fp-1"

    # Re-dispatching the SAME invocation is idempotent: the join binding is
    # reused (no double counter/binding) and the durable command id is stable.
    dispatch.model_reply_error = None
    before = await ledger.load_by_record_id(parked.call_record_id)
    result = await runtime.dispatch_model_reply(
        inv,
        parent_call_record_id=parked.call_record_id,
        interaction_fingerprint=None,
        signal=NeverCancelled(),
    )
    assert isinstance(result, ToolResult)
    assert result.status == "completed"
    persisted = await ledger.load_by_record_id(parked.call_record_id)
    assert persisted.model_reply_rounds == before.model_reply_rounds
    assert len(persisted.model_reply_joins) == len(before.model_reply_joins)
    command_ids = {command.command_id for command in dispatch.commands}
    assert len(command_ids) == 1


async def test_model_reply_card_contract_failure_terminalizes_parent_and_replay_is_safe():
    dispatch = Dispatch(
        model_reply_error=AgentCardContractError("Agent Card could not be resolved.")
    )
    owner = RuntimeFinalizerFaultHITL("absent")
    runtime, ledger, _, dispatch, _ = await setup(dispatch=dispatch, hitl=owner)
    parked = ledger_record(state="input_required").model_copy(
        update={
            "a2a_task_id": "task-1",
            "a2a_context_id": "context-1",
            "pending_interaction_id": "interaction-1",
            "interaction_revision": 1,
            "interaction_fingerprint": "fp-1",
        }
    )
    assert await ledger.insert(parked) == "accepted"
    join = invocation(call_id="join-1")

    result = await runtime.dispatch_model_reply(
        join,
        parent_call_record_id=parked.call_record_id,
        interaction_fingerprint=None,
        signal=NeverCancelled(),
    )
    terminal = await ledger.load_by_record_id(parked.call_record_id)

    assert isinstance(result, ToolResult)
    assert result.call_id == join.invocation_id
    assert result.status == "failed"
    assert result.error_code == "agent_card_contract_error"
    assert terminal is not None
    assert terminal.state == "failed"
    assert terminal.terminal_result is not None
    assert terminal.terminal_result.call_id == parked.invocation_id
    assert terminal.terminal_result.error_code == result.error_code
    assert terminal.pending_interaction_id == "interaction-1"
    assert owner.abandon_calls == 1
    assert len(dispatch.commands) == 1

    replay = await runtime.dispatch_model_reply(
        join,
        parent_call_record_id=parked.call_record_id,
        interaction_fingerprint=None,
        signal=NeverCancelled(),
    )
    assert replay == result
    assert len(dispatch.commands) == 1
    assert (await ledger.load_by_record_id(parked.call_record_id)).terminal_result == (
        terminal.terminal_result
    )


async def test_model_reply_card_contract_failure_retries_interaction_finalizer_only():
    dispatch = Dispatch(
        model_reply_error=AgentCardContractError("Agent Card could not be resolved.")
    )
    owner = RuntimeFinalizerFaultHITL("outage")
    runtime, ledger, _, dispatch, _ = await setup(dispatch=dispatch, hitl=owner)
    parked = ledger_record(state="input_required").model_copy(
        update={
            "a2a_task_id": "task-1",
            "a2a_context_id": "context-1",
            "pending_interaction_id": "interaction-1",
            "interaction_revision": 1,
            "interaction_fingerprint": "fp-1",
        }
    )
    assert await ledger.insert(parked) == "accepted"
    join = invocation(call_id="join-1")

    first = await runtime.dispatch_model_reply(
        join,
        parent_call_record_id=parked.call_record_id,
        interaction_fingerprint=None,
        signal=NeverCancelled(),
    )
    assert isinstance(first, ToolSuspension)
    assert (await ledger.load_by_record_id(parked.call_record_id)).state == "failed"
    assert len(dispatch.commands) == 1

    replay = await runtime.dispatch_model_reply(
        join,
        parent_call_record_id=parked.call_record_id,
        interaction_fingerprint=None,
        signal=NeverCancelled(),
    )
    assert isinstance(replay, ToolResult)
    assert replay.error_code == "agent_card_contract_error"
    assert owner.abandon_calls == 2
    assert len(dispatch.commands) == 1


async def test_model_reply_accepted_receipt_suspends_without_retry_discriminator():
    """A bare accepted acknowledgement is not a transport failure.

    The Agent accepted the reply and is still working; the response observation
    will arrive asynchronously. ``dispatch_model_reply`` must return a
    ``waiting_external`` suspension with ``delivery_state="accepted"`` so the
    kernel does NOT re-send the delivered message.
    """
    dispatch = Dispatch(
        model_reply_receipt=A2ADispatchReceipt(
            outcome="accepted", task_id="task-1", context_id="context-1"
        )
    )
    runtime, ledger, _, dispatch, _ = await setup(dispatch=dispatch)
    parked = ledger_record(state="input_required").model_copy(
        update={
            "a2a_task_id": "task-1",
            "a2a_context_id": "context-1",
            "pending_interaction_id": "interaction-1",
            "interaction_revision": 1,
            "interaction_fingerprint": "fp-1",
        }
    )
    assert await ledger.insert(parked) == "accepted"
    inv = invocation(call_id="join-1")

    suspension = await runtime.dispatch_model_reply(
        inv,
        parent_call_record_id=parked.call_record_id,
        interaction_fingerprint=None,
        signal=NeverCancelled(),
    )

    assert isinstance(suspension, ToolSuspension)
    assert suspension.status == "waiting_external"
    assert suspension.delivery_state == "accepted"
    assert suspension.call_record_id == parked.call_record_id
    assert suspension.interaction_id == "interaction-1"
    assert suspension.interaction_fingerprint == "fp-1"
    # The Agent is still working: the parent call stays parked (not resuming),
    # the join binding is durable, and exactly one remote message was sent.
    persisted = await ledger.load_by_record_id(parked.call_record_id)
    assert persisted.state == "input_required"
    assert persisted.model_reply_rounds == {"fp-1": 1}
    assert len(persisted.model_reply_joins) == 1
    assert len(dispatch.commands) == 1


async def test_model_reply_redispatch_returns_persisted_outcome_without_resend():
    runtime, ledger, _, dispatch, _ = await setup()
    result_content = ToolResult(
        call_id="call-1",
        tool_name="agent_abc",
        status="completed",
        content=[TextPart(text="done")],
        artifact_refs=[],
        error_code=None,
        error_message=None,
    )
    terminal = ledger_record(state="input_required").model_copy(
        update={
            "state": "completed",
            "a2a_task_id": "task-1",
            "a2a_context_id": "context-1",
            "pending_interaction_id": "interaction-1",
            "interaction_revision": 1,
            "interaction_fingerprint": None,
            "terminal_at": NOW,
            "terminal_result": result_content,
            "terminal_result_digest": sha256(
                result_content.model_dump_json().encode()
            ).hexdigest(),
            "model_reply_joins": [
                A2AJoinBinding(
                    join_invocation_id="join-1",
                    command_id="model-reply-abc",
                    interaction_fingerprint=None,
                    created_at=NOW,
                )
            ],
        }
    )
    assert await ledger.insert(terminal) == "accepted"
    inv = invocation(call_id="join-1")

    commands_before = len(dispatch.commands)
    result = await runtime.dispatch_model_reply(
        inv,
        parent_call_record_id=terminal.call_record_id,
        interaction_fingerprint=None,
        signal=NeverCancelled(),
    )
    assert isinstance(result, ToolResult)
    assert result.status == "completed"
    assert result.error_code is None
    assert result.content == [TextPart(text="done")]
    # The parent already resolved: no remote message was sent.
    assert len(dispatch.commands) == commands_before

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pymongo.errors import AutoReconnect, ServerSelectionTimeoutError

from execution.orchestrator.a2a_runtime.errors import RecoverableAdapterError
from execution.orchestrator.a2a_runtime.ingress import (
    A2AObservationIngress,
    RejectExternalIngressAuthenticator,
)
from execution.orchestrator.a2a_runtime.models import (
    A2ADispatchReceipt,
    NormalizedA2AObservation,
)
from execution.orchestrator.a2a_runtime.recovery import A2ACallRecoveryService
from execution.orchestrator.a2a_runtime.runtime import A2AAgentToolRuntime
from execution.orchestrator.a2a_runtime.terminal_interactions import (
    TerminalInteractionFinalizer,
)
from execution.orchestrator.models import ToolSuspension
from tests.orchestrator_harness.a2a_in_memory import (
    InMemoryAgentCallLedgerStore,
    InMemoryHITLApplicationPort,
    InMemoryObservationConflictStore,
    InMemoryObservationInboxStore,
    InMemoryPreparedInvocationSnapshotReader,
    InMemoryRoomEpochStore,
)

from ._orchestrator_a2a_helpers import invocation, prepared
from ._orchestrator_helpers import NOW, NeverCancelled


class Authorization:
    async def authorize(self, **kwargs):
        return "authorized"


class Checkpoints:
    async def is_acceptance_checkpointed(self, *args):
        return True

    async def is_suspension_checkpointed(self, *args):
        return True


class Resources:
    async def materialize(self, manifest, **kwargs):
        return []

    async def materialize_inbound_artifacts(self, **kwargs):
        return kwargs["artifact_refs"]


class Dispatch:
    def __init__(self, receipt):
        self.receipt = receipt
        self.commands = []

    async def dispatch(self, command):
        self.commands.append(command)
        return self.receipt

    async def inspect(self, command):
        return self.receipt

    async def continue_task(self, command):
        return self.receipt

    async def inspect_continuation(self, command):
        return self.receipt

    async def cancel(self, command):
        return self.receipt

    async def inspect_cancellation(self, command):
        return self.receipt

    def is_command_retry_safe(self, transport_kind):
        return True


class FaultLedger(InMemoryAgentCallLedgerStore):
    def __init__(self, fail_state=None):
        super().__init__()
        self.fail_state = fail_state

    async def cas(self, record, *, expected_state_version):
        if self.fail_state == record.state:
            self.fail_state = None
            raise RecoverableAdapterError(f"injected {record.state} CAS outage")
        return await super().cas(record, expected_state_version=expected_state_version)


class AfterReceiptRenewLedger(InMemoryAgentCallLedgerStore):
    def __init__(self, error):
        super().__init__()
        self.error = error
        self.renew_count = 0

    async def renew(self, *args, **kwargs):
        self.renew_count += 1
        if self.renew_count == 4:
            raise self.error
        return await super().renew(*args, **kwargs)


class PersistedWinnerReloadLedger(InMemoryAgentCallLedgerStore):
    def __init__(self, error):
        super().__init__()
        self.error = error
        self.fail_reload = False

    async def cas(self, record, *, expected_state_version):
        if record.state == "completed":
            self.fail_reload = True
            return "conflict"
        return await super().cas(record, expected_state_version=expected_state_version)

    async def load(self, run_id, invocation_id):
        if self.fail_reload:
            self.fail_reload = False
            raise self.error
        return await super().load(run_id, invocation_id)


class TypedTerminalCasLedger(InMemoryAgentCallLedgerStore):
    def __init__(self, error):
        super().__init__()
        self.error = error

    async def cas(self, record, *, expected_state_version):
        if record.state == "completed":
            raise self.error
        return await super().cas(record, expected_state_version=expected_state_version)


class FaultRecorder:
    def __init__(
        self,
        delegate,
        *,
        fail_record=False,
        fail_mark=False,
        record_error=None,
        mark_error=None,
    ):
        self.delegate = delegate
        self.fail_record = fail_record
        self.fail_mark = fail_mark
        self.record_error = record_error
        self.mark_error = mark_error

    async def record(self, observation):
        if self.record_error is not None:
            error = self.record_error
            self.record_error = None
            raise error
        if self.fail_record:
            self.fail_record = False
            raise RecoverableAdapterError("injected inbox outage")
        return await self.delegate.record(observation)

    async def mark_ledger_applied(self, observation_id):
        await self.delegate.mark_ledger_applied(observation_id)

    async def mark_executor_outcome(self, observation_id, *, outcome_digest):
        if self.mark_error is not None:
            error = self.mark_error
            self.mark_error = None
            raise error
        if self.fail_mark:
            self.fail_mark = False
            raise RecoverableAdapterError("injected outcome-marker outage")
        await self.delegate.mark_executor_outcome(
            observation_id, outcome_digest=outcome_digest
        )


def translated(error):
    recoverable = RecoverableAdapterError("translated persistence outage")
    recoverable.__cause__ = error
    return recoverable


def terminal_receipt():
    observation = NormalizedA2AObservation(
        observation_id="terminal-1",
        call_record_id=None,
        source_kind="direct",
        source_identity="direct:terminal-1",
        binding_scope="endpoint",
        event_kind="terminal",
        status="completed",
        observed_at=NOW,
        task_id="task-1",
        context_id="context-1",
    )
    return A2ADispatchReceipt(outcome="terminal", terminal_observation=observation)


async def setup(
    *,
    receipt,
    ledger=None,
    fail_record=False,
    fail_mark=False,
    record_error=None,
    mark_error=None,
):
    ledger = ledger or FaultLedger()
    snapshots = InMemoryPreparedInvocationSnapshotReader()
    snapshots.put(prepared())
    epochs = InMemoryRoomEpochStore()
    await epochs.activate("room-1", "create-1", activated_at=NOW)
    inbox = InMemoryObservationInboxStore()
    ingress = A2AObservationIngress(
        inbox=inbox,
        conflicts=InMemoryObservationConflictStore(),
        ledger=ledger,
        authenticator=RejectExternalIngressAuthenticator(),
    )
    recorder = FaultRecorder(
        ingress,
        fail_record=fail_record,
        fail_mark=fail_mark,
        record_error=record_error,
        mark_error=mark_error,
    )
    dispatch = Dispatch(receipt)
    runtime = A2AAgentToolRuntime(
        ledger=ledger,
        prepared_reader=snapshots,
        checkpoint_reader=Checkpoints(),
        authorization=Authorization(),
        room_epochs=epochs,
        resources=Resources(),
        dispatch=dispatch,
        observations=recorder,
        terminal_finalizer=TerminalInteractionFinalizer(InMemoryHITLApplicationPort()),
    )
    return runtime, ledger, epochs, ingress, dispatch


async def test_accepted_receipt_ledger_failure_suspends_and_leaves_recovery_state():
    ledger = FaultLedger(fail_state="working")
    runtime, ledger, _, _, dispatch = await setup(
        receipt=A2ADispatchReceipt(
            outcome="accepted", task_id="task-1", context_id="context-1"
        ),
        ledger=ledger,
    )
    accepted = await runtime.accept(invocation())
    outcome = await runtime.execute(invocation(), accepted, signal=NeverCancelled())
    assert isinstance(outcome, ToolSuspension)
    assert len(dispatch.commands) == 1
    assert (await ledger.load("run-1", "call-1")).state == "dispatching"


async def test_terminal_receipt_inbox_failure_never_escapes_execute():
    runtime, ledger, _, ingress, dispatch = await setup(
        receipt=terminal_receipt(), fail_record=True
    )
    accepted = await runtime.accept(invocation())
    outcome = await runtime.execute(invocation(), accepted, signal=NeverCancelled())
    assert isinstance(outcome, ToolSuspension)
    assert len(dispatch.commands) == 1
    assert (await ledger.load("run-1", "call-1")).state == "dispatching"
    assert await ingress.inbox.load("terminal-1") is None


async def test_terminal_ledger_cas_failure_preserves_inboxed_evidence():
    ledger = FaultLedger(fail_state="completed")
    runtime, ledger, _, ingress, _ = await setup(
        receipt=terminal_receipt(), ledger=ledger
    )
    accepted = await runtime.accept(invocation())
    outcome = await runtime.execute(invocation(), accepted, signal=NeverCancelled())
    assert isinstance(outcome, ToolSuspension)
    assert (await ledger.load("run-1", "call-1")).state == "dispatching"
    assert await ingress.inbox.load("terminal-1") is not None


async def test_executor_outcome_mark_failure_suspends_with_terminal_ledger_winner():
    runtime, ledger, _, ingress, _ = await setup(
        receipt=terminal_receipt(), fail_mark=True
    )
    accepted = await runtime.accept(invocation())
    outcome = await runtime.execute(invocation(), accepted, signal=NeverCancelled())
    assert isinstance(outcome, ToolSuspension)
    assert (await ledger.load("run-1", "call-1")).state == "completed"
    assert (await ingress.inbox.load("terminal-1")).delivery_route == "unresolved"


@pytest.mark.parametrize(
    "stage,provider_error",
    [
        ("after_receipt", AutoReconnect("receipt renewal")),
        ("inbox_insert", ServerSelectionTimeoutError("inbox insert")),
        ("ledger_cas", AutoReconnect("terminal CAS")),
        ("winner_reload", ServerSelectionTimeoutError("winner reload")),
        ("outcome_mark", AutoReconnect("outcome mark")),
    ],
)
async def test_independent_production_class_post_effect_outages_suspend(
    stage, provider_error
):
    error = translated(provider_error)
    ledger = None
    setup_kwargs = {}
    receipt = terminal_receipt()
    if stage == "after_receipt":
        ledger = AfterReceiptRenewLedger(error)
        receipt = A2ADispatchReceipt(
            outcome="accepted", task_id="task-1", context_id="context-1"
        )
    elif stage == "inbox_insert":
        setup_kwargs["record_error"] = error
    elif stage == "ledger_cas":
        ledger = TypedTerminalCasLedger(error)
    elif stage == "winner_reload":
        ledger = PersistedWinnerReloadLedger(error)
    else:
        setup_kwargs["mark_error"] = error
    runtime, persisted, _, ingress, dispatch = await setup(
        receipt=receipt, ledger=ledger, **setup_kwargs
    )
    accepted = await runtime.accept(invocation())
    outcome = await runtime.execute(invocation(), accepted, signal=NeverCancelled())
    assert isinstance(outcome, ToolSuspension)
    assert len(dispatch.commands) == 1
    current = await persisted.load("run-1", "call-1")
    assert current is not None
    if stage == "outcome_mark":
        assert current.state == "completed"
        assert await ingress.inbox.load("terminal-1") is not None
    else:
        assert current.state in {"dispatching", "completed"}


async def test_checkpointed_accepted_call_recovery_releases_lease_then_dispatches_once():
    receipt = A2ADispatchReceipt(
        outcome="accepted", task_id="task-1", context_id="context-1"
    )
    runtime, ledger, epochs, ingress, dispatch = await setup(receipt=receipt)
    accepted = await runtime.accept(invocation())
    outcomes = []

    async def concrete_recovery_dispatch(_record):
        outcomes.append(
            await runtime.execute(invocation(), accepted, signal=NeverCancelled())
        )

    recovery = A2ACallRecoveryService(
        ledger=ledger,
        checkpoints=Checkpoints(),
        room_epochs=epochs,
        dispatch=dispatch,
        observations=ingress,
        recover_dispatch=concrete_recovery_dispatch,
        hitl=InMemoryHITLApplicationPort(),
    )
    record = await ledger.load("run-1", "call-1")
    recovered = await recovery.recover_call(record, now=datetime.now(UTC))
    assert recovered
    assert len(dispatch.commands) == 1
    assert len(outcomes) == 1 and isinstance(outcomes[0], ToolSuspension)
    assert (await ledger.load("run-1", "call-1")).state == "working"

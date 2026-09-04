"""Focused tests for the process-local orchestrator session host."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from common.dto import CancellationAck
from delivery.room_events import InMemoryRoomEventStore
from delivery.snapshot import SnapshotService
from execution.adapters.session_host import RoomSessionHost
from execution.orchestrator.a2a_runtime.in_memory import InMemoryRoomEpochStore
from execution.orchestrator.a2a_runtime.observations import (
    RunAddressedToolObservationSink,
)
from execution.orchestrator.budget import BudgetPolicy
from execution.orchestrator.context import ContextCompiler
from execution.orchestrator.fake_tools import (
    RecordingFakeToolRuntime,
    StaticFakeToolCatalog,
)
from execution.orchestrator.in_memory import (
    InMemoryOrchestratorRunStore,
    InMemoryProjectionDriver,
)
from execution.orchestrator.kernel import OrchestratorKernel
from execution.orchestrator.models import (
    FrozenToolCatalogSnapshot,
    ModelStreamEvent,
    ToolObservation,
    ToolResult,
)
from execution.orchestrator.public_projection import PublicProjectionTranslator
from execution.orchestrator.session import DefaultRunFactory, SessionConflict
from execution.orchestrator_routing import DualRuntimeRouter

from ._orchestrator_helpers import (
    NOW,
    ScriptedModelRuntime,
    final_events,
    make_run,
    profile,
    tool_events,
    user_message,
)


@pytest.fixture
def catalog() -> FrozenToolCatalogSnapshot:
    return FrozenToolCatalogSnapshot(catalog_id="catalog-1", entries=[], created_at=NOW)


def _host(
    *,
    run_store,
    epoch_store,
    runtime=None,
    listener=None,
    run_factory=None,
):
    def kernel_for_catalog(_snapshot) -> OrchestratorKernel:
        return OrchestratorKernel(
            run_store=run_store,
            model_runtime=runtime,
            tool_runtime=RecordingFakeToolRuntime(),
            tool_catalog=StaticFakeToolCatalog(),
            context_compiler=ContextCompiler(),
            budget_policy=BudgetPolicy(),
            projection_driver=InMemoryProjectionDriver(run_store),
        )

    return RoomSessionHost(
        kernel_factory=kernel_for_catalog,
        run_store=run_store,
        epoch_store=epoch_store,
        listener=listener,
        run_factory=run_factory,
    )


async def test_session_requires_active_epoch_and_rejects_duplicate_rooms(catalog):
    run_store = InMemoryOrchestratorRunStore()
    epoch_store = InMemoryRoomEpochStore()
    host = _host(run_store=run_store, epoch_store=epoch_store)

    with pytest.raises(SessionConflict, match="epoch is not active"):
        await host.create_session(
            room_id="room-1",
            profile=profile(),
            candidate_scope=make_run().candidate_scope,
            requesting_subject_id="user-1",
            frozen_catalog=catalog,
        )

    await epoch_store.activate("room-1", "create-1", activated_at=NOW)
    first = await host.create_session(
        room_id="room-1",
        profile=profile(),
        candidate_scope=make_run().candidate_scope,
        requesting_subject_id="user-1",
        frozen_catalog=catalog,
    )
    assert host.get_session("room-1") is first
    with pytest.raises(SessionConflict, match="already active"):
        await host.create_session(
            room_id="room-1",
            profile=profile(),
            candidate_scope=make_run().candidate_scope,
            requesting_subject_id="user-1",
            frozen_catalog=catalog,
        )
    host.drop_session("room-1")
    assert host.get_session("room-1") is None
    with pytest.raises(SessionConflict, match="no active session"):
        await host.continue_run("room-1")


async def test_prompt_runs_the_kernel_and_forwards_lifecycle_events(catalog):
    run_store = InMemoryOrchestratorRunStore()
    epoch_store = InMemoryRoomEpochStore()
    await epoch_store.activate("room-1", "create-1", activated_at=NOW)
    events = []

    async def listener(event):
        events.append(event.event_type)

    host = _host(
        run_store=run_store,
        epoch_store=epoch_store,
        runtime=ScriptedModelRuntime([final_events("done")]),
        listener=listener,
    )
    await host.create_session(
        room_id="room-1",
        profile=profile(),
        candidate_scope=make_run().candidate_scope,
        requesting_subject_id="user-1",
        frozen_catalog=catalog,
    )

    result = await host.prompt("room-1", user_message(), client_request_id="req-1")

    assert result.outcome == "final_answer"
    assert {"session_started", "run_started", "run_final_answer_ready"} <= set(events)
    assert "session_idle" in events


async def test_observation_sink_reenters_without_a_session_object(catalog):
    run_store = InMemoryOrchestratorRunStore()
    epoch_store = InMemoryRoomEpochStore()
    await epoch_store.activate("room-1", "create-1", activated_at=NOW)
    observed = []

    async def listener(event):
        observed.append(event)

    host = _host(
        run_store=run_store,
        epoch_store=epoch_store,
        listener=listener,
    )

    sink = host.observation_sink()
    assert sink.lifecycle_factory is not None
    run = make_run()
    lifecycle = sink.lifecycle_factory(run)
    assert lifecycle is not None
    await lifecycle(
        "message_completed",
        run,
        {
            "call_id": "call-1",
            "message_kind": "tool_result",
            "result_status": "completed",
        },
    )
    assert len(observed) == 1
    assert observed[0].run_id == run.run_id
    assert observed[0].room_id == run.room_id
    assert observed[0].payload["result_status"] == "completed"
    assert observed[0].sequence == run.state_version

    await lifecycle("turn_started", run, {})
    await asyncio.sleep(0)
    assert len(observed) == 2
    assert observed[-1].event_type == "turn_started"
    assert observed[-1].lifecycle_family == "canonical"

    legacy = run.model_copy(update={"schema_version": 5, "lifecycle_family": "legacy"})
    legacy_lifecycle = sink.lifecycle_factory(legacy)
    assert legacy_lifecycle is not None
    await legacy_lifecycle("turn_started", legacy, {})
    assert len(observed) == 2
    await legacy_lifecycle(
        "tool_execution_completed",
        legacy,
        {"call_id": "call-1", "result_status": "completed"},
    )
    assert len(observed) == 3
    assert observed[-1].event_type == "tool_execution_completed"
    assert observed[-1].lifecycle_family == "legacy"

    # Re-entry requires an existing Run; a missing Run is a KeyError.
    with pytest.raises(KeyError):
        await host.observation_sink().deliver(
            "run-missing",
            ToolObservation(
                observation_id="obs-1",
                invocation_id="call-1",
                outcome=ToolResult(
                    call_id="call-1",
                    tool_name="agent",
                    status="completed",
                    content=[],
                    artifact_refs=[],
                ),
                observed_at=NOW,
            ),
        )


@pytest.mark.asyncio
async def test_run_addressed_observation_sink_attaches_lifecycle_listener():
    kernel = SimpleNamespace(observe_tool=AsyncMock())
    listener = AsyncMock()
    sink = RunAddressedToolObservationSink(
        run_store=SimpleNamespace(load=AsyncMock(return_value=SimpleNamespace())),
        kernel_factory=lambda _run: kernel,
        signal_factory=lambda: SimpleNamespace(),
        listener=listener,
    )

    observation = ToolObservation(
        observation_id="obs-1",
        invocation_id="call-1",
        outcome=ToolResult(
            call_id="call-1",
            tool_name="agent",
            status="completed",
            content=[],
            artifact_refs=[],
        ),
        observed_at=NOW,
    )

    await sink.deliver("run-1", observation)

    kwargs = kernel.observe_tool.await_args.kwargs
    assert callable(kwargs["lifecycle"])

    run = make_run()
    await kwargs["lifecycle"](
        "message_completed",
        run,
        {"call_id": "call-1", "message_kind": "tool_result"},
    )

    await asyncio.sleep(0)
    listener.assert_awaited_once()
    forwarded = listener.await_args.args[0]
    assert forwarded.event_type == "message_completed"
    assert forwarded.room_id == run.room_id
    assert forwarded.run_id == run.run_id


async def test_no_hosted_session_canonical_observation_converges_public_lifecycle(
    catalog,
):
    run_store = InMemoryOrchestratorRunStore()
    room_events = InMemoryRoomEventStore()
    epoch_store = InMemoryRoomEpochStore()
    await epoch_store.activate("room-1", "create-1", activated_at=NOW)
    translator = PublicProjectionTranslator(lifecycle_family="canonical")
    published_types: list[str] = []

    async def listener(event):
        public = translator.translate(event)
        if public is None:
            return
        await room_events.append(
            room_id=public.room_id,
            kind="run_event",
            payload_public={
                "event_id": public.event_id,
                "run_id": public.run_id,
                "seq": public.seq,
                "type": public.kind,
                "payload": public.payload,
                "correlation_id": public.client_request_id,
            },
            event_id=public.event_id,
            run_id=public.run_id,
            ts=NOW,
        )
        published_types.append(public.kind)

    host = _host(
        run_store=run_store,
        epoch_store=epoch_store,
        runtime=ScriptedModelRuntime(
            [
                tool_events(
                    ("wait", "fake_agent_pause", '{"status":"waiting_external"}')
                ),
                final_events("recovered final"),
            ]
        ),
        listener=listener,
        run_factory=DefaultRunFactory(),
    )
    await host.create_session(
        room_id="room-1",
        profile=profile(),
        candidate_scope=make_run().candidate_scope,
        requesting_subject_id="user-1",
        frozen_catalog=catalog,
    )
    waiting = await host.prompt("room-1", user_message(), client_request_id="request-1")
    assert waiting.outcome == "waiting_external"
    host.drop_session("room-1")

    await host.observation_sink().deliver(
        waiting.run.run_id,
        ToolObservation(
            observation_id="observation-after-session-loss",
            invocation_id="wait",
            outcome=ToolResult(
                call_id="wait",
                tool_name="fake_agent_pause",
                status="completed",
                content=[],
                artifact_refs=[],
            ),
            observed_at=NOW,
        ),
    )
    completed = await run_store.load(waiting.run.run_id)
    assert completed is not None and completed.status == "completed"
    tool_end = published_types.index("tool_execution_end")
    first_turn_end = published_types.index("turn_end", tool_end)
    final_turn_end = len(published_types) - 1 - published_types[::-1].index("turn_end")
    assert tool_end < first_turn_end < final_turn_end

    # The Kernel marks the public terminal only after the awaited listener has
    # durably appended it to room_events.
    saved = await run_store.load(waiting.run.run_id)
    assert saved is not None
    entry = saved.tool_batches[0].entries[0]
    assert entry.public_terminal_emitted is True
    records = await room_events.read_range("room-1", include_skipped=True)
    assert any(
        row["payload_public"].get("type") == "tool_execution_end" for row in records
    )

    final_message_id = saved.proposed_final_message_id
    assert final_message_id is not None
    await room_events.append(
        room_id="room-1",
        kind="agent_response",
        payload_public={
            "message_id": final_message_id,
            "agent_id": "system:hybro",
            "content": "recovered final",
            "client_request_id": "request-1",
            "related_message_id": "user-1",
        },
        event_id=f"agent-response:{final_message_id}",
        run_id=saved.run_id,
        ts=NOW,
    )
    await room_events.append(
        room_id="room-1",
        kind="run_event",
        payload_public={
            "event_id": f"public:{saved.run_id}:run_settled",
            "run_id": saved.run_id,
            "seq": saved.state_version,
            "type": "run_settled",
            "payload": {
                "status": "completed",
                "started_at": saved.created_at,
                "settled_at": saved.updated_at,
                "duration_ms": 0,
                "final_message_id": final_message_id,
            },
            "correlation_id": saved.client_request_id,
        },
        event_id=f"public:{saved.run_id}:run_settled",
        run_id=saved.run_id,
        ts=NOW,
    )
    snapshot = await SnapshotService(store=room_events).snapshot("room-1", force=True)
    turn = snapshot["turns"][0]
    assert turn["state"] == "completed"
    assert all(item["status"] != "active" for item in turn["internal_turns"])
    assert all(item.get("status") != "running" for item in turn["activity"])


async def test_no_hosted_session_does_not_checkpoint_public_terminal_before_ack(
    catalog,
):
    run_store = InMemoryOrchestratorRunStore()
    epoch_store = InMemoryRoomEpochStore()
    await epoch_store.activate("room-1", "create-1", activated_at=NOW)
    reject_tool_terminal = False

    async def listener(event):
        if reject_tool_terminal and event.event_type == "tool_execution_completed":
            raise RuntimeError("durable publish rejected")

    host = _host(
        run_store=run_store,
        epoch_store=epoch_store,
        runtime=ScriptedModelRuntime(
            [
                tool_events(
                    ("wait", "fake_agent_pause", '{"status":"waiting_external"}')
                ),
                final_events("must not run"),
            ]
        ),
        listener=listener,
        run_factory=DefaultRunFactory(),
    )
    await host.create_session(
        room_id="room-1",
        profile=profile(),
        candidate_scope=make_run().candidate_scope,
        requesting_subject_id="user-1",
        frozen_catalog=catalog,
    )
    waiting = await host.prompt("room-1", user_message(), client_request_id="request-1")
    host.drop_session("room-1")
    reject_tool_terminal = True

    with pytest.raises(RuntimeError, match="durable publish rejected"):
        await host.observation_sink().deliver(
            waiting.run.run_id,
            ToolObservation(
                observation_id="unacknowledged-observation",
                invocation_id="wait",
                outcome=ToolResult(
                    call_id="wait",
                    tool_name="fake_agent_pause",
                    status="completed",
                    content=[],
                    artifact_refs=[],
                ),
                observed_at=NOW,
            ),
        )

    saved = await run_store.load(waiting.run.run_id)
    assert saved is not None
    assert saved.tool_batches[0].entries[0].public_terminal_emitted is False
    assert saved.status == "running"


class CancellationAwareBlockingModelRuntime:
    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def stream_turn(self, request, *, signal):
        self.started.set()
        yield ModelStreamEvent(kind="attempt_started", attempt=1)
        await signal.wait()
        yield ModelStreamEvent(
            kind="attempt_failed",
            attempt=1,
            error_class="aborted",
            retryable=False,
        )
        yield ModelStreamEvent(
            kind="error",
            attempt=1,
            error_class="aborted",
            retryable=False,
            error_code="aborted",
        )


async def test_router_user_cancellation_terminalizes_live_canonical_run(catalog):
    run_store = InMemoryOrchestratorRunStore()
    epoch_store = InMemoryRoomEpochStore()
    await epoch_store.activate("room-1", "create-1", activated_at=NOW)
    blocking = CancellationAwareBlockingModelRuntime()
    events = []

    async def listener(event):
        events.append(event)

    host = _host(
        run_store=run_store,
        epoch_store=epoch_store,
        runtime=blocking,
        listener=listener,
        run_factory=DefaultRunFactory(),
    )
    await host.create_session(
        room_id="room-1",
        profile=profile(),
        candidate_scope=make_run().candidate_scope,
        requesting_subject_id="user-1",
        frozen_catalog=catalog,
    )

    class CancellationCoordinator:
        async def cancel_run(self, run_id, *, reason, deletion_id=None):
            assert reason == "user:user-1"
            assert deletion_id is None
            return {f"call:{run_id}": "canceled"}

    router = DualRuntimeRouter(
        runtime=type(
            "Runtime",
            (),
            {
                "run_store": run_store,
                "session_host": host,
                "cancellation_coordinator": CancellationCoordinator(),
            },
        )()
    )
    prompt_task = asyncio.create_task(
        host.prompt("room-1", user_message(), client_request_id="request-1")
    )
    await blocking.started.wait()

    results = await router.route_cancellation_by_user_message(
        "user-1", reason="user:user-1"
    )
    result = await prompt_task

    assert results == CancellationAck(
        status="canceled", cancellation_applied=True, reconciled=True
    )
    assert result.outcome == "cancellation_pending"
    run = next(iter(run_store.runs.values()))
    assert run.status == "canceled"
    assert run.active_internal_turn_id is None
    assert any(
        event.event_type == "message_completed"
        and event.payload.get("disposition") == "aborted"
        for event in events
    )
    assert any(
        event.event_type == "turn_completed"
        and event.payload.get("status") == "aborted"
        for event in events
    )


async def test_interrupt_requires_durable_cancellation_before_reconciliation(catalog):
    run_store = InMemoryOrchestratorRunStore()
    epoch_store = InMemoryRoomEpochStore()
    await epoch_store.activate("room-1", "create-1", activated_at=NOW)
    blocking = CancellationAwareBlockingModelRuntime()
    events = []

    async def listener(event):
        events.append(event)

    host = _host(
        run_store=run_store,
        epoch_store=epoch_store,
        runtime=blocking,
        listener=listener,
        run_factory=DefaultRunFactory(),
    )
    await host.create_session(
        room_id="room-1",
        profile=profile(),
        candidate_scope=make_run().candidate_scope,
        requesting_subject_id="user-1",
        frozen_catalog=catalog,
    )

    prompt_task = asyncio.create_task(
        host.prompt("room-1", user_message(), client_request_id="request-1")
    )
    await blocking.started.wait()
    run = next(iter(run_store.runs.values()))
    command_id = f"cancel:{run.run_id}:user_requested"
    requested = await run_store.request_cancellation(
        run.run_id,
        expected_state_version=run.state_version,
        command_id=command_id,
        cause="user_requested",
        requested_at=NOW,
    )
    assert requested.run is not None
    await host.interrupt_run(requested.run, command_id)
    result = await prompt_task
    assert result.outcome == "cancellation_pending"
    pending = await run_store.load(run.run_id)
    assert pending is not None and pending.status == "canceling"

    await host.reconcile_cancellation(pending)
    saved = await run_store.load(run.run_id)
    assert saved is not None and saved.status == "canceled"
    assert saved.active_internal_turn_id is None
    await asyncio.sleep(0)
    assert any(
        event.event_type == "message_completed"
        and event.payload.get("disposition") == "aborted"
        for event in events
    ), [(event.event_type, event.payload) for event in events]
    assert any(
        event.event_type == "turn_completed"
        and event.payload.get("status") == "aborted"
        for event in events
    )
    assert not any(event.event_type == "run_canceled" for event in events)


class BlockingModelRuntime:
    """Streams an attempt start and then blocks until the task is cancelled."""

    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def stream_turn(self, request, *, signal):
        self.started.set()
        yield ModelStreamEvent(kind="attempt_started", attempt=1)
        await asyncio.Event().wait()


async def test_shutdown_cancels_tasks_without_persisting_terminal_state(catalog):
    run_store = InMemoryOrchestratorRunStore()
    epoch_store = InMemoryRoomEpochStore()
    await epoch_store.activate("room-1", "create-1", activated_at=NOW)
    blocking = BlockingModelRuntime()
    host = _host(
        run_store=run_store,
        epoch_store=epoch_store,
        runtime=blocking,
    )
    await host.create_session(
        room_id="room-1",
        profile=profile(),
        candidate_scope=make_run().candidate_scope,
        requesting_subject_id="user-1",
        frozen_catalog=catalog,
    )

    prompt_task = asyncio.create_task(
        host.prompt("room-1", user_message(), client_request_id="request-1")
    )
    await blocking.started.wait()

    await host.shutdown()
    with pytest.raises(asyncio.CancelledError):
        await prompt_task

    # The Run stays non-terminal so recovery workers can re-enter it.
    run_id = run_store.runs
    assert run_id
    run = next(iter(run_id.values()))
    assert run.status == "running"
    assert run.recovery_claim.next_attempt_at is not None
    assert run.recovery_claim.next_attempt_at < run.budget.deadline_at

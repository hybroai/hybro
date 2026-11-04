from __future__ import annotations

import asyncio
import logging

import pytest

from execution.orchestrator.budget import BudgetExceeded, BudgetPolicy
from execution.orchestrator.compaction import DeterministicFakeCompactor
from execution.orchestrator.context import CompiledContext
from execution.orchestrator.kernel import KernelConflict
from execution.orchestrator.models import (
    AssistantMessage,
    ModelMessage,
    ModelStreamEvent,
    ModelTextPart,
    SessionNotice,
    TextPart,
    ToolBatchEntry,
    ToolCall,
    ToolCallBatch,
    ToolInvocation,
    ToolObservation,
    ToolResult,
    ToolResultMessage,
    ToolSuspension,
    UsageRecord,
)
from execution.orchestrator.public_projection import canonical_settlement_payload
from tests._orchestrator_helpers import (
    NOW,
    EventCancellationSignal,
    NeverCancelled,
    final_events,
    make_kernel,
    make_run,
    tool_events,
)
from tests.orchestrator_harness.fake_tools import RecordingFakeToolRuntime
from tests.orchestrator_harness.in_memory import (
    InMemoryOrchestratorRunStore,
    InMemoryRunStoreResult,
)


@pytest.mark.asyncio
async def test_kernel_completes_final_answer_without_tools():
    kernel, store, runtime, _ = await make_kernel([final_events("answer")])
    run_id = next(iter(store.runs))
    result = await kernel.run(run_id, signal=NeverCancelled())
    assert result.outcome == "final_answer"
    assert result.run.status == "completed"
    assert result.run.projection_state == "settled"
    assert runtime.requests[0].tools


@pytest.mark.asyncio
async def test_finalizing_run_resumes_terminal_commit_without_new_model_turn():
    run = make_run()
    assistant = AssistantMessage(
        message_id="pending-final",
        content=[TextPart(text="durable answer")],
        tool_calls=[],
        finish_reason="stop",
        usage=None,
        created_at=NOW,
    )
    run = run.model_copy(
        update={
            "status": "finalizing",
            "proposed_final_message_id": assistant.message_id,
            "transcript": [*run.transcript, assistant],
        }
    )
    kernel, _, runtime, _ = await make_kernel([], run=run)

    result = await kernel.run(run.run_id, signal=NeverCancelled())

    assert result.outcome == "final_answer"
    assert result.run.status == "completed"
    assert result.run.proposed_final_message_id == assistant.message_id
    assert runtime.requests == []
    assert [
        item.message_id
        for item in result.run.transcript
        if isinstance(item, AssistantMessage)
    ] == [assistant.message_id]


@pytest.mark.asyncio
async def test_process_restart_adopts_checkpointed_final_terminal_before_settlement():
    run = make_run()
    assistant = AssistantMessage(
        message_id="assistant-restart-final",
        content=[TextPart(text="durable final")],
        tool_calls=[],
        finish_reason="stop",
        usage=None,
        created_at=NOW,
    )
    run = run.model_copy(
        update={
            "transcript": [*run.transcript, assistant],
            "active_internal_turn_id": "turn-restart",
            "active_assistant_message_id": assistant.message_id,
            "active_attempt": 1,
            "active_public_text": "durable final",
            "greatest_public_text_offset": len("durable final"),
            "proposed_final_message_id": assistant.message_id,
        }
    )
    kernel, _, runtime, _ = await make_kernel([], run=run)
    lifecycle_events: list[str] = []

    async def lifecycle(event_type, _run, _payload):
        lifecycle_events.append(event_type)

    result = await kernel.run(
        run.run_id,
        signal=NeverCancelled(),
        lifecycle=lifecycle,
    )

    assert result.outcome == "final_answer"
    assert runtime.requests == []
    assert lifecycle_events[:4] == [
        "turn_started",
        "message_started",
        "message_completed",
        "turn_completed",
    ]
    assert result.run.active_internal_turn_id is None
    assert result.run.active_assistant_message_id is None


@pytest.mark.asyncio
async def test_recovery_adopts_durable_final_after_active_assistant_id_was_cleared():
    run = make_run()
    assistant = AssistantMessage(
        message_id="assistant-durable-final",
        content=[TextPart(text="durable final")],
        tool_calls=[],
        finish_reason="stop",
        usage=None,
        created_at=NOW,
    )
    run = run.model_copy(
        update={
            "transcript": [*run.transcript, assistant],
            "active_internal_turn_id": "turn-durable",
            "active_assistant_message_id": None,
            "active_attempt": 1,
            "proposed_final_message_id": assistant.message_id,
        }
    )
    kernel, _, runtime, _ = await make_kernel([], run=run)

    async def read_events(_conversation_id, _run_id):
        return [
            {
                "room_seq": 1,
                "payload_public": {
                    "run_id": run.run_id,
                    "type": "message_start",
                    "payload": {
                        "internal_turn_id": "turn-durable",
                        "message_id": assistant.message_id,
                    },
                },
            },
            {
                "room_seq": 2,
                "payload_public": {
                    "run_id": run.run_id,
                    "type": "message_end",
                    "payload": {
                        "internal_turn_id": "turn-durable",
                        "message_id": assistant.message_id,
                        "disposition": "final",
                        "stop_reason": "stop",
                        "text": "durable final",
                    },
                },
            },
        ]

    kernel.canonical_event_reader = read_events
    events: list[str] = []

    async def lifecycle(event_type, _run, _payload):
        events.append(event_type)

    result = await kernel.run(run.run_id, signal=NeverCancelled(), lifecycle=lifecycle)

    assert result.outcome == "final_answer"
    assert runtime.requests == []
    assert events == ["turn_started", "turn_completed"]


@pytest.mark.asyncio
async def test_recovery_repairs_stale_offset_from_durable_chunks_before_aborting():
    run = make_run()
    run = run.model_copy(
        update={
            "active_internal_turn_id": "turn-stale-offset",
            "active_assistant_message_id": "assistant-stale-offset",
            "active_attempt": 1,
            "active_public_text": "stale",
            "greatest_public_text_offset": 5,
        }
    )
    kernel, _, runtime, _ = await make_kernel([final_events("recovered")], run=run)

    async def read_events(_conversation_id, _run_id):
        return [
            {
                "room_seq": 1,
                "payload_public": {
                    "run_id": run.run_id,
                    "type": "message_start",
                    "payload": {
                        "internal_turn_id": "turn-stale-offset",
                        "message_id": "assistant-stale-offset",
                    },
                },
            },
            {
                "room_seq": 2,
                "payload_public": {
                    "run_id": run.run_id,
                    "type": "message_update",
                    "payload": {
                        "internal_turn_id": "turn-stale-offset",
                        "message_id": "assistant-stale-offset",
                        "assistant_message_event": {
                            "delta": "durable text",
                            "start_offset": 0,
                            "end_offset": 12,
                        },
                    },
                },
            },
        ]

    kernel.canonical_event_reader = read_events
    observed: list[tuple[str, dict[str, object]]] = []

    async def lifecycle(event_type, _run, payload):
        observed.append((event_type, payload))

    result = await kernel.run(run.run_id, signal=NeverCancelled(), lifecycle=lifecycle)

    aborted = next(
        payload
        for event_type, payload in observed
        if event_type == "message_completed" and payload.get("disposition") == "aborted"
    )
    assert aborted["text"] == "durable text"
    assert result.outcome == "final_answer"
    assert len(runtime.requests) == 1


@pytest.mark.asyncio
async def test_recovery_publishes_checkpointed_accepted_tool_terminal_before_settlement():
    kernel, store, _, _ = await make_kernel(
        [tool_events(("call-crash", "fake_agent_echo", '{"value":"ok"}'))]
    )
    run_id = next(iter(store.runs))
    crashed = False

    async def crash_after_private_terminal(event_type, _run, _payload):
        nonlocal crashed
        if event_type == "tool_execution_completed" and not crashed:
            crashed = True
            raise OSError("crash before public terminal checkpoint")

    with pytest.raises(OSError, match="public terminal checkpoint"):
        await kernel.run(
            run_id,
            signal=NeverCancelled(),
            lifecycle=crash_after_private_terminal,
        )
    checkpoint = await store.load(run_id)
    assert checkpoint is not None
    entry = checkpoint.tool_batches[0].entries[0]
    assert entry.state == "terminal"
    assert entry.acceptance is not None
    assert entry.public_terminal_emitted is False

    from tests._orchestrator_helpers import ScriptedModelRuntime

    runtime = ScriptedModelRuntime([final_events("recovered")])
    kernel.model_runtime = runtime
    published: list[str] = []

    async def lifecycle(event_type, _run, _payload):
        published.append(event_type)

    result = await kernel.run(
        run_id,
        signal=NeverCancelled(),
        lifecycle=lifecycle,
    )
    assert result.outcome == "final_answer"
    assert runtime.requests
    assert published[0] == "tool_execution_completed"
    recovered_entry = result.run.tool_batches[0].entries[0]
    assert recovered_entry.public_terminal_emitted is True


@pytest.mark.asyncio
async def test_tool_calling_assistant_and_pending_batch_checkpoint_atomically():
    kernel, store, _, _ = await make_kernel([])
    run = next(iter(store.runs.values()))
    assistant = AssistantMessage(
        message_id="assistant-tools",
        content=[],
        tool_calls=[
            ToolCall(
                call_id="call-atomic",
                tool_name="fake_agent_echo",
                arguments={"value": "ok"},
            )
        ],
        finish_reason="tool_calls",
        usage=None,
        created_at=NOW,
    )

    stored = await kernel._append_assistant(run, assistant)

    assert stored.transcript[-1] == assistant
    assert len(stored.tool_batches) == 1
    assert stored.tool_batches[0].assistant_message_id == assistant.message_id
    assert stored.tool_batches[0].entries[0].call_id == "call-atomic"
    assert stored.tool_batches[0].entries[0].state == "pending"


@pytest.mark.asyncio
async def test_running_run_reconstructs_missing_tool_batch_after_crash():
    run = make_run()
    assistant = AssistantMessage(
        message_id="assistant-torn",
        content=[],
        tool_calls=[
            ToolCall(
                call_id="call-torn",
                tool_name="fake_agent_echo",
                arguments={"value": "recovered"},
            )
        ],
        finish_reason="tool_calls",
        usage=None,
        created_at=NOW,
    )
    run = run.model_copy(update={"transcript": [*run.transcript, assistant]})
    kernel, _, runtime, tools = await make_kernel([final_events()], run=run)

    result = await kernel.run(run.run_id, signal=NeverCancelled())

    assert result.outcome == "final_answer"
    assert tools.accept_log == ["call-torn"]
    assert tools.execute_log == ["call-torn"]
    assert result.run.tool_batches[0].results_flushed is True
    assert len(runtime.requests) == 1


@pytest.mark.asyncio
async def test_reconstructed_tool_batch_can_suspend_and_accept_observation():
    run = make_run()
    assistant = AssistantMessage(
        message_id="assistant-torn-pause",
        content=[],
        tool_calls=[
            ToolCall(
                call_id="call-torn-pause",
                tool_name="fake_agent_pause",
                arguments={"status": "waiting_external"},
            )
        ],
        finish_reason="tool_calls",
        usage=None,
        created_at=NOW,
    )
    run = run.model_copy(update={"transcript": [*run.transcript, assistant]})
    kernel, _, runtime, _ = await make_kernel([final_events()], run=run)
    lifecycle_events: list[tuple[str, dict[str, object]]] = []

    async def lifecycle(event_type, _run, payload):
        lifecycle_events.append((event_type, payload))

    waiting = await kernel.run(run.run_id, signal=NeverCancelled(), lifecycle=lifecycle)
    assert waiting.outcome == "waiting_external"
    assert waiting.run.status == "waiting_external"

    result = await kernel.observe_tool(
        run.run_id,
        ToolObservation(
            observation_id="torn-complete",
            invocation_id="call-torn-pause",
            outcome=ToolResult(
                call_id="call-torn-pause",
                tool_name="fake_agent_pause",
                status="completed",
                content=[TextPart(text="done")],
                artifact_refs=[],
            ),
            observed_at=NOW,
        ),
        signal=NeverCancelled(),
        lifecycle=lifecycle,
    )

    assert result.outcome == "final_answer"
    assert len(runtime.requests) == 1
    tool_events_seen = [
        event_type
        for event_type, payload in lifecycle_events
        if payload.get("call_id") == "call-torn-pause"
    ]
    assert tool_events_seen.count("tool_execution_updated") == 1
    assert tool_events_seen.count("tool_execution_completed") == 1
    assert tool_events_seen.index("tool_execution_updated") < tool_events_seen.index(
        "tool_execution_completed"
    )
    event_names = [event_type for event_type, _ in lifecycle_events]
    resumed_end = event_names.index("tool_execution_completed")
    prior_turn_end = event_names.index("turn_completed", resumed_end)
    successor_turn_start = event_names.index("turn_started", prior_turn_end)
    assert resumed_end < prior_turn_end < successor_turn_start


@pytest.mark.asyncio
async def test_kernel_sanitizes_configured_secret_before_final_checkpoint():
    kernel, store, _, _ = await make_kernel([final_events("echo top-secret")])
    kernel.public_secret_values = ("top-secret",)
    result = await kernel.run(next(iter(store.runs)), signal=NeverCancelled())
    final = next(
        item
        for item in result.run.transcript
        if isinstance(item, AssistantMessage) and not item.tool_calls
    )
    assert final.content[0].text == "echo [REDACTED]"


@pytest.mark.asyncio
async def test_short_safe_answer_timer_update_precedes_message_end_when_provider_stalls():
    class StallingRuntime:
        async def stream_turn(self, request, *, signal):
            del request, signal
            yield ModelStreamEvent(kind="attempt_started", attempt=1)
            yield ModelStreamEvent(kind="text_delta", attempt=1, delta="short answer")
            await asyncio.sleep(0.09)
            yield ModelStreamEvent(kind="finish", attempt=1, finish_reason="stop")

    run = make_run()
    store = InMemoryOrchestratorRunStore()
    kernel, _, _, _ = await make_kernel([], run=run, run_store=store)
    kernel.model_runtime = StallingRuntime()
    observed: list[tuple[str, dict[str, object]]] = []

    async def lifecycle(event_type, _run, payload):
        observed.append((event_type, payload))

    result = await kernel.run(run.run_id, signal=NeverCancelled(), lifecycle=lifecycle)

    assert result.outcome == "final_answer"
    names = [name for name, _ in observed]
    update_at = names.index("message_updated")
    end_at = names.index("message_completed")
    assert update_at < end_at
    assert observed[update_at][1]["delta"] == "short answer"


@pytest.mark.asyncio
async def test_kernel_accepts_before_execute_and_feeds_result_to_next_turn():
    kernel, store, runtime, tools = await make_kernel(
        [
            tool_events(("call-1", "fake_agent_echo", '{"value":"hello"}')),
            final_events(),
        ]
    )
    result = await kernel.run(next(iter(store.runs)), signal=NeverCancelled())
    assert result.outcome == "final_answer"
    assert tools.accept_log == ["call-1"]
    assert tools.execute_log == ["call-1"]
    assert runtime.requests[1].messages[-1].role == "tool"
    assert runtime.requests[1].messages[-1].content[0].tool_name == "fake_agent_echo"
    assert result.run.tool_batches[0].results_flushed is True


@pytest.mark.asyncio
async def test_kernel_correlates_unknown_and_schema_invalid_calls_without_execution():
    kernel, store, runtime, tools = await make_kernel(
        [
            tool_events(
                ("unknown", "not_registered", "{}"),
                ("invalid", "fake_agent_pause", "{}"),
                ("ok", "fake_agent_echo", '{"value":1,"structured":true}'),
            ),
            final_events(),
        ]
    )
    result = await kernel.run(next(iter(store.runs)), signal=NeverCancelled())
    messages = [
        item for item in result.run.transcript if isinstance(item, ToolResultMessage)
    ]
    assert [item.call_id for item in messages] == ["unknown", "invalid", "ok"]
    assert [item.error_code for item in messages] == [
        "invalid_tool_call",
        "skipped_due_to_prior_rejection",
        "skipped_due_to_prior_rejection",
    ]
    assert tools.execute_log == []
    assert len(runtime.requests) == 2
    assert runtime.requests[1].messages[-1].role == "tool"
    assert result.outcome == "final_answer"


@pytest.mark.asyncio
async def test_schema_invalid_agent_call_gets_actionable_correction_turn():
    kernel, store, runtime, tools = await make_kernel(
        [
            tool_events(
                (
                    "bad-resource",
                    "fake_agent_echo",
                    '{"value":"submission","attachment_refs":["pdf-1"]}',
                )
            ),
            final_events("retried without unsupported attachment"),
        ]
    )
    lifecycle_events: list[tuple[str, dict[str, object]]] = []

    async def lifecycle(event_type, _run, payload):
        lifecycle_events.append((event_type, payload))

    result = await kernel.run(
        next(iter(store.runs)), signal=NeverCancelled(), lifecycle=lifecycle
    )

    assert result.outcome == "final_answer"
    assert tools.accept_log == tools.execute_log == []
    assert len(runtime.requests) == 2
    rejected = next(
        item for item in result.run.transcript if isinstance(item, ToolResultMessage)
    )
    assert rejected.error_code == "invalid_tool_call"
    assert "unsupported properties: attachment_refs" in rejected.error_message
    assert runtime.requests[1].messages[-1].role == "tool"
    assert [
        payload["status"]
        for event_type, payload in lifecycle_events
        if event_type == "turn_completed"
    ] == ["error", "completed"]


@pytest.mark.asyncio
async def test_provider_prefetch_is_canceled_and_stream_closed_on_processing_failure():
    class BlockingPrefetchStream:
        def __init__(self) -> None:
            self.calls = 0
            self.prefetch_started = asyncio.Event()
            self.prefetch_canceled = False
            self.closed = False

        def __aiter__(self):
            return self

        async def __anext__(self):
            self.calls += 1
            if self.calls == 1:
                return ModelStreamEvent(kind="attempt_started", attempt=1)
            self.prefetch_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.prefetch_canceled = True
                raise
            raise AssertionError("blocking provider pull unexpectedly completed")

        async def aclose(self) -> None:
            self.closed = True

    class TrackingRuntime:
        def __init__(self, stream: BlockingPrefetchStream) -> None:
            self.stream = stream

        def stream_turn(self, request, *, signal):
            del request, signal
            return self.stream

    stream = BlockingPrefetchStream()
    kernel, store, _, _ = await make_kernel([])
    kernel.model_runtime = TrackingRuntime(stream)

    async def fail_while_prefetch_is_running(run, turn_id, event, *, message_id=None):
        del run, turn_id, event, message_id
        await stream.prefetch_started.wait()
        raise RuntimeError("durable event processing failed")

    kernel._record_model_event = fail_while_prefetch_is_running

    with pytest.raises(RuntimeError, match="durable event processing failed"):
        await kernel.run(next(iter(store.runs)), signal=NeverCancelled())

    assert stream.prefetch_canceled is True
    assert stream.closed is True


@pytest.mark.asyncio
async def test_malformed_tool_call_adds_stable_notice_and_never_invokes_tools():
    malformed = [
        ModelStreamEvent(kind="attempt_started", attempt=1),
        ModelStreamEvent(
            kind="tool_call_start", call_id="bad", tool_name="fake_agent_echo"
        ),
        ModelStreamEvent(kind="tool_call_arguments_delta", call_id="bad", delta="{"),
        ModelStreamEvent(kind="finish", finish_reason="tool_calls"),
    ]
    kernel, store, _, tools = await make_kernel([malformed, final_events()])
    result = await kernel.run(next(iter(store.runs)), signal=NeverCancelled())
    notices = [
        item for item in result.run.transcript if isinstance(item, SessionNotice)
    ]
    assert len(notices) == 1
    assert notices[0].related_call_id == "bad"
    assert tools.accept_log == tools.execute_log == []


@pytest.mark.asyncio
async def test_invalid_first_declaration_fail_fast_skips_later_siblings():
    kernel, store, runtime, tools = await make_kernel(
        [
            tool_events(
                ("bad", "fake_agent_echo", "{}"),
                ("done", "fake_agent_echo", '{"value":"ok"}'),
                ("wait", "fake_agent_pause", '{"status":"waiting_external"}'),
            ),
            final_events("resumed"),
        ]
    )
    run_id = next(iter(store.runs))
    result = await kernel.run(run_id, signal=NeverCancelled())
    assert result.outcome == "final_answer"
    results = [
        item for item in result.run.transcript if isinstance(item, ToolResultMessage)
    ]
    assert [item.call_id for item in results] == ["bad", "done", "wait"]
    assert [item.error_code for item in results] == [
        "invalid_tool_call",
        "skipped_due_to_prior_rejection",
        "skipped_due_to_prior_rejection",
    ]
    assert tools.execute_log == []
    assert len(runtime.requests) == 2


@pytest.mark.asyncio
async def test_replaying_completed_run_after_corrected_declaration_is_idempotent():
    kernel, store, runtime, _ = await make_kernel(
        [
            tool_events(("bad", "fake_agent_echo", "{}")),
            final_events("corrected"),
        ]
    )
    run_id = next(iter(store.runs))
    result = await kernel.run(run_id, signal=NeverCancelled())
    assert result.outcome == "final_answer"
    replay = await kernel.run(run_id, signal=NeverCancelled())
    replay_results = [
        item for item in replay.run.transcript if isinstance(item, ToolResultMessage)
    ]
    assert len(replay_results) == 1
    assert len(runtime.requests) == 2


@pytest.mark.asyncio
async def test_cas_conflict_after_acceptance_reconciles_without_duplicate_execution():
    class ConflictOnceStore(InMemoryOrchestratorRunStore):
        conflicted = False

        async def cas_mutate(self, run, *, expected_state_version, command_id):
            if command_id.startswith("accepted-tool:") and not self.conflicted:
                self.conflicted = True
                current = self.runs[run.run_id]
                self.runs[run.run_id] = current.model_copy(
                    update={"state_version": current.state_version + 1}
                )
                return InMemoryRunStoreResult("conflict", self.runs[run.run_id])
            return await super().cas_mutate(
                run,
                expected_state_version=expected_state_version,
                command_id=command_id,
            )

    store = ConflictOnceStore()
    kernel, store, _, tools = await make_kernel(
        [tool_events(("call-1", "fake_agent_echo", '{"value":"ok"}')), final_events()],
        run_store=store,
    )
    result = await kernel.run(next(iter(store.runs)), signal=NeverCancelled())
    assert result.outcome == "final_answer"
    assert tools.accept_log == ["call-1"]
    assert tools.execute_log == ["call-1"]


@pytest.mark.asyncio
async def test_stale_tool_entry_writer_cannot_overwrite_terminal_result():
    winner = ToolResult(
        call_id="call-1",
        tool_name="fake_agent_echo",
        status="completed",
        content=[TextPart(text="winner")],
        artifact_refs=[],
    )

    class InterveningWinnerStore(InMemoryOrchestratorRunStore):
        conflicted = False

        async def cas_mutate(self, run, *, expected_state_version, command_id):
            if command_id == "stale-writer" and not self.conflicted:
                self.conflicted = True
                current = self.runs[run.run_id]
                batch = current.tool_batches[0]
                entry = batch.entries[0].model_copy(
                    update={
                        "state": "terminal",
                        "buffered_terminal_result": winner,
                    }
                )
                self.runs[run.run_id] = current.model_copy(
                    update={
                        "tool_batches": [batch.model_copy(update={"entries": [entry]})],
                        "state_version": current.state_version + 1,
                    }
                )
                return InMemoryRunStoreResult("conflict", self.runs[run.run_id])
            return await super().cas_mutate(
                run,
                expected_state_version=expected_state_version,
                command_id=command_id,
            )

    run = make_run()
    assistant = AssistantMessage(
        message_id="assistant-1",
        content=[],
        tool_calls=[
            ToolCall(
                call_id="call-1",
                tool_name="fake_agent_echo",
                arguments={"value": "ok"},
            )
        ],
        finish_reason="tool_calls",
        usage=None,
        created_at=NOW,
    )
    batch = ToolCallBatch(
        assistant_message_id=assistant.message_id,
        entries=[
            ToolBatchEntry(
                call_id="call-1",
                assistant_message_id=assistant.message_id,
                source_index=0,
                tool_name="fake_agent_echo",
            )
        ],
    )
    run = run.model_copy(
        update={"transcript": [*run.transcript, assistant], "tool_batches": [batch]}
    )
    store = InterveningWinnerStore()
    kernel, store, _, _ = await make_kernel([], run=run, run_store=store)
    loser = ToolResult(
        call_id="call-1",
        tool_name="fake_agent_echo",
        status="rejected",
        content=[TextPart(text="loser")],
        artifact_refs=[],
        error_code="invalid_tool_call",
    )

    with pytest.raises(KernelConflict, match="entry changed"):
        await kernel._update_entry(
            run,
            0,
            0,
            state="terminal",
            result=loser,
            command="stale-writer",
        )

    stored = await store.load(run.run_id)
    assert stored.tool_batches[0].entries[0].buffered_terminal_result == winner


@pytest.mark.asyncio
async def test_acceptance_failure_skips_later_unstarted_siblings():
    tools = RecordingFakeToolRuntime(fail_accept_for={"fake_agent_fail"})
    kernel, store, _, _ = await make_kernel(
        [
            tool_events(
                ("accept-fail", "fake_agent_fail", "{}"),
                ("wait", "fake_agent_pause", '{"status":"input_required"}'),
            ),
            final_events(),
        ],
        tool_runtime=tools,
    )
    run_id = next(iter(store.runs))
    result = await kernel.run(run_id, signal=NeverCancelled())
    assert result.outcome == "failed"
    assert [entry.state for entry in result.run.tool_batches[0].entries] == [
        "terminal",
        "terminal",
    ]
    assert tools.execute_log == []
    messages = [
        item for item in result.run.transcript if isinstance(item, ToolResultMessage)
    ]
    assert [item.call_id for item in messages] == ["accept-fail", "wait"]
    assert [item.error_code for item in messages] == [
        "acceptance_failed",
        "skipped_due_to_prior_rejection",
    ]


@pytest.mark.asyncio
async def test_running_run_recovers_persisted_accepted_tool_entry():
    kernel, store, _, tools = await make_kernel([final_events()])
    run_id = next(iter(store.runs))
    run = store.runs[run_id]
    assistant = AssistantMessage(
        message_id="assistant-crash",
        content=[],
        tool_calls=[
            ToolCall(
                call_id="recover-call",
                tool_name="fake_agent_echo",
                arguments={"value": "recovered"},
            )
        ],
        finish_reason="tool_calls",
        usage=None,
        created_at=NOW,
    )
    resolved = kernel.tool_catalog.resolve(run, "fake_agent_echo")
    invocation = ToolInvocation(
        invocation_id="recover-call",
        run_id=run_id,
        expected_run_version=run.state_version,
        assistant_message_id=assistant.message_id,
        source_index=0,
        causation_id=assistant.message_id,
        idempotency_key="recovery-key",
        tool=resolved,
        arguments={"value": "recovered"},
        deadline_at=run.budget.deadline_at,
    )
    acceptance = await tools.accept(invocation)
    tools.accept_log.clear()
    batch = ToolCallBatch(
        assistant_message_id=assistant.message_id,
        entries=[
            ToolBatchEntry(
                call_id=invocation.invocation_id,
                assistant_message_id=assistant.message_id,
                source_index=0,
                tool_name="fake_agent_echo",
                state="accepted",
                invocation=invocation,
                acceptance=acceptance,
            )
        ],
    )
    store.runs[run_id] = run.model_copy(
        update={"transcript": [*run.transcript, assistant], "tool_batches": [batch]}
    )

    result = await kernel.run(run_id, signal=NeverCancelled())

    assert result.outcome == "final_answer"
    assert tools.accept_log == []
    assert tools.execute_log == ["recover-call"]
    assert result.run.tool_batches[0].results_flushed is True


@pytest.mark.asyncio
async def test_consecutive_tool_turns_have_distinct_turn_and_idempotency_keys():
    kernel, store, runtime, tools = await make_kernel(
        [
            tool_events(("call-1", "fake_agent_echo", '{"value":"one"}')),
            tool_events(("call-2", "fake_agent_echo", '{"value":"two"}')),
            final_events(),
        ]
    )

    result = await kernel.run(next(iter(store.runs)), signal=NeverCancelled())

    assert result.outcome == "final_answer"
    assert tools.execute_log == ["call-1", "call-2"]
    invocations = [batch.entries[0].invocation for batch in result.run.tool_batches]
    assert invocations[0].idempotency_key != invocations[1].idempotency_key
    assert len({request.turn_id for request in runtime.requests}) == 3


@pytest.mark.asyncio
async def test_tool_budget_is_reserved_before_acceptance():
    run = make_run(max_agent_calls=1)
    kernel, store, _, tools = await make_kernel(
        [
            tool_events(
                ("first", "fake_agent_echo", '{"value":"one"}'),
                ("excess", "fake_agent_echo", '{"value":"two"}'),
            ),
            final_events(),
        ],
        run=run,
    )

    result = await kernel.run(run.run_id, signal=NeverCancelled())
    results = [
        item for item in result.run.transcript if isinstance(item, ToolResultMessage)
    ]

    assert tools.accept_log == ["first"]
    assert tools.execute_log == ["first"]
    assert result.run.budget.agent_calls_used == 1
    assert next(item for item in results if item.call_id == "excess").is_error


@pytest.mark.asyncio
async def test_wrap_up_tool_calls_are_rejected_and_flushed_before_grace_final():
    run = make_run(max_model_turns=1, grace_model_turns=2)
    kernel, _, runtime, tools = await make_kernel(
        [
            tool_events(("normal", "fake_agent_echo", '{"value":"first"}')),
            tool_events(("grace-tool", "fake_agent_echo", '{"value":"blocked"}')),
            final_events("wrapped up"),
        ],
        run=run,
    )

    result = await kernel.run(run.run_id, signal=NeverCancelled())

    assert result.outcome == "final_answer"
    assert len(runtime.requests) == 3
    assert runtime.requests[1].tools == []
    assert tools.execute_log == ["normal"]
    grace_batch = result.run.tool_batches[1]
    assert grace_batch.results_flushed is True
    assert grace_batch.entries[0].state == "terminal"
    assert grace_batch.entries[0].buffered_terminal_result.error_code == (
        "grace_tools_disabled"
    )


@pytest.mark.asyncio
async def test_deadline_during_tool_reservation_aborts_batch_without_side_effects():
    class DeadlineAtToolPolicy(BudgetPolicy):
        def before_tool_call(self, budget, profile, *, now):
            del budget, profile, now
            raise BudgetExceeded("deadline")

    kernel, store, _, tools = await make_kernel(
        [
            tool_events(
                ("first", "fake_agent_echo", '{"value":"one"}'),
                ("second", "fake_agent_echo", '{"value":"two"}'),
            )
        ]
    )
    kernel.budget_policy = DeadlineAtToolPolicy()

    result = await kernel.run(next(iter(store.runs)), signal=NeverCancelled())

    assert result.outcome == "budget_exhausted"
    assert result.run.terminal_reason == "deadline"
    assert tools.accept_log == tools.execute_log == []
    assert all(
        entry.state == "terminal"
        and entry.buffered_terminal_result is not None
        and entry.buffered_terminal_result.error_code == "skipped_due_to_run_terminal"
        for entry in result.run.tool_batches[0].entries
    )
    assert result.run.tool_batches[0].results_flushed is True


@pytest.mark.asyncio
async def test_tool_budget_reservation_conflict_propagates_without_execution():
    class ReservationConflictStore(InMemoryOrchestratorRunStore):
        async def cas_mutate(self, run, *, expected_state_version, command_id):
            if command_id.startswith("tool-budget-reserve:"):
                current = self.runs[run.run_id]
                return InMemoryRunStoreResult("conflict", current)
            return await super().cas_mutate(
                run,
                expected_state_version=expected_state_version,
                command_id=command_id,
            )

    store = ReservationConflictStore()
    kernel, store, _, tools = await make_kernel(
        [tool_events(("call-1", "fake_agent_echo", '{"value":"ok"}'))],
        run_store=store,
    )

    with pytest.raises(KernelConflict, match="tool-budget-reserve"):
        await kernel.run(next(iter(store.runs)), signal=NeverCancelled())

    assert tools.accept_log == tools.execute_log == []
    stored = await store.load(next(iter(store.runs)))
    assert stored.tool_batches[0].entries[0].state == "pending"
    assert stored.tool_batches[0].entries[0].buffered_terminal_result is None


@pytest.mark.asyncio
async def test_usage_budget_exhaustion_prevents_tool_side_effects():
    run = make_run(max_output_tokens_total=1)
    events = [
        ModelStreamEvent(kind="attempt_started", attempt=1),
        ModelStreamEvent(
            kind="usage",
            attempt=1,
            usage=UsageRecord(input_tokens=1, output_tokens=2),
        ),
        *tool_events(("never", "fake_agent_echo", '{"value":"no"}'))[1:],
    ]
    kernel, store, _, tools = await make_kernel([events], run=run)

    result = await kernel.run(run.run_id, signal=NeverCancelled())

    assert result.outcome == "budget_exhausted"
    assert result.run.budget.output_tokens == 2
    assert tools.accept_log == tools.execute_log == []


@pytest.mark.asyncio
async def test_context_overflow_closes_canonical_turn_before_compaction_retry():
    class ShrinkingContextCompiler:
        def compile(self, _run, *, tools, summary=None):
            del tools
            message = ModelMessage(
                role="user", content=[ModelTextPart(text="compiled context")]
            )
            return CompiledContext(
                kind="ready",
                messages=[message],
                estimated_input_tokens=100 if summary is None else 50,
                reserved_output_tokens=10,
                retained_transcript_indexes=(0,),
                compacted=summary is not None,
            )

    overflow = [
        ModelStreamEvent(kind="attempt_started", attempt=1),
        ModelStreamEvent(
            kind="attempt_failed",
            attempt=1,
            error_class="context_overflow",
            retryable=False,
        ),
        ModelStreamEvent(
            kind="error",
            attempt=1,
            error_class="context_overflow",
            retryable=False,
        ),
    ]
    kernel, store, _, _ = await make_kernel([overflow, final_events("done")])
    kernel.context_compiler = ShrinkingContextCompiler()
    kernel.context_compactor = DeterministicFakeCompactor()
    lifecycle: list[tuple[str, dict[str, object]]] = []

    async def capture(event_type, _run, payload):
        lifecycle.append((event_type, payload))

    run_id = next(iter(store.runs))
    result = await kernel.run(
        run_id,
        signal=NeverCancelled(),
        lifecycle=capture,
    )

    boundaries = [
        (event_type, payload)
        for event_type, payload in lifecycle
        if event_type
        in {
            "turn_started",
            "message_started",
            "message_completed",
            "turn_completed",
            "model_retry_scheduled",
        }
    ]
    assert [event_type for event_type, _ in boundaries] == [
        "turn_started",
        "message_started",
        "message_completed",
        "turn_completed",
        "model_retry_scheduled",
        "turn_started",
        "message_started",
        "message_completed",
        "turn_completed",
    ]
    first_turn_id = str(boundaries[0][1]["internal_turn_id"])
    retry_payload = boundaries[4][1]
    second_turn_id = str(boundaries[5][1]["internal_turn_id"])
    assert boundaries[2][1]["disposition"] == "error"
    assert boundaries[3][1]["status"] == "error"
    assert retry_payload == {
        "internal_turn_id": first_turn_id,
        "attempt": 2,
        "error_class": "context_overflow",
        "retry_delay_ms": 0,
    }
    assert second_turn_id != first_turn_id
    assert result.outcome == "final_answer"
    assert result.run.active_internal_turn_id is None


@pytest.mark.asyncio
async def test_retry_without_usage_is_durably_counted_once():
    retry_then_final = [
        ModelStreamEvent(kind="attempt_started", attempt=1),
        ModelStreamEvent(
            kind="attempt_failed",
            attempt=1,
            error_class="timeout",
            retryable=True,
        ),
        ModelStreamEvent(
            kind="retry_scheduled",
            attempt=2,
            error_class="timeout",
            retryable=True,
        ),
        ModelStreamEvent(kind="attempt_started", attempt=2),
        ModelStreamEvent(kind="finish", attempt=2, finish_reason="stop"),
    ]
    kernel, store, _, _ = await make_kernel([retry_then_final])

    result = await kernel.run(next(iter(store.runs)), signal=NeverCancelled())

    assert result.run.budget.provider_retries_used == 1
    assert result.run.budget.model_turns_used == 1
    assert len(result.run.budget.provider_attempt_keys) == 2


@pytest.mark.asyncio
async def test_late_or_mismatched_observations_cannot_reopen_terminal_run():
    kernel, store, _, _ = await make_kernel(
        [
            tool_events(("wait", "fake_agent_pause", '{"status":"waiting_external"}')),
            final_events(),
        ]
    )
    run_id = next(iter(store.runs))
    await kernel.run(run_id, signal=NeverCancelled())
    with pytest.raises(ValueError, match="does not correlate"):
        await kernel.observe_tool(
            run_id,
            ToolObservation(
                observation_id="bad-suspension",
                invocation_id="wait",
                outcome=ToolSuspension(
                    invocation_id="other", status="waiting_external"
                ),
                observed_at=NOW,
            ),
            signal=NeverCancelled(),
        )
    completion = ToolObservation(
        observation_id="complete",
        invocation_id="wait",
        outcome=ToolResult(
            call_id="wait",
            tool_name="fake_agent_pause",
            status="completed",
            content=[TextPart(text="done")],
            artifact_refs=[],
        ),
        observed_at=NOW,
    )
    completed = await kernel.observe_tool(run_id, completion, signal=NeverCancelled())
    with pytest.raises(KernelConflict, match="terminal Run"):
        await kernel.observe_tool(
            run_id,
            ToolObservation(
                observation_id="late",
                invocation_id="wait",
                outcome=ToolSuspension(invocation_id="wait", status="input_required"),
                observed_at=NOW,
            ),
            signal=NeverCancelled(),
        )
    with pytest.raises(KeyError):
        await kernel.observe_tool(
            run_id,
            ToolObservation(
                observation_id="fabricated",
                invocation_id="other-run-call",
                outcome=ToolSuspension(
                    invocation_id="other-run-call", status="input_required"
                ),
                observed_at=NOW,
            ),
            signal=NeverCancelled(),
        )

    assert completed.outcome == "final_answer"
    assert completed.run.status == "completed"


@pytest.mark.asyncio
async def test_fake_tools_cover_failure_delays_and_cancellation():
    kernel, store, _, tools = await make_kernel(
        [
            tool_events(
                ("fail", "fake_agent_fail", '{"message":"expected"}'),
                ("parallel", "fake_agent_delay_parallel", '{"seconds":0}'),
                ("sequential", "fake_agent_delay_sequential", '{"seconds":0}'),
            ),
            final_events(),
        ]
    )
    result = await kernel.run(next(iter(store.runs)), signal=NeverCancelled())
    messages = [
        item for item in result.run.transcript if isinstance(item, ToolResultMessage)
    ]
    assert [item.status for item in messages] == ["failed", "completed", "completed"]
    assert tools.execute_log == ["fail", "parallel", "sequential"]

    invocation = result.run.tool_batches[0].entries[1].invocation
    acceptance = result.run.tool_batches[0].entries[1].acceptance
    with pytest.raises(ValueError, match="missing or mismatched"):
        await RecordingFakeToolRuntime().execute(
            invocation, acceptance, signal=NeverCancelled()
        )

    signal = EventCancellationSignal()
    # Runtime-level cancellation remains independent of cached replay outcomes.
    fresh_tools = RecordingFakeToolRuntime()
    cancel_invocation = invocation.model_copy(
        update={"invocation_id": "cancel-call", "idempotency_key": "cancel-key"}
    )
    cancel_acceptance = await fresh_tools.accept(cancel_invocation)
    signal.cancel()
    with pytest.raises(asyncio.CancelledError):
        await fresh_tools.execute(cancel_invocation, cancel_acceptance, signal=signal)


@pytest.mark.asyncio
async def test_terminal_tool_artifacts_merge_atomically_into_run_inventory():
    class ArtifactRuntime(RecordingFakeToolRuntime):
        async def execute(self, invocation, acceptance, *, signal):
            await super().execute(invocation, acceptance, signal=signal)
            return ToolResult(
                call_id=invocation.invocation_id,
                tool_name=invocation.tool.definition.name,
                status="completed",
                content=[],
                artifact_refs=["artifact-1"],
            )

    kernel, store, _, _ = await make_kernel(
        [
            tool_events(("artifact-call", "fake_agent_echo", '{"value":"ok"}')),
            final_events(),
        ],
        tool_runtime=ArtifactRuntime(),
    )
    result = await kernel.run(next(iter(store.runs)), signal=NeverCancelled())
    assert result.run.artifact_refs == ["artifact-1"]
    assert result.run.tool_batches[0].entries[
        0
    ].buffered_terminal_result.artifact_refs == ["artifact-1"]


@pytest.mark.asyncio
async def test_observed_terminal_tool_artifacts_merge_with_observation_checkpoint():
    kernel, store, _, _ = await make_kernel(
        [
            tool_events(
                (
                    "wait",
                    "fake_agent_pause",
                    '{"status":"waiting_external"}',
                )
            ),
            final_events(),
        ]
    )
    run_id = next(iter(store.runs))
    waiting = await kernel.run(run_id, signal=NeverCancelled())
    completed = await kernel.observe_tool(
        run_id,
        ToolObservation(
            observation_id="artifact-observation",
            invocation_id="wait",
            outcome=ToolResult(
                call_id="wait",
                tool_name="fake_agent_pause",
                status="completed",
                content=[],
                artifact_refs=["artifact-observed"],
            ),
            observed_at=NOW,
        ),
        signal=NeverCancelled(),
    )
    assert waiting.outcome == "waiting_external"
    assert completed.run.artifact_refs == ["artifact-observed"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "cause", ["user_requested", "room_closed", "shutdown", "policy"]
)
async def test_canceled_terminal_persists_typed_cancellation_cause(cause):
    kernel, store, _, _ = await make_kernel([])
    run_id = next(iter(store.runs))

    result = await kernel.terminalize(
        run_id,
        status="canceled",
        reason=f"canceled:{cause}",
        cancellation_cause=cause,
    )

    assert result.outcome == "aborted"
    saved = await store.load(run_id)
    assert saved is not None
    assert saved.status == "canceled"
    assert saved.cancellation_cause == cause
    assert canonical_settlement_payload(saved)["cancellation_code"] == cause


def test_request_user_input_schema_accepts_up_to_twelve_choices():
    from execution.orchestrator.kernel import REQUEST_USER_INPUT_TOOL_DEFINITION

    choices_schema = REQUEST_USER_INPUT_TOOL_DEFINITION.input_schema["properties"][
        "choices"
    ]
    assert choices_schema["maxItems"] == 12
    assert choices_schema["items"]["maxLength"] == 500


class RecordingSupervisorHITL:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def __call__(self, **kwargs) -> None:
        self.calls.append(kwargs)


@pytest.mark.asyncio
async def test_canonical_ask_user_suspends_and_resumes_with_durable_answer():
    from execution.orchestrator.kernel import (
        REQUEST_USER_INPUT_TOOL_NAME,
        supervisor_answer_observation,
    )

    hitl_port = RecordingSupervisorHITL()
    kernel, store, runtime, _ = await make_kernel(
        [
            tool_events(
                ("ask-1", REQUEST_USER_INPUT_TOOL_NAME, '{"question": "Which city?"}')
            ),
            final_events("You chose Shanghai."),
        ],
        supervisor_hitl=hitl_port,
    )
    run_id = next(iter(store.runs))

    lifecycle_events: list[tuple[str, dict[str, object]]] = []

    async def lifecycle(event_type, _run, payload):
        lifecycle_events.append((event_type, payload))

    result = await kernel.run(run_id, signal=NeverCancelled(), lifecycle=lifecycle)
    assert result.outcome == "awaiting_user"
    assert result.run.status == "awaiting_user"
    # Public protocol contract: the suspension update must extend a
    # tool_execution_started event for the same public call id.
    ask_events = [
        (event_type, payload)
        for event_type, payload in lifecycle_events
        if payload.get("call_id") == "ask-1"
    ]
    assert [event_type for event_type, _ in ask_events][:2] == [
        "tool_execution_started",
        "tool_execution_updated",
    ]
    start_payload = ask_events[0][1]
    assert start_payload["public_event_id"].endswith(":start")
    assert (
        start_payload["public_call_id"]
        == start_payload["public_event_id"].split(":start")[0].rsplit(":", 1)[1]
    )
    assert len(hitl_port.calls) == 1
    request = hitl_port.calls[0]
    assert request["run"].run_id == run_id
    assert request["question"] == "Which city?"
    from hashlib import sha256

    expected_interaction_id = sha256(f"{run_id}:ask:ask-1".encode()).hexdigest()
    assert request["interaction_id"] == expected_interaction_id
    entry = result.run.tool_batches[0].entries[0]
    assert entry.state == "input_required"
    assert entry.tool_name == REQUEST_USER_INPUT_TOOL_NAME
    assert entry.invocation is not None
    # The structured ask tool is only exposed on canonical Runs with the port.
    assert any(
        tool.name == REQUEST_USER_INPUT_TOOL_NAME for tool in runtime.requests[0].tools
    )

    observation = supervisor_answer_observation(run_id, "ask-1", "Shanghai", NOW)
    events_before_resume = len(lifecycle_events)
    resumed = await kernel.observe_tool(
        run_id, observation, signal=NeverCancelled(), lifecycle=lifecycle
    )
    assert resumed.outcome == "final_answer"
    assert resumed.run.status == "completed"
    # The resumed ask_user publishes running update + terminal end, and the
    # user answer never enters the public end event.
    resumed_events = [
        (event_type, payload)
        for event_type, payload in lifecycle_events[events_before_resume:]
        if payload.get("call_id") == "ask-1"
        and event_type in {"tool_execution_updated", "tool_execution_completed"}
    ]
    assert [event_type for event_type, _ in resumed_events] == [
        "tool_execution_updated",
        "tool_execution_completed",
    ]
    assert resumed_events[1][1]["result_text"] == ""
    assert "Shanghai" not in [str(payload) for _, payload in lifecycle_events]
    flushed = [
        item for item in resumed.run.transcript if isinstance(item, ToolResultMessage)
    ]
    assert len(flushed) == 1
    assert flushed[0].tool_name == REQUEST_USER_INPUT_TOOL_NAME
    assert flushed[0].content[0].text == "Shanghai"


def test_interaction_answers_are_recorded_once_per_round():
    from execution.orchestrator.kernel import _with_interaction_answers
    from execution.orchestrator.models import (
        ToolInteractionAnswer,
        ToolInteractionAnswers,
    )

    round_1 = ToolInteractionAnswers(
        interaction_id="interaction-1",
        answers=[ToolInteractionAnswer(question_id="q", answer="first")],
    )
    round_2 = ToolInteractionAnswers(interaction_id="interaction-2", answers=[])
    known = _with_interaction_answers([], round_1)
    # Every later observation of the same continuation replays round 1.
    assert _with_interaction_answers(known, round_1) == [round_1]
    assert _with_interaction_answers(known, None) == [round_1]
    assert _with_interaction_answers(known, round_2) == [round_1, round_2]


@pytest.mark.parametrize(
    "arguments",
    [
        '{"question": 42}',
        '{"question": "   "}',
        '{"question": "Which city?", "choices": ["Shanghai", "   "]}',
    ],
)
@pytest.mark.asyncio
async def test_ask_user_invalid_declaration_self_corrects_without_hitl(arguments):
    from execution.orchestrator.kernel import REQUEST_USER_INPUT_TOOL_NAME

    hitl_port = RecordingSupervisorHITL()
    kernel, store, _, _ = await make_kernel(
        [
            tool_events(("ask-1", REQUEST_USER_INPUT_TOOL_NAME, arguments)),
            final_events("fallback"),
        ],
        supervisor_hitl=hitl_port,
    )
    run_id = next(iter(store.runs))

    result = await kernel.run(run_id, signal=NeverCancelled())
    # Invalid ask_user declarations fail closed at the HITL boundary but remain
    # a model-correctable orchestration error.
    assert result.outcome == "final_answer"
    assert result.run.status == "completed"
    assert hitl_port.calls == []


@pytest.mark.asyncio
async def test_recovery_keeps_multi_batch_turn_open_when_entry_still_parked():
    """Restart replay must not emit a premature turn_end for a multi-batch turn.

    One batch is fully terminal (a prior join resolved it) while a second batch
    still carries a presented interaction awaiting a decision. Recovery must
    keep the internal turn active instead of closing it from the single
    completed batch.
    """
    run = make_run()
    assistant = AssistantMessage(
        message_id="assistant-1",
        content=[],
        tool_calls=[
            ToolCall(
                call_id="call-1",
                tool_name="fake_agent_echo",
                arguments={"value": "ok"},
            ),
        ],
        finish_reason="tool_calls",
        usage=None,
        created_at=NOW,
    )
    run = run.model_copy(
        update={
            "transcript": [*run.transcript, assistant],
            "active_internal_turn_id": "turn-open",
            "active_assistant_message_id": "assistant-1",
            "active_attempt": 1,
            "tool_batches": [
                ToolCallBatch(
                    assistant_message_id="assistant-1",
                    internal_turn_id="turn-open",
                    results_flushed=True,
                    entries=[
                        ToolBatchEntry(
                            call_id="call-1",
                            assistant_message_id="assistant-1",
                            source_index=0,
                            tool_name="fake_agent_echo",
                            state="terminal",
                            result_flushed=True,
                            opaque_public_call_id="inv_call-1",
                            buffered_terminal_result=ToolResult(
                                call_id="call-1",
                                tool_name="fake_agent_echo",
                                status="completed",
                                content=[],
                                artifact_refs=[],
                            ),
                        ),
                    ],
                ),
                ToolCallBatch(
                    assistant_message_id="assistant-1",
                    internal_turn_id="turn-open",
                    entries=[
                        ToolBatchEntry(
                            call_id="call-2",
                            assistant_message_id="assistant-1",
                            source_index=1,
                            tool_name="fake_agent_pause",
                            state="input_required",  # type: ignore[arg-type]
                            presented=True,
                            suspended_call_record_id="parent-2",
                            interaction_id="interaction-2",
                            interaction_fingerprint="fp-2",
                            opaque_public_call_id="inv_call-2",
                        ),
                    ],
                ),
            ],
        }
    )
    kernel, store, _, _ = await make_kernel([], run=run)
    stored = await store.load(next(iter(store.runs)))
    assert stored is not None

    async def read_events(_conversation_id, _run_id):
        return [
            {
                "room_seq": 1,
                "payload_public": {
                    "run_id": run.run_id,
                    "type": "turn_start",
                    "payload": {"internal_turn_id": "turn-open", "attempt": 1},
                },
            },
            {
                "room_seq": 2,
                "payload_public": {
                    "run_id": run.run_id,
                    "type": "message_start",
                    "payload": {
                        "internal_turn_id": "turn-open",
                        "message_id": "assistant-1",
                    },
                },
            },
            {
                "room_seq": 3,
                "payload_public": {
                    "run_id": run.run_id,
                    "type": "message_end",
                    "payload": {
                        "internal_turn_id": "turn-open",
                        "message_id": "assistant-1",
                        "disposition": "commentary",
                        "stop_reason": "tool_use",
                        "text": "",
                    },
                },
            },
        ]

    kernel.canonical_event_reader = read_events
    events: list[str] = []

    async def lifecycle(event_type, _run, _payload):
        events.append(event_type)

    recovered, closed_turn_id = await kernel._recover_active_canonical_attempt(
        stored, lifecycle
    )
    assert closed_turn_id is None
    assert "turn_completed" not in events
    assert recovered.active_internal_turn_id == "turn-open"


@pytest.mark.asyncio
async def test_failed_supervisor_ask_user_creation_is_logged(caplog):
    from execution.orchestrator.kernel import REQUEST_USER_INPUT_TOOL_NAME

    async def failing_hitl(**_kwargs) -> None:
        raise RuntimeError("ask store secret-detail")

    kernel, store, _runtime, _ = await make_kernel(
        [
            tool_events(
                ("ask-1", REQUEST_USER_INPUT_TOOL_NAME, '{"question": "Which city?"}')
            ),
        ],
        supervisor_hitl=failing_hitl,
    )
    run = next(iter(store.runs.values()))

    with caplog.at_level(logging.ERROR, logger="execution.orchestrator.kernel"):
        with pytest.raises(RuntimeError, match="secret-detail"):
            await kernel.run(run.run_id, signal=NeverCancelled())

    [record] = [
        r
        for r in caplog.records
        if r.getMessage() == "supervisor_ask_user_creation_failed"
    ]
    assert record.run_id == run.run_id
    assert record.conversation_id == run.conversation_id
    assert record.client_request_id == run.client_request_id
    assert record.client_request_id is not None
    assert record.call_id == "ask-1"
    assert record.error_type == "RuntimeError"
    assert "secret-detail" not in caplog.text

from __future__ import annotations

import pytest

from execution.model_loop.compaction import (
    DeterministicFakeCompactor,
    ModelBackedCompactor,
)
from execution.model_loop.context import ContextCompiler, UnresolvedToolBatchError
from execution.orchestrator.budget import BudgetPolicy
from execution.orchestrator.kernel import OrchestratorKernel
from execution.orchestrator.models import (
    AssistantMessage,
    ModelStreamEvent,
    TextPart,
    ToolCall,
    ToolInteractionMessage,
    ToolInteractionQuestion,
    ToolResultMessage,
    UsageRecord,
)
from tests._orchestrator_helpers import (
    NOW,
    FixedClock,
    FixedIDs,
    NeverCancelled,
    ScriptedModelRuntime,
    final_events,
    make_run,
    user_message,
)
from tests.orchestrator_harness.fake_tools import (
    RecordingFakeToolRuntime,
    StaticFakeToolCatalog,
)
from tests.orchestrator_harness.in_memory import (
    InMemoryOrchestratorRunStore,
    InMemoryProjectionDriver,
)


def pair(index):
    call_id = f"call-{index}"
    return [
        AssistantMessage(
            message_id=f"assistant-{index}",
            content=[],
            tool_calls=[ToolCall(call_id=call_id, tool_name="echo", arguments={})],
            finish_reason="tool_calls",
            usage=None,
            created_at=NOW,
        ),
        ToolResultMessage(
            message_id=f"result-{index}",
            call_id=call_id,
            tool_name="echo",
            status="completed",
            content=[TextPart(text="x" * 200)],
            artifact_refs=[],
            is_error=False,
            created_at=NOW,
        ),
    ]


def test_context_compaction_is_deterministic_non_destructive_and_pair_safe():
    run = make_run(context_window=500, max_output_tokens=100)
    transcript = [user_message("original"), *pair(1), *pair(2), *pair(3)]
    run = run.model_copy(update={"transcript": transcript})
    original = run.model_dump_json()
    compiler = ContextCompiler()

    first = compiler.compile(run, tools=[], summary="prior facts")
    second = compiler.compile(run, tools=[], summary="prior facts")

    assert first == second
    assert first.kind == "ready" and first.compacted is True
    assert (
        first.estimated_input_tokens + first.reserved_output_tokens
        <= run.profile.model.context_window
    )
    retained = set(first.retained_transcript_indexes)
    for index in (1, 3, 5):
        assert (index in retained) == (index + 1 in retained)
    assert run.model_dump_json() == original
    assert len(first.messages) < len(transcript) + 1


def test_oversized_transcript_requires_explicit_compaction_without_silent_tail():
    run = make_run(context_window=500, max_output_tokens=100)
    transcript = [user_message("original"), *pair(1), *pair(2), *pair(3)]
    run = run.model_copy(update={"transcript": transcript})

    compiled = ContextCompiler().compile(run, tools=[])

    assert compiled.kind == "needs_compaction"
    assert compiled.retained_transcript_indexes == tuple(range(len(transcript)))


def test_compacted_view_requires_first_user_request_to_fit():
    run = make_run(context_window=300, max_output_tokens=100)
    run = run.model_copy(update={"transcript": [user_message("x" * 1_000)]})

    compiled = ContextCompiler().compile(run, tools=[], summary="summary")

    assert compiled.kind == "context_unfit"
    assert compiled.retained_transcript_indexes == ()


def test_context_rejects_unresolved_tool_calls():
    run = make_run()
    run = run.model_copy(update={"transcript": [user_message(), pair(1)[0]]})
    with pytest.raises(UnresolvedToolBatchError):
        ContextCompiler().compile(run, tools=[])


@pytest.mark.asyncio
async def test_mandatory_context_unfit_never_calls_model_runtime():
    run = make_run(context_window=32, max_output_tokens=32)
    store = InMemoryOrchestratorRunStore()
    await store.create(run, command_id="create")
    runtime = ScriptedModelRuntime([final_events()])
    catalog = StaticFakeToolCatalog()
    kernel = OrchestratorKernel(
        run_store=store,
        model_runtime=runtime,
        tool_runtime=RecordingFakeToolRuntime(),
        tool_catalog=catalog,
        context_compiler=ContextCompiler(),
        budget_policy=BudgetPolicy(),
        projection_driver=InMemoryProjectionDriver(store),
        clock=FixedClock(),
        id_factory=FixedIDs(),
    )
    result = await kernel.run(run.run_id, signal=NeverCancelled())
    assert result.outcome == "failed"
    assert result.run.terminal_reason == "context_unfit"
    assert runtime.requests == []


@pytest.mark.asyncio
async def test_kernel_preflight_compaction_consumes_budget_before_model_turn():
    run = make_run(context_window=1_000, max_output_tokens=100)
    run = run.model_copy(
        update={"transcript": [user_message("original"), *pair(1), *pair(2), *pair(3)]}
    )
    store = InMemoryOrchestratorRunStore()
    await store.create(run, command_id="create")
    runtime = ScriptedModelRuntime([final_events()])
    kernel = OrchestratorKernel(
        run_store=store,
        model_runtime=runtime,
        tool_runtime=RecordingFakeToolRuntime(),
        tool_catalog=StaticFakeToolCatalog(),
        context_compiler=ContextCompiler(),
        context_compactor=DeterministicFakeCompactor(),
        budget_policy=BudgetPolicy(),
        projection_driver=InMemoryProjectionDriver(store),
        clock=FixedClock(),
        id_factory=FixedIDs(),
    )

    result = await kernel.run(run.run_id, signal=NeverCancelled())

    assert result.outcome == "final_answer"
    assert result.run.budget.compactions_used == 1
    assert result.run.budget.model_turns_used == 1
    assert result.run.compaction_summary == "Compacted 7 prior model messages."
    assert result.run.compaction_baseline_tokens is None


@pytest.mark.asyncio
async def test_restart_reuses_durable_compaction_without_second_compactor_call():
    run = make_run(context_window=1_000, max_output_tokens=100)
    run = run.model_copy(
        update={
            "transcript": [user_message("original"), *pair(1), *pair(2), *pair(3)],
            "budget": run.budget.model_copy(update={"compactions_used": 1}),
            "compaction_summary": "Durable prior-turn summary.",
            "compaction_baseline_tokens": 10_000,
        }
    )
    store = InMemoryOrchestratorRunStore()
    await store.create(run, command_id="create")
    runtime = ScriptedModelRuntime([final_events()])
    kernel = OrchestratorKernel(
        run_store=store,
        model_runtime=runtime,
        tool_runtime=RecordingFakeToolRuntime(),
        tool_catalog=StaticFakeToolCatalog(),
        context_compiler=ContextCompiler(),
        context_compactor=None,
        budget_policy=BudgetPolicy(),
        projection_driver=InMemoryProjectionDriver(store),
        clock=FixedClock(),
        id_factory=FixedIDs(),
    )

    result = await kernel.run(run.run_id, signal=NeverCancelled())

    assert result.outcome == "final_answer"
    assert result.run.budget.compactions_used == 1
    assert result.run.compaction_summary == "Durable prior-turn summary."
    assert result.run.compaction_baseline_tokens is None
    assert len(runtime.requests) == 1


@pytest.mark.asyncio
async def test_model_backed_compaction_checkpoints_attempts_and_usage():
    run = make_run(context_window=1_000, max_output_tokens=100)
    run = run.model_copy(
        update={"transcript": [user_message("original"), *pair(1), *pair(2), *pair(3)]}
    )
    store = InMemoryOrchestratorRunStore()
    await store.create(run, command_id="create")
    model_runtime = ScriptedModelRuntime([final_events()])
    compaction_runtime = ScriptedModelRuntime(
        [
            [
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
                ModelStreamEvent(
                    kind="usage",
                    attempt=2,
                    usage=UsageRecord(input_tokens=5, output_tokens=2),
                ),
                ModelStreamEvent(kind="text_delta", attempt=2, delta="summary"),
                ModelStreamEvent(kind="finish", attempt=2, finish_reason="stop"),
            ]
        ]
    )
    kernel = OrchestratorKernel(
        run_store=store,
        model_runtime=model_runtime,
        tool_runtime=RecordingFakeToolRuntime(),
        tool_catalog=StaticFakeToolCatalog(),
        context_compiler=ContextCompiler(),
        context_compactor=ModelBackedCompactor(
            compaction_runtime, model=run.profile.model
        ),
        budget_policy=BudgetPolicy(),
        projection_driver=InMemoryProjectionDriver(store),
        clock=FixedClock(),
        id_factory=FixedIDs(),
    )

    result = await kernel.run(run.run_id, signal=NeverCancelled())

    assert result.outcome == "final_answer"
    assert result.run.budget.compactions_used == 1
    assert result.run.budget.provider_retries_used == 1
    assert result.run.budget.input_tokens == 5
    assert len(result.run.budget.provider_attempt_keys) == 3


@pytest.mark.asyncio
async def test_deterministic_compactor_reduces_view_without_mutating_messages():
    messages = [object(), object(), object()]

    async def on_event(event):
        raise AssertionError(f"unexpected event {event}")

    result = await DeterministicFakeCompactor().compact(
        messages,
        turn_id="compaction-1",
        remaining_provider_retries=0,
        deadline_at=NOW,
        on_event=on_event,
        signal=NeverCancelled(),
    )
    assert result.summary == "Compacted 3 prior model messages."
    assert len(messages) == 3


def test_context_compaction_with_multi_round_interactions():
    run = make_run(context_window=4000, max_output_tokens=100)

    agent_call = AssistantMessage(
        message_id="a1",
        content=[],
        tool_calls=[
            ToolCall(
                call_id="call-1",
                tool_name="travel_planner",
                arguments={"task": "Plan trip"},
            )
        ],
        finish_reason="tool_calls",
        usage=None,
        created_at=NOW,
    )
    interaction1 = ToolInteractionMessage(
        message_id="interaction:call-1:fp1",
        call_id="call-1",
        tool_name="travel_planner",
        presentation_id="prs_1",
        interaction_id="interaction-1",
        interaction_fingerprint="fp1",
        questions=[
            ToolInteractionQuestion(
                question_id="q-1",
                interaction_kind="questionnaire",
                prompt="Destination?",
                answer_kind="text",
                required=True,
            )
        ],
        created_at=NOW,
    )
    surface_call = AssistantMessage(
        message_id="assistant-surface",
        content=[],
        tool_calls=[
            ToolCall(
                call_id="call-surface",
                tool_name="surface_agent_questions",
                arguments={},
            )
        ],
        finish_reason="tool_calls",
        usage=None,
        created_at=NOW,
    )
    surface_result = ToolResultMessage(
        message_id="result-surface",
        call_id="call-surface",
        tool_name="surface_agent_questions",
        status="completed",
        content=[TextPart(text="Answers applied")],
        artifact_refs=[],
        is_error=False,
        created_at=NOW,
    )
    interaction2 = ToolInteractionMessage(
        message_id="interaction:call-1:fp2",
        call_id="call-1",
        tool_name="travel_planner",
        presentation_id="prs_2",
        interaction_id="interaction-2",
        interaction_fingerprint="fp2",
        questions=[
            ToolInteractionQuestion(
                question_id="q-2",
                interaction_kind="questionnaire",
                prompt="Duration?",
                answer_kind="text",
                required=True,
            )
        ],
        created_at=NOW,
    )
    completed = ToolResultMessage(
        message_id="result-final",
        call_id="call-1",
        tool_name="travel_planner",
        status="completed",
        content=[TextPart(text="Here is your travel plan")],
        artifact_refs=[],
        is_error=False,
        created_at=NOW,
    )

    transcript = [
        user_message("Plan trip"),
        agent_call,
        interaction1,
        surface_call,
        surface_result,
        interaction2,
        completed,
    ]

    run = run.model_copy(update={"transcript": transcript})
    compiled = ContextCompiler().compile(run, tools=[], summary="Previous summary")
    assert compiled.kind == "ready"
    assert compiled.compacted is True
    # Verify summary message is first and all parts of the multi-round interaction are preserved
    assert len(compiled.messages) == 4
    assert compiled.messages[0].role == "user"
    assert "[summary] Previous summary" in compiled.messages[0].content[0].text
    assert compiled.messages[1].role == "user"
    assert "Plan trip" in compiled.messages[1].content[0].text
    assert compiled.messages[2].role == "assistant"
    assert compiled.messages[3].role == "tool"


def test_context_compaction_with_terminal_flush_order():
    run = make_run(context_window=4000, max_output_tokens=100)

    parent_assistant = AssistantMessage(
        message_id="a-parent",
        content=[],
        tool_calls=[
            ToolCall(
                call_id="call-parent",
                tool_name="travel_planner",
                arguments={"task": "Plan trip"},
            )
        ],
        finish_reason="tool_calls",
        usage=None,
        created_at=NOW,
    )
    interaction = ToolInteractionMessage(
        message_id="interaction:call-parent:fp1",
        call_id="call-parent",
        tool_name="travel_planner",
        presentation_id="prs_1",
        interaction_id="interaction-1",
        interaction_fingerprint="fp1",
        questions=[
            ToolInteractionQuestion(
                question_id="q-1",
                interaction_kind="questionnaire",
                prompt="Destination?",
                answer_kind="text",
                required=True,
            )
        ],
        created_at=NOW,
    )
    surface_assistant = AssistantMessage(
        message_id="a-surface",
        content=[],
        tool_calls=[
            ToolCall(
                call_id="call-surface",
                tool_name="surface_agent_questions",
                arguments={},
            )
        ],
        finish_reason="tool_calls",
        usage=None,
        created_at=NOW,
    )
    parent_result = ToolResultMessage(
        message_id="result-parent",
        call_id="call-parent",
        tool_name="travel_planner",
        status="completed",
        content=[TextPart(text="Travel plan generated")],
        artifact_refs=[],
        is_error=False,
        created_at=NOW,
    )
    surface_result = ToolResultMessage(
        message_id="result-surface",
        call_id="call-surface",
        tool_name="surface_agent_questions",
        status="completed",
        content=[TextPart(text="Answers applied")],
        artifact_refs=[],
        is_error=False,
        created_at=NOW,
    )

    # Actual terminal flush order: parent assistant -> interaction -> surface assistant -> parent result -> surface result
    transcript = [
        user_message("Plan trip"),
        parent_assistant,
        interaction,
        surface_assistant,
        parent_result,
        surface_result,
    ]

    run = run.model_copy(update={"transcript": transcript})
    compiled = ContextCompiler().compile(run, tools=[], summary="Previous summary")
    assert compiled.kind == "ready"
    assert compiled.compacted is True
    assert len(compiled.messages) == 4

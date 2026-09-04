"""Kernel integration tests for the model-first HITL loop (Phase 1 polish).

Covers join routing, per-fingerprint no-progress, F5 degrade, abandon closeout,
and per-decision-turn provider-error retry. These exercise the kernel through
``kernel.run`` / ``kernel.observe_tool`` with a deterministic fake runtime so the
assertions observe real public event emission and durable entry transitions.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock

import pytest

from execution.orchestrator.a2a_runtime.errors import RecoverableCheckpointError
from execution.orchestrator.fake_tools import RecordingFakeToolRuntime
from execution.orchestrator.kernel import (
    KernelConflict,
    _surface_agent_questions_tool_definition,
)
from execution.orchestrator.models import (
    TextPart,
    ToolBatchEntry,
    ToolCallBatch,
    ToolInteractionMessage,
    ToolInteractionQuestion,
    ToolResult,
    ToolSuspension,
)
from tests._orchestrator_a2a_helpers import invocation as _a2a_invocation
from tests._orchestrator_helpers import (
    NOW,
    NeverCancelled,
    final_events,
    make_kernel,
    make_run,
    tool_events,
)


def _interaction_suspension(
    call_id: str,
    *,
    call_record_id: str = "parent-1",
    interaction_id: str = "interaction-1",
    fingerprint: str = "fp-1",
    prompt: str = "Which cloud provider?",
) -> ToolSuspension:
    return ToolSuspension(
        invocation_id=call_id,
        status="input_required",
        call_record_id=call_record_id,
        interaction_id=interaction_id,
        interaction_fingerprint=fingerprint,
        questions=[
            ToolInteractionQuestion(question_id="q1", prompt=prompt, answer_kind="text")
        ],
    )


def test_surface_tool_schema_uses_singleton_and_multiple_private_presentations():
    singleton = make_run().model_copy(
        update={
            "tool_batches": [
                ToolCallBatch(
                    assistant_message_id="assistant-1",
                    internal_turn_id="turn-1",
                    entries=[
                        ToolBatchEntry(
                            call_id="call-1",
                            assistant_message_id="assistant-1",
                            source_index=0,
                            tool_name="agent-a",
                            state="input_required",
                            presented=True,
                            presentation_id="prs_one",
                            interaction_id="agent-alias-1",
                            interaction_fingerprint="fp-1",
                        )
                    ],
                )
            ]
        }
    )
    single_definition = _surface_agent_questions_tool_definition(singleton)
    assert single_definition.input_schema == {
        "type": "object",
        "additionalProperties": False,
        "properties": {},
    }

    second_batch = ToolCallBatch(
        assistant_message_id="assistant-2",
        internal_turn_id="turn-1",
        entries=[
            ToolBatchEntry(
                call_id="call-2",
                assistant_message_id="assistant-2",
                source_index=0,
                tool_name="agent-b",
                state="input_required",
                presented=True,
                presentation_id="prs_two",
                interaction_id="agent-alias-2",
                interaction_fingerprint="fp-2",
            )
        ],
    )
    multiple = singleton.model_copy(
        update={"tool_batches": [*singleton.tool_batches, second_batch]}
    )
    multiple_definition = _surface_agent_questions_tool_definition(multiple)
    assert multiple_definition.input_schema["required"] == ["presentation_id"]
    assert multiple_definition.input_schema["properties"]["presentation_id"][
        "enum"
    ] == ["prs_one", "prs_two"]
    serialized = str(multiple_definition.input_schema)
    assert "agent-alias" not in serialized
    assert "call-" not in serialized


class InteractionRuntime(RecordingFakeToolRuntime):
    """Fake runtime with configurable interaction suspensions and model replies."""

    def __init__(self) -> None:
        super().__init__()
        self.suspensions: dict[str, ToolSuspension] = {}
        self.model_replies: dict[str, ToolResult | ToolSuspension] = {}
        self.model_reply_calls: list[tuple[str, str, str | None]] = []
        self.published: list[tuple[str, str]] = []
        self.abandoned: list[tuple[str, str, str]] = []

    async def execute(self, invocation, acceptance, *, signal):
        suspension = self.suspensions.get(invocation.invocation_id)
        if suspension is not None:
            self.execute_log.append(invocation.invocation_id)
            self.acceptances[invocation.idempotency_key] = acceptance
            return suspension
        return await super().execute(invocation, acceptance, signal=signal)

    async def dispatch_model_reply(
        self,
        invocation,
        *,
        parent_call_record_id: str,
        interaction_fingerprint: str | None,
        signal,
    ):
        self.model_reply_calls.append(
            (invocation.invocation_id, parent_call_record_id, interaction_fingerprint)
        )
        outcome = self.model_replies.get(invocation.invocation_id)
        if outcome is not None:
            return outcome
        return await super().dispatch_model_reply(
            invocation,
            parent_call_record_id=parent_call_record_id,
            interaction_fingerprint=interaction_fingerprint,
            signal=signal,
        )

    async def publish_parked_interaction(
        self, *, call_record_id: str, interaction_id: str
    ) -> None:
        self.published.append((call_record_id, interaction_id))

    async def abandon_parked_interaction(
        self, *, call_record_id: str, interaction_id: str, terminal_state: str
    ) -> None:
        self.abandoned.append((call_record_id, interaction_id, terminal_state))


class FatalMixedRuntime(InteractionRuntime):
    async def accept(self, invocation):
        if invocation.invocation_id == "call-2":
            raise RuntimeError("acceptance unavailable")
        return await super().accept(invocation)


def _decision_payloads(events):
    return [payload for event_type, payload in events if event_type == "model_decision"]


@pytest.mark.asyncio
async def test_fatal_mixed_batch_terminalizes_suspended_sibling_and_folds():
    runtime = FatalMixedRuntime()
    runtime.suspensions["call-1"] = _interaction_suspension("call-1")
    kernel, store, _, _ = await make_kernel(
        [
            tool_events(
                ("call-1", "fake_agent_pause", '{"status":"input_required"}'),
                ("call-2", "fake_agent_pause", '{"status":"input_required"}'),
            ),
            final_events("unexpected retry"),
        ],
        tool_runtime=runtime,
    )
    events: list[tuple[str, dict[str, object]]] = []

    async def lifecycle(event_type, _run, payload):
        events.append((event_type, payload))

    result = await kernel.run(
        next(iter(store.runs)), signal=NeverCancelled(), lifecycle=lifecycle
    )

    assert result.outcome == "failed"
    by_call = {
        entry.call_id: entry
        for batch in result.run.tool_batches
        for entry in batch.entries
    }
    assert by_call["call-1"].state == "terminal"
    assert by_call["call-1"].buffered_terminal_result.error_code == (
        "interaction_abandoned"
    )
    assert runtime.abandoned == [("parent-1", "interaction-1", "failed")]
    assert by_call["call-2"].state == "terminal"
    assert by_call["call-2"].buffered_terminal_result.error_code == (
        "acceptance_failed"
    )
    turn_ends = [payload for kind, payload in events if kind == "turn_completed"]
    assert len(turn_ends) == 1
    assert turn_ends[0]["tool_call_ids"] == [by_call["call-1"].opaque_public_call_id]

    from delivery.snapshot import RoomEventFold
    from execution.orchestrator.lifecycle import SessionEvent
    from execution.orchestrator.public_projection import PublicProjectionTranslator

    translator = PublicProjectionTranslator(lifecycle_family="canonical")
    fold = RoomEventFold()
    all_events = [
        (
            "run_started",
            {"mode": "ultimate"},
        ),
        *events,
    ]
    room_seq = 0
    for sequence, (event_type, payload) in enumerate(all_events, start=1):
        projected = translator.translate(
            SessionEvent(
                event_type=event_type,
                session_id=result.run.session_id,
                run_id=result.run.run_id,
                causation_id=result.run.request.user_message_id,
                sequence=sequence,
                timestamp=NOW,
                payload=payload,
                room_id=result.run.room_id,
                user_message_id=result.run.request.user_message_id,
                client_request_id=result.run.client_request_id,
                lifecycle_family="canonical",
            ),
            catalog=result.run.tool_catalog,
        )
        if projected is None:
            continue
        room_seq += 1
        assert fold.apply(
            {
                "room_id": result.run.room_id,
                "room_seq": room_seq,
                "kind": "run_event",
                "ts": NOW.isoformat(),
                "payload_public": {
                    "event_id": projected.event_id,
                    "run_id": projected.run_id,
                    "seq": projected.seq,
                    "type": projected.kind,
                    "payload": projected.payload,
                    "correlation_id": projected.client_request_id,
                },
            }
        )


@pytest.mark.asyncio
async def test_model_first_join_routes_and_emits_answered_from_context():
    runtime = InteractionRuntime()
    runtime.suspensions["call-1"] = _interaction_suspension("call-1")
    runtime.model_replies["call-2"] = ToolResult(
        call_id="call-2",
        tool_name="fake_agent_pause",
        status="completed",
        content=[TextPart(text="done")],
        artifact_refs=[],
    )
    kernel, store, _, tools = await make_kernel(
        [
            tool_events(("call-1", "fake_agent_pause", '{"status":"input_required"}')),
            tool_events(("call-2", "fake_agent_pause", '{"status":"input_required"}')),
            final_events("done"),
        ],
        tool_runtime=runtime,
    )
    events: list[tuple[str, dict[str, object]]] = []

    async def lifecycle(event_type, _run, payload):
        events.append((event_type, payload))

    result = await kernel.run(
        next(iter(store.runs)), signal=NeverCancelled(), lifecycle=lifecycle
    )

    assert result.outcome == "final_answer"
    # call-1 executed normally; call-2 joined the parked interaction and did not
    # open a new task (no accept/execute for it).
    assert tools.execute_log == ["call-1"]
    assert "call-2" not in tools.accept_log
    assert runtime.model_reply_calls == [("call-2", "parent-1", None)]

    decisions = _decision_payloads(events)
    assert [d["decision"] for d in decisions] == [
        "interaction_received",
        "answered_from_context",
    ]
    answered = decisions[1]
    assert answered["agent_label"] == "fake_agent_pause"
    assert answered["question_summary"] == "Which cloud provider?"
    assert answered["source_summary"] == "from earlier messages and attachments"

    # Three-way consumption: the original parked entry is terminalized by the
    # join's terminal result. The model-first turn closes with both tool ids;
    # the subsequent final-answer turn closes separately with no tools.
    turn_end = [p for e, p in events if e == "turn_completed"]
    assert turn_end[0]["message_id"] == "assistant-2"
    assert len(turn_end[0]["tool_call_ids"]) == 2


@pytest.mark.asyncio
async def test_model_first_per_fingerprint_bound_emits_no_progress():
    runtime = InteractionRuntime()
    runtime.suspensions["call-1"] = _interaction_suspension("call-1")
    runtime.model_replies["call-2"] = ToolResult(
        call_id="call-2",
        tool_name="fake_agent_pause",
        status="failed",
        content=[],
        artifact_refs=[],
        error_code="auto_reply_limit_reached",
        error_message="The platform will not auto-reply to the same Agent question.",
    )
    kernel, store, _, _ = await make_kernel(
        [
            tool_events(("call-1", "fake_agent_pause", '{"status":"input_required"}')),
            tool_events(("call-2", "fake_agent_pause", '{"status":"input_required"}')),
            final_events("done"),
        ],
        tool_runtime=runtime,
    )
    events: list[tuple[str, dict[str, object]]] = []

    async def lifecycle(event_type, _run, payload):
        events.append((event_type, payload))

    result = await kernel.run(
        next(iter(store.runs)), signal=NeverCancelled(), lifecycle=lifecycle
    )

    decisions = _decision_payloads(events)
    no_progress = [d for d in decisions if d["decision"] == "no_progress"]
    assert len(no_progress) == 1
    assert no_progress[0]["reason"] == "auto_reply_limit_reached"

    # A failed join does NOT close the parked parent entry: the Agent's question
    # is still unanswered and must remain eligible for user input / abandon.
    parked = [
        entry
        for batch in result.run.tool_batches
        for entry in batch.entries
        if entry.call_id == "call-1"
    ][0]
    assert parked.state == "input_required"
    assert parked.presented is True
    assert parked.buffered_terminal_result is None


@pytest.mark.asyncio
async def test_model_first_join_accepted_reply_does_not_retry():
    """A bare accepted acknowledgement is not a retryable transport failure.

    The Agent acknowledged the reply and is still working; the response
    observation arrives later via the parent call's async path. The kernel must
    NOT re-send the delivered message and must leave the join in
    ``waiting_external`` while the parked parent stays parked.
    """
    runtime = InteractionRuntime()
    runtime.suspensions["call-1"] = _interaction_suspension("call-1")
    runtime.model_replies["call-2"] = ToolSuspension(
        invocation_id="call-2",
        status="waiting_external",
        delivery_state="accepted",
        call_record_id="parent-1",
        interaction_id="interaction-1",
        interaction_fingerprint="fp-1",
        questions=[
            ToolInteractionQuestion(
                question_id="q1", prompt="Which cloud provider?", answer_kind="text"
            )
        ],
    )
    kernel, store, _, _ = await make_kernel(
        [
            tool_events(("call-1", "fake_agent_pause", '{"status":"input_required"}')),
            tool_events(("call-2", "fake_agent_pause", '{"status":"input_required"}')),
        ],
        tool_runtime=runtime,
    )

    result = await kernel.run(
        next(iter(store.runs)), signal=NeverCancelled(), lifecycle=None
    )

    # Exactly one dispatch: the accepted reply was not re-sent.
    assert result.outcome == "waiting_external"
    assert runtime.model_reply_calls == [("call-2", "parent-1", None)]
    join = [
        entry
        for batch in result.run.tool_batches
        for entry in batch.entries
        if entry.call_id == "call-2"
    ][0]
    assert join.state == "waiting_external"
    assert join.suspended_call_record_id == "parent-1"
    parked = [
        entry
        for batch in result.run.tool_batches
        for entry in batch.entries
        if entry.call_id == "call-1"
    ][0]
    assert parked.state == "input_required"
    assert parked.presented is True


@pytest.mark.asyncio
async def test_model_first_join_transport_uncertain_retries_then_fails(
    monkeypatch,
):
    """A recoverable transport failure re-dispatches idempotently, bounded.

    After ``MAX_JOIN_DISPATCH_RETRIES`` attempts the join terminalizes with
    ``model_reply_dispatch_failed``; the parked parent entry stays parked (the
    Agent's question is still unanswered).
    """

    async def _no_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr("execution.orchestrator.kernel.asyncio.sleep", _no_sleep)
    runtime = InteractionRuntime()
    runtime.suspensions["call-1"] = _interaction_suspension("call-1")
    runtime.model_replies["call-2"] = ToolSuspension(
        invocation_id="call-2",
        status="waiting_external",
        delivery_state="transport_uncertain",
        call_record_id="parent-1",
        interaction_id="interaction-1",
        interaction_fingerprint="fp-1",
        questions=[
            ToolInteractionQuestion(
                question_id="q1", prompt="Which cloud provider?", answer_kind="text"
            )
        ],
    )
    kernel, store, _, _ = await make_kernel(
        [
            tool_events(("call-1", "fake_agent_pause", '{"status":"input_required"}')),
            tool_events(("call-2", "fake_agent_pause", '{"status":"input_required"}')),
            final_events("done"),
        ],
        tool_runtime=runtime,
    )

    result = await kernel.run(
        next(iter(store.runs)), signal=NeverCancelled(), lifecycle=None
    )

    # initial attempt + (MAX_JOIN_DISPATCH_RETRIES - 1) retries = 3 dispatches.
    assert len(runtime.model_reply_calls) == 3
    join = [
        entry
        for batch in result.run.tool_batches
        for entry in batch.entries
        if entry.call_id == "call-2"
    ][0]
    assert join.state == "terminal"
    assert join.buffered_terminal_result is not None
    assert join.buffered_terminal_result.error_code == "model_reply_dispatch_failed"
    # The parent parked entry is not terminalized by the failed join.
    parked = [
        entry
        for batch in result.run.tool_batches
        for entry in batch.entries
        if entry.call_id == "call-1"
    ][0]
    assert parked.state == "input_required"
    assert parked.presented is True


@pytest.mark.asyncio
async def test_failed_join_then_termination_abandons_parked_interaction():
    """A failed join leaves the parent parked; Run termination abandons it.

    ``model_reply_dispatch_failed`` must not terminalize the parked parent
    entries (no state divergence with the still-parked runtime call). A later
    Run termination must still abandon the parked interaction cleanly.
    """
    runtime = InteractionRuntime()
    runtime.suspensions["call-1"] = _interaction_suspension("call-1")
    runtime.model_replies["call-2"] = ToolResult(
        call_id="call-2",
        tool_name="fake_agent_pause",
        status="failed",
        content=[],
        artifact_refs=[],
        error_code="model_reply_dispatch_failed",
        error_message="The platform could not deliver the reply to the Agent.",
    )
    kernel, store, _, _ = await make_kernel(
        [
            tool_events(("call-1", "fake_agent_pause", '{"status":"input_required"}')),
            tool_events(("call-2", "fake_agent_pause", '{"status":"input_required"}')),
            final_events("done"),
        ],
        tool_runtime=runtime,
    )

    result = await kernel.run(
        next(iter(store.runs)), signal=NeverCancelled(), lifecycle=None
    )

    parked = [
        entry
        for batch in result.run.tool_batches
        for entry in batch.entries
        if entry.call_id == "call-1"
    ][0]
    assert parked.state == "input_required"
    assert parked.presented is True
    assert parked.buffered_terminal_result is None

    # Run termination still abandons the parked interaction (no divergence).
    terminated = await kernel.terminalize(
        next(iter(store.runs)), status="failed", reason="test termination"
    )
    assert runtime.abandoned == [("parent-1", "interaction-1", "failed")]
    assert terminated.outcome == "failed"

    runtime = InteractionRuntime()
    runtime.suspensions["call-1"] = _interaction_suspension("call-1")
    kernel, store, _, _ = await make_kernel(
        [
            tool_events(("call-1", "fake_agent_pause", '{"status":"input_required"}')),
            final_events("I need help from the user"),
        ],
        tool_runtime=runtime,
    )
    events: list[tuple[str, dict[str, object]]] = []

    async def lifecycle(event_type, _run, payload):
        events.append((event_type, payload))

    result = await kernel.run(
        next(iter(store.runs)), signal=NeverCancelled(), lifecycle=lifecycle
    )

    assert result.outcome == "awaiting_user"
    assert runtime.published == [("parent-1", "interaction-1")]
    decisions = _decision_payloads(events)
    degraded = [d for d in decisions if d["decision"] == "degraded_to_user"]
    assert len(degraded) == 1
    assert degraded[0]["reason"] == "decision_turn_inconclusive"


@pytest.mark.asyncio
async def test_provider_error_decision_retry_is_per_decision_turn():
    from execution.orchestrator.models import ModelStreamEvent

    def provider_error_events() -> list[ModelStreamEvent]:
        return [
            ModelStreamEvent(kind="attempt_started", attempt=1),
            ModelStreamEvent(
                kind="attempt_failed",
                attempt=1,
                error_class="timeout",
                retryable=True,
            ),
            ModelStreamEvent(
                kind="error", attempt=1, error_class="timeout", retryable=True
            ),
        ]

    runtime = InteractionRuntime()
    runtime.suspensions["call-1"] = _interaction_suspension("call-1")
    # Turn 1 is an unrelated provider error; it must not consume the decision
    # turn's single retry. Turn 1's retry parks a question; the decision turn
    # then gets exactly one retry before degrading.
    kernel, store, _, _ = await make_kernel(
        [
            provider_error_events(),
            tool_events(("call-1", "fake_agent_pause", '{"status":"input_required"}')),
            provider_error_events(),
            provider_error_events(),
        ],
        tool_runtime=runtime,
    )
    events: list[tuple[str, dict[str, object]]] = []

    async def lifecycle(event_type, _run, payload):
        events.append((event_type, payload))

    result = await kernel.run(
        next(iter(store.runs)), signal=NeverCancelled(), lifecycle=lifecycle
    )

    assert result.outcome == "awaiting_user"
    assert runtime.published == [("parent-1", "interaction-1")]
    degraded = [
        p
        for e, p in events
        if e == "model_decision" and p.get("decision") == "degraded_to_user"
    ]
    assert len(degraded) == 1
    assert degraded[0]["reason"] == "provider_error"


@pytest.mark.asyncio
async def test_termination_abandons_parked_interaction():
    from execution.orchestrator.models import ToolBatchEntry, ToolCallBatch

    run = make_run()
    run = run.model_copy(
        update={
            "active_internal_turn_id": "turn-1",
            "tool_batches": [
                ToolCallBatch(
                    assistant_message_id="assistant-1",
                    internal_turn_id="turn-1",
                    entries=[
                        ToolBatchEntry(
                            call_id="call-1",
                            assistant_message_id="assistant-1",
                            source_index=0,
                            tool_name="fake_agent_pause",
                            state="input_required",  # type: ignore[arg-type]
                            presented=True,
                            suspended_call_record_id="parent-1",
                            interaction_id="interaction-1",
                            interaction_fingerprint="fp-1",
                            opaque_public_call_id="inv_call-1",
                        ),
                    ],
                ),
            ],
        }
    )
    runtime = InteractionRuntime()
    kernel, store, _, _ = await make_kernel([], run=run, tool_runtime=runtime)

    result = await kernel.terminalize(
        next(iter(store.runs)),
        status="failed",
        reason="test termination",
    )

    assert result.outcome == "failed"
    assert runtime.abandoned == [("parent-1", "interaction-1", "failed")]
    parked = [
        entry
        for batch in result.run.tool_batches
        for entry in batch.entries
        if entry.call_id == "call-1"
    ][0]
    assert parked.state == "terminal"
    assert parked.buffered_terminal_result is not None
    assert parked.buffered_terminal_result.error_code == "interaction_abandoned"


@pytest.mark.asyncio
async def test_terminal_preflight_rejects_orphan_active_turn_without_effects():
    from execution.orchestrator.models import ToolBatchEntry, ToolCallBatch

    run = make_run().model_copy(
        update={
            "active_internal_turn_id": "turn-orphan",
            "active_assistant_message_id": None,
            "tool_batches": [
                ToolCallBatch(
                    assistant_message_id="assistant-historical",
                    internal_turn_id="turn-historical",
                    entries=[
                        ToolBatchEntry(
                            call_id="call-historical",
                            assistant_message_id="assistant-historical",
                            source_index=0,
                            tool_name="fake_agent_pause",
                            state="input_required",
                            presented=True,
                            suspended_call_record_id="parent-historical",
                            interaction_id="interaction-historical",
                            interaction_fingerprint="fp-historical",
                            opaque_public_call_id="inv-historical",
                        )
                    ],
                )
            ],
        }
    )
    runtime = InteractionRuntime()
    kernel, store, _, _ = await make_kernel([], run=run, tool_runtime=runtime)
    before = await store.load(run.run_id)
    events = []

    async def lifecycle(event_type, _run, payload):
        events.append((event_type, payload))

    with pytest.raises(
        KernelConflict, match="canonical active turn has no durable assistant message"
    ):
        await kernel.terminalize(
            run.run_id,
            status="failed",
            reason="legacy recovery",
            lifecycle=lifecycle,
        )

    assert await store.load(run.run_id) == before
    assert runtime.abandoned == []
    assert events == []


@pytest.mark.asyncio
async def test_terminal_abandonment_failure_is_mutation_free_and_retries_exactly():
    from execution.orchestrator.models import ToolBatchEntry, ToolCallBatch

    class FailsOnceRuntime(InteractionRuntime):
        def __init__(self):
            super().__init__()
            self.failures_remaining = 1

        async def abandon_parked_interaction(self, **kwargs):
            if self.failures_remaining:
                self.failures_remaining -= 1
                raise RecoverableCheckpointError("HITL store unavailable")
            await super().abandon_parked_interaction(**kwargs)

    run = make_run().model_copy(
        update={
            "active_internal_turn_id": "turn-1",
            "tool_batches": [
                ToolCallBatch(
                    assistant_message_id="assistant-1",
                    internal_turn_id="turn-1",
                    entries=[
                        ToolBatchEntry(
                            call_id="call-1",
                            assistant_message_id="assistant-1",
                            source_index=0,
                            tool_name="fake_agent_pause",
                            state="input_required",
                            presented=True,
                            suspended_call_record_id="parent-1",
                            interaction_id="interaction-1",
                            interaction_fingerprint="fp-1",
                            opaque_public_call_id="inv-call-1",
                        )
                    ],
                )
            ],
        }
    )
    runtime = FailsOnceRuntime()
    kernel, store, _, _ = await make_kernel([], run=run, tool_runtime=runtime)
    before = await store.load(run.run_id)
    events = []

    async def lifecycle(event_type, _run, payload):
        events.append((event_type, payload))

    with pytest.raises(RecoverableCheckpointError, match="HITL store unavailable"):
        await kernel.terminalize(
            run.run_id,
            status="failed",
            reason="legacy recovery",
            lifecycle=lifecycle,
        )

    assert await store.load(run.run_id) == before
    assert runtime.abandoned == []
    assert events == []

    result = await kernel.terminalize(
        run.run_id,
        status="failed",
        reason="legacy recovery",
        lifecycle=lifecycle,
    )
    assert result.outcome == "failed"
    assert runtime.abandoned == [("parent-1", "interaction-1", "failed")]


@pytest.mark.asyncio
@pytest.mark.parametrize("clear_active_turn", [False, True])
async def test_expired_suspended_run_closes_all_descendants_and_replays_noop(
    clear_active_turn,
):
    from delivery.snapshot import RoomEventFold
    from execution.orchestrator.lifecycle import SessionEvent
    from execution.orchestrator.public_projection import PublicProjectionTranslator

    runtime = InteractionRuntime()
    runtime.suspensions["call-1"] = _interaction_suspension("call-1")
    kernel, store, _, _ = await make_kernel(
        [
            tool_events(("call-1", "fake_agent_pause", '{"status":"input_required"}')),
            tool_events(("call-2", "surface_agent_questions", "{}")),
        ],
        tool_runtime=runtime,
        supervisor_hitl=AsyncMock(),
    )
    run_id = next(iter(store.runs))
    translator = PublicProjectionTranslator(lifecycle_family="canonical")
    records: dict[str, dict[str, object]] = {}
    private_sequence = 0
    room_sequence = 0

    async def lifecycle(event_type, run, payload):
        nonlocal private_sequence, room_sequence
        private_sequence += 1
        projected = translator.translate(
            SessionEvent(
                event_type=event_type,
                session_id=run.session_id,
                run_id=run.run_id,
                causation_id=run.request.user_message_id,
                sequence=private_sequence,
                timestamp=NOW,
                payload=payload,
                room_id=run.room_id,
                user_message_id=run.request.user_message_id,
                client_request_id=run.client_request_id,
                lifecycle_family="canonical",
            ),
            catalog=run.tool_catalog,
        )
        if projected is None or projected.event_id in records:
            return
        room_sequence += 1
        records[projected.event_id] = {
            "room_id": run.room_id,
            "room_seq": room_sequence,
            "kind": "run_event",
            "ts": NOW.isoformat(),
            "payload_public": {
                "event_id": projected.event_id,
                "run_id": projected.run_id,
                "seq": projected.seq,
                "type": projected.kind,
                "payload": projected.payload,
                "correlation_id": projected.client_request_id,
            },
        }

    await lifecycle("run_started", await store.load(run_id), {"mode": "ultimate"})
    waiting = await kernel.run(run_id, signal=NeverCancelled(), lifecycle=lifecycle)
    assert waiting.outcome == "awaiting_user"
    parent = waiting.run.tool_batches[0].entries[0]
    assert parent.acceptance is not None
    assert parent.presented is True
    assert parent.public_terminal_emitted is False
    active_turn_id = waiting.run.active_internal_turn_id
    assert active_turn_id is not None

    expired = waiting.run.model_copy(
        update={
            "budget": waiting.run.budget.model_copy(
                update={"deadline_at": NOW - timedelta(seconds=1)}
            ),
            "active_internal_turn_id": None if clear_active_turn else active_turn_id,
            "active_assistant_message_id": (
                None if clear_active_turn else waiting.run.active_assistant_message_id
            ),
            "active_attempt": None if clear_active_turn else waiting.run.active_attempt,
            "active_public_text": ""
            if clear_active_turn
            else waiting.run.active_public_text,
            "greatest_public_text_offset": (
                0 if clear_active_turn else waiting.run.greatest_public_text_offset
            ),
            "state_version": waiting.run.state_version + 1,
        }
    )
    saved = await store.cas_mutate(
        expired,
        expected_state_version=waiting.run.state_version,
        command_id=f"fixture-expire:{clear_active_turn}",
    )
    assert saved.outcome == "accepted"

    async def canonical_event_reader(_room_id, _run_id):
        return list(records.values())

    kernel.canonical_event_reader = canonical_event_reader
    terminal = await kernel.run(run_id, signal=NeverCancelled(), lifecycle=lifecycle)
    assert terminal.outcome == "budget_exhausted"
    assert terminal.run.status == "budget_exhausted"
    assert terminal.run.projection_state == "settled"
    assert all(item.status == "completed" for item in terminal.run.projection_outbox)
    assert runtime.abandoned == [("parent-1", "interaction-1", "failed")]
    assert all(
        batch.results_flushed
        and all(
            entry.state == "terminal"
            and (entry.acceptance is None or entry.public_terminal_emitted)
            for entry in batch.entries
        )
        for batch in terminal.run.tool_batches
    )
    tool_end = next(
        payload
        for event_type, payload in [
            (
                record["payload_public"]["type"],
                record["payload_public"]["payload"],
            )
            for record in records.values()
        ]
        if event_type == "tool_execution_end"
    )
    assert tool_end["internal_turn_id"] == active_turn_id
    turn_end = next(
        record["payload_public"]["payload"]
        for record in records.values()
        if record["payload_public"]["type"] == "turn_end"
    )
    assert turn_end["internal_turn_id"] == active_turn_id
    assert turn_end["message_id"] == waiting.run.tool_batches[-1].assistant_message_id
    assert turn_end["tool_call_ids"] == [
        entry.opaque_public_call_id
        for batch in waiting.run.tool_batches
        for entry in batch.entries
        if entry.opaque_public_call_id is not None
    ]

    emitted_before_replay = len(records)
    replay = await kernel.run(run_id, signal=NeverCancelled(), lifecycle=lifecycle)
    assert replay.run.state_version == terminal.run.state_version
    assert len(records) == emitted_before_replay
    assert runtime.abandoned == [("parent-1", "interaction-1", "failed")]

    fold = RoomEventFold()
    for record in records.values():
        assert fold.apply(record), record
    room_sequence += 1
    settled_record = {
        "room_id": terminal.run.room_id,
        "room_seq": room_sequence,
        "kind": "run_event",
        "ts": NOW.isoformat(),
        "payload_public": {
            "event_id": f"run-settled:{run_id}",
            "run_id": run_id,
            "seq": private_sequence + 1,
            "type": "run_settled",
            "payload": {
                "status": "failed",
                "started_at": terminal.run.created_at.isoformat(),
                "settled_at": NOW.isoformat(),
                "duration_ms": 1,
                "failure_code": "budget_exhausted",
                "error_summary": "The request could not be completed.",
            },
            "correlation_id": terminal.run.client_request_id,
        },
    }
    assert fold.apply(settled_record)
    folded = fold.state(room_seq=room_sequence)["turns"][0]
    assert folded["state"] == "failed"
    assert folded["internal_turns"][0]["status"] == "error"


async def _terminal_public_tool_replay_fixture(status):
    from execution.orchestrator.lifecycle import SessionEvent
    from execution.orchestrator.models import ToolAcceptance
    from execution.orchestrator.public_projection import PublicProjectionTranslator

    base = make_run()
    invocation = _a2a_invocation(run_id=base.run_id, call_id="call-private")
    acceptance = ToolAcceptance(
        acceptance_id="accepted-private",
        invocation_id=invocation.invocation_id,
        idempotency_key=invocation.idempotency_key,
        accepted_at=NOW,
    )
    result = ToolResult(
        call_id="call-private",
        tool_name="fake_agent_pause",
        status=status,
        content=[],
        artifact_refs=[],
        error_code=None if status == "completed" else "terminal",
        error_message=None,
    )
    entry = ToolBatchEntry(
        call_id="call-private",
        assistant_message_id="assistant-public-owner",
        source_index=0,
        tool_name="fake_agent_pause",
        state="terminal",
        invocation=invocation,
        acceptance=acceptance,
        opaque_public_call_id="inv-public",
        buffered_terminal_result=result,
    )
    run = base.model_copy(
        update={
            "status": "running",
            "active_internal_turn_id": None,
            "active_assistant_message_id": None,
            "tool_batches": [
                ToolCallBatch(
                    assistant_message_id="assistant-public-owner",
                    internal_turn_id="turn-public-owner",
                    entries=[entry],
                )
            ],
        }
    )
    runtime = InteractionRuntime()
    kernel, store, _, _ = await make_kernel([], run=run, tool_runtime=runtime)
    projected = PublicProjectionTranslator(lifecycle_family="canonical").translate(
        SessionEvent(
            event_type="tool_execution_completed",
            session_id=run.session_id,
            run_id=run.run_id,
            causation_id=run.request.user_message_id,
            sequence=1,
            timestamp=NOW,
            payload={
                "public_event_id": "public-existing-tool-end",
                "call_id": entry.call_id,
                "public_call_id": entry.opaque_public_call_id,
                "internal_turn_id": "turn-public-owner",
                "status": status,
                "result_status": status,
                "tool_name": entry.tool_name,
                "agent_label": kernel._tool_label(run, entry.tool_name),
                "duration_ms": 0,
                "result_text": "",
            },
            room_id=run.room_id,
            user_message_id=run.request.user_message_id,
            client_request_id=run.client_request_id,
            lifecycle_family="canonical",
        ),
        catalog=run.tool_catalog,
    )
    assert projected is not None
    record = {
        "payload_public": {
            "run_id": run.run_id,
            "type": "tool_execution_end",
            "payload": dict(projected.payload),
        }
    }
    return kernel, store, runtime, run, record


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("completed", ("completed", False, None)),
        ("canceled", ("canceled", False, None)),
        ("failed", ("failed", True, "execution")),
        ("rejected", ("failed", True, "rejected")),
        ("expired", ("failed", True, "expired")),
    ],
)
async def test_terminal_preflight_reconciles_exact_public_outcome_for_all_statuses(
    status, expected
):
    kernel, store, runtime, run, record = await _terminal_public_tool_replay_fixture(
        status
    )
    payload = record["payload_public"]["payload"]
    expected_outcome, expected_is_error, expected_failure = expected
    assert payload["outcome"] == expected_outcome
    assert payload["is_error"] is expected_is_error
    if expected_failure is None:
        assert "failure_reason" not in payload
        # Existing room-event translators may serialize an absent optional
        # failure as JSON null. It is semantically equivalent to omission.
        payload["failure_reason"] = None
    else:
        assert payload["failure_reason"] == expected_failure

    async def canonical_event_reader(_room_id, _run_id):
        return [record]

    emissions = []

    async def lifecycle(event_type, _run, event_payload):
        emissions.append((event_type, event_payload))

    kernel.canonical_event_reader = canonical_event_reader
    terminal = await kernel.terminalize(
        run.run_id,
        status="failed",
        reason="legacy recovery",
        lifecycle=lifecycle,
    )

    assert terminal.outcome == "failed"
    assert terminal.run.tool_batches[0].entries[0].public_terminal_emitted is True
    assert runtime.abandoned == []
    assert not any(
        event_type == "tool_execution_completed"
        and event_payload.get("public_call_id") == "inv-public"
        for event_type, event_payload in emissions
    )
    assert await store.load(run.run_id) == terminal.run


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("private_status", "mutation"),
    [
        ("failed", {"outcome": "completed"}),
        ("failed", {"outcome": 1}),
        ("failed", {"__delete__": "outcome"}),
        ("failed", {"is_error": False}),
        ("failed", {"is_error": 1}),
        ("failed", {"__delete__": "is_error"}),
        ("failed", {"failure_reason": "expired"}),
        ("failed", {"failure_reason": None}),
        ("failed", {"__delete__": "failure_reason"}),
        ("completed", {"failure_reason": "execution"}),
        ("failed", {"internal_turn_id": None}),
        ("failed", {"internal_turn_id": 1}),
        ("failed", {"internal_turn_id": "turn-unknown"}),
        ("failed", {"tool_call_id": None}),
        ("failed", {"tool_call_id": 1}),
        ("failed", {"__malformed_payload__": True}),
        ("failed", {"tool_name": "Wrong public label"}),
        ("failed", {"tool_name": 1}),
    ],
)
async def test_terminal_preflight_rejects_malformed_public_tool_end_before_effects(
    private_status, mutation
):
    kernel, store, runtime, run, record = await _terminal_public_tool_replay_fixture(
        private_status
    )
    delete_key = mutation.get("__delete__")
    if mutation.get("__malformed_payload__"):
        record["payload_public"]["payload"] = None
    elif delete_key is None:
        record["payload_public"]["payload"].update(mutation)
    else:
        record["payload_public"]["payload"].pop(delete_key)

    async def canonical_event_reader(_room_id, _run_id):
        return [record]

    kernel.canonical_event_reader = canonical_event_reader
    before = await store.load(run.run_id)
    emissions = []

    async def lifecycle(event_type, _run, payload):
        emissions.append((event_type, payload))

    with pytest.raises(KernelConflict):
        await kernel.terminalize(
            run.run_id,
            status="failed",
            reason="legacy recovery",
            lifecycle=lifecycle,
        )

    assert await store.load(run.run_id) == before
    assert runtime.abandoned == []
    assert emissions == []


@pytest.mark.asyncio
async def test_expiry_reconciles_durable_public_tool_end_without_reemitting():
    """Crash after public append but before the aggregate emitted checkpoint."""
    from delivery.snapshot import RoomEventFold
    from execution.orchestrator.lifecycle import SessionEvent
    from execution.orchestrator.public_projection import PublicProjectionTranslator

    runtime = InteractionRuntime()
    runtime.suspensions["call-1"] = _interaction_suspension("call-1")
    kernel, store, _, _ = await make_kernel(
        [
            tool_events(("call-1", "fake_agent_pause", '{"status":"input_required"}')),
            tool_events(("call-2", "surface_agent_questions", "{}")),
        ],
        tool_runtime=runtime,
        supervisor_hitl=AsyncMock(),
    )
    run_id = next(iter(store.runs))
    translator = PublicProjectionTranslator(lifecycle_family="canonical")
    records: dict[str, dict[str, object]] = {}
    emissions: list[tuple[str, dict[str, object]]] = []
    private_sequence = 0
    room_sequence = 0

    async def lifecycle(event_type, run, payload):
        nonlocal private_sequence, room_sequence
        emissions.append((event_type, payload))
        private_sequence += 1
        projected = translator.translate(
            SessionEvent(
                event_type=event_type,
                session_id=run.session_id,
                run_id=run.run_id,
                causation_id=run.request.user_message_id,
                sequence=private_sequence,
                timestamp=NOW,
                payload=payload,
                room_id=run.room_id,
                user_message_id=run.request.user_message_id,
                client_request_id=run.client_request_id,
                lifecycle_family="canonical",
            ),
            catalog=run.tool_catalog,
        )
        if projected is None or projected.event_id in records:
            return
        room_sequence += 1
        records[projected.event_id] = {
            "room_id": run.room_id,
            "room_seq": room_sequence,
            "kind": "run_event",
            "ts": NOW.isoformat(),
            "payload_public": {
                "event_id": projected.event_id,
                "run_id": projected.run_id,
                "seq": projected.seq,
                "type": projected.kind,
                "payload": projected.payload,
                "correlation_id": projected.client_request_id,
            },
        }

    await lifecycle("run_started", await store.load(run_id), {"mode": "ultimate"})
    waiting = await kernel.run(run_id, signal=NeverCancelled(), lifecycle=lifecycle)
    assert waiting.outcome == "awaiting_user"
    parent = waiting.run.tool_batches[0].entries[0]
    assert parent.opaque_public_call_id is not None
    assert parent.public_terminal_emitted is False
    assert waiting.run.active_internal_turn_id is not None

    # The public append wins, then the process crashes before checkpointing
    # public_terminal_emitted=True on the already-terminal private child.
    result = ToolResult(
        call_id=parent.call_id,
        tool_name=parent.tool_name,
        status="failed",
        content=[],
        artifact_refs=[],
        error_code="run_failed",
        error_message=None,
    )
    batches = list(waiting.run.tool_batches)
    entries = list(batches[0].entries)
    entries[0] = parent.model_copy(
        update={"state": "terminal", "buffered_terminal_result": result}
    )
    batches[0] = batches[0].model_copy(update={"entries": entries})
    await lifecycle(
        "tool_execution_completed",
        waiting.run,
        {
            "public_event_id": (f"public:{run_id}:{parent.opaque_public_call_id}:end"),
            "call_id": parent.call_id,
            "public_call_id": parent.opaque_public_call_id,
            "internal_turn_id": waiting.run.active_internal_turn_id,
            "status": "failed",
            "result_status": "failed",
            "tool_name": parent.tool_name,
            "agent_label": kernel._tool_label(waiting.run, parent.tool_name),
            "duration_ms": 0,
            "result_text": "",
        },
    )
    durable_tool_end = next(
        record["payload_public"]
        for record in records.values()
        if record["payload_public"]["type"] == "tool_execution_end"
        and record["payload_public"]["payload"]["tool_call_id"]
        == parent.opaque_public_call_id
    )
    assert "call_id" not in durable_tool_end["payload"]

    expired = waiting.run.model_copy(
        update={
            "budget": waiting.run.budget.model_copy(
                update={"deadline_at": NOW - timedelta(seconds=1)}
            ),
            "tool_batches": batches,
            "state_version": waiting.run.state_version + 1,
        }
    )
    saved = await store.cas_mutate(
        expired,
        expected_state_version=waiting.run.state_version,
        command_id="fixture-public-tool-end-won",
    )
    assert saved.outcome == "accepted"

    async def canonical_event_reader(_room_id, _run_id):
        return list(records.values())

    kernel.canonical_event_reader = canonical_event_reader
    emissions.clear()
    terminal = await kernel.run(run_id, signal=NeverCancelled(), lifecycle=lifecycle)

    assert terminal.outcome == "budget_exhausted"
    recovered_parent = terminal.run.tool_batches[0].entries[0]
    assert recovered_parent.state == "terminal"
    assert recovered_parent.public_terminal_emitted is True
    assert not any(
        event_type == "tool_execution_completed"
        and payload.get("public_call_id") == parent.opaque_public_call_id
        for event_type, payload in emissions
    )
    assert (
        sum(
            record["payload_public"]["type"] == "tool_execution_end"
            and record["payload_public"]["payload"]["tool_call_id"]
            == parent.opaque_public_call_id
            for record in records.values()
        )
        == 1
    )

    fold = RoomEventFold()
    for record in records.values():
        assert fold.apply(record), record
    room_sequence += 1
    assert fold.apply(
        {
            "room_id": terminal.run.room_id,
            "room_seq": room_sequence,
            "kind": "run_event",
            "ts": NOW.isoformat(),
            "payload_public": {
                "event_id": f"run-settled:{run_id}",
                "run_id": run_id,
                "seq": private_sequence + 1,
                "type": "run_settled",
                "payload": {
                    "status": "failed",
                    "started_at": terminal.run.created_at.isoformat(),
                    "settled_at": NOW.isoformat(),
                    "duration_ms": 1,
                    "failure_code": "budget_exhausted",
                    "error_summary": "The request could not be completed.",
                },
                "correlation_id": terminal.run.client_request_id,
            },
        }
    )
    folded = fold.state(room_seq=room_sequence)["turns"][0]
    assert folded["state"] == "failed"
    assert folded["internal_turns"][0]["status"] == "error"


@pytest.mark.asyncio
@pytest.mark.parametrize("conflict", ["unknown", "misowned", "duplicate"])
async def test_terminal_preflight_rejects_contradictory_public_tool_end_before_effects(
    conflict,
):
    from execution.orchestrator.lifecycle import SessionEvent
    from execution.orchestrator.models import ToolAcceptance
    from execution.orchestrator.public_projection import PublicProjectionTranslator

    base = make_run()
    invocation = _a2a_invocation(run_id=base.run_id, call_id="call-private")
    acceptance = ToolAcceptance(
        acceptance_id="accepted-private",
        invocation_id=invocation.invocation_id,
        idempotency_key=invocation.idempotency_key,
        accepted_at=NOW,
    )
    result = ToolResult(
        call_id="call-private",
        tool_name="fake_agent_pause",
        status="failed",
        content=[],
        artifact_refs=[],
        error_code="run_failed",
        error_message=None,
    )
    entry = ToolBatchEntry(
        call_id="call-private",
        assistant_message_id="assistant-public-owner",
        source_index=0,
        tool_name="fake_agent_pause",
        state="terminal" if conflict == "duplicate" else "input_required",
        invocation=invocation,
        acceptance=acceptance,
        presented=True,
        suspended_call_record_id="parent-private",
        interaction_id="interaction-private",
        interaction_fingerprint="fp-private",
        opaque_public_call_id="inv-public",
        buffered_terminal_result=result if conflict == "duplicate" else None,
    )
    run = base.model_copy(
        update={
            "status": "running",
            "active_internal_turn_id": None,
            "active_assistant_message_id": None,
            "tool_batches": [
                ToolCallBatch(
                    assistant_message_id="assistant-public-owner",
                    internal_turn_id="turn-public-owner",
                    entries=[entry],
                )
            ],
        }
    )
    runtime = InteractionRuntime()
    kernel, store, _, _ = await make_kernel([], run=run, tool_runtime=runtime)
    translator = PublicProjectionTranslator(lifecycle_family="canonical")
    projected = translator.translate(
        SessionEvent(
            event_type="tool_execution_completed",
            session_id=run.session_id,
            run_id=run.run_id,
            causation_id=run.request.user_message_id,
            sequence=1,
            timestamp=NOW,
            payload={
                "public_event_id": "public-existing-tool-end",
                "call_id": "call-private",
                "public_call_id": "inv-public",
                "internal_turn_id": "turn-public-owner",
                "status": "failed",
                "result_status": "failed",
                "tool_name": "fake_agent_pause",
                "agent_label": kernel._tool_label(run, "fake_agent_pause"),
                "duration_ms": 0,
                "result_text": "",
            },
            room_id=run.room_id,
            user_message_id=run.request.user_message_id,
            client_request_id=run.client_request_id,
            lifecycle_family="canonical",
        ),
        catalog=run.tool_catalog,
    )
    assert projected is not None
    public_payload = dict(projected.payload)
    assert "call_id" not in public_payload
    if conflict == "unknown":
        public_payload["tool_call_id"] = "inv-unknown"
    elif conflict == "misowned":
        public_payload["internal_turn_id"] = "turn-wrong-owner"
    record = {
        "payload_public": {
            "run_id": run.run_id,
            "type": "tool_execution_end",
            "payload": public_payload,
        }
    }
    canonical_records = [record, record] if conflict == "duplicate" else [record]

    async def canonical_event_reader(_room_id, _run_id):
        return canonical_records

    kernel.canonical_event_reader = canonical_event_reader
    before = await store.load(run.run_id)
    emissions = []

    async def lifecycle(event_type, _run, payload):
        emissions.append((event_type, payload))

    with pytest.raises(KernelConflict):
        await kernel.terminalize(
            run.run_id,
            status="failed",
            reason="legacy recovery",
            lifecycle=lifecycle,
        )

    assert await store.load(run.run_id) == before
    assert runtime.abandoned == []
    assert emissions == []


@pytest.mark.asyncio
async def test_termination_closes_each_historical_turn_under_its_own_owner():
    from execution.orchestrator.models import ToolAcceptance

    base = make_run()
    batches = []
    for index in (1, 2):
        invocation = _a2a_invocation(
            run_id=base.run_id, call_id=f"call-{index}"
        ).model_copy(
            update={
                "assistant_message_id": f"assistant-{index}",
                "causation_id": f"assistant-{index}",
            }
        )
        acceptance = ToolAcceptance(
            acceptance_id=f"accepted-{index}",
            invocation_id=invocation.invocation_id,
            idempotency_key=invocation.idempotency_key,
            accepted_at=NOW,
        )
        batches.append(
            ToolCallBatch(
                assistant_message_id=f"assistant-{index}",
                internal_turn_id=f"turn-{index}",
                entries=[
                    ToolBatchEntry(
                        call_id=f"call-{index}",
                        assistant_message_id=f"assistant-{index}",
                        source_index=0,
                        tool_name="fake_agent_pause",
                        state="input_required",
                        invocation=invocation,
                        acceptance=acceptance,
                        presented=True,
                        suspended_call_record_id=f"parent-{index}",
                        interaction_id=f"interaction-{index}",
                        interaction_fingerprint=f"fp-{index}",
                        opaque_public_call_id=f"inv-{index}",
                    )
                ],
            )
        )
    run = base.model_copy(
        update={
            "status": "running",
            "active_internal_turn_id": None,
            "active_assistant_message_id": None,
            "tool_batches": batches,
        }
    )
    runtime = InteractionRuntime()
    kernel, store, _, _ = await make_kernel([], run=run, tool_runtime=runtime)
    events = []

    async def lifecycle(event_type, _run, payload):
        events.append((event_type, payload))

    terminal = await kernel.terminalize(
        run.run_id,
        status="failed",
        reason="legacy recovery",
        lifecycle=lifecycle,
    )

    assert terminal.outcome == "failed"
    assert runtime.abandoned == [
        ("parent-1", "interaction-1", "failed"),
        ("parent-2", "interaction-2", "failed"),
    ]
    ends = [payload for kind, payload in events if kind == "tool_execution_completed"]
    assert [(item["call_id"], item["internal_turn_id"]) for item in ends] == [
        ("call-1", "turn-1"),
        ("call-2", "turn-2"),
    ]
    turn_ends = [payload for kind, payload in events if kind == "turn_completed"]
    assert [item["internal_turn_id"] for item in turn_ends] == ["turn-1", "turn-2"]
    assert [item["message_id"] for item in turn_ends] == [
        "assistant-1",
        "assistant-2",
    ]
    assert [item["tool_call_ids"] for item in turn_ends] == [["inv-1"], ["inv-2"]]


@pytest.mark.asyncio
async def test_terminal_recovery_rejects_incomplete_child_of_closed_legacy_turn():
    from execution.orchestrator.models import ToolAcceptance

    base = make_run()
    invocation = _a2a_invocation(run_id=base.run_id, call_id="call-legacy")
    acceptance = ToolAcceptance(
        acceptance_id="accepted-legacy",
        invocation_id=invocation.invocation_id,
        idempotency_key=invocation.idempotency_key,
        accepted_at=NOW,
    )
    run = base.model_copy(
        update={
            "status": "running",
            "active_internal_turn_id": None,
            "active_assistant_message_id": None,
            "tool_batches": [
                ToolCallBatch(
                    assistant_message_id="assistant-legacy",
                    internal_turn_id="turn-legacy",
                    entries=[
                        ToolBatchEntry(
                            call_id="call-legacy",
                            assistant_message_id="assistant-legacy",
                            source_index=0,
                            tool_name="fake_agent_pause",
                            state="input_required",
                            invocation=invocation,
                            acceptance=acceptance,
                            presented=True,
                            suspended_call_record_id="parent-legacy",
                            interaction_id="interaction-legacy",
                            interaction_fingerprint="fp-legacy",
                            opaque_public_call_id="inv-legacy",
                        )
                    ],
                )
            ],
        }
    )

    async def canonical_event_reader(_room_id, _run_id):
        return [
            {
                "room_seq": 9,
                "payload_public": {
                    "run_id": run.run_id,
                    "type": "turn_end",
                    "payload": {
                        "internal_turn_id": "turn-legacy",
                        "message_id": "assistant-legacy",
                        "tool_call_ids": ["inv-legacy"],
                        "status": "error",
                    },
                },
            }
        ]

    runtime = InteractionRuntime()
    kernel, _, _, _ = await make_kernel([], run=run, tool_runtime=runtime)
    kernel.canonical_event_reader = canonical_event_reader

    with pytest.raises(
        KernelConflict,
        match="canonical closed turn retains an incomplete Tool child",
    ):
        await kernel.terminalize(
            run.run_id,
            status="failed",
            reason="legacy recovery",
        )

    assert runtime.abandoned == []


@pytest.mark.asyncio
async def test_ensure_tool_batch_reconstructs_mixed_presented_batch():
    from execution.orchestrator.models import (
        AssistantMessage,
        ToolCall,
        ToolInteractionMessage,
    )

    run = make_run()
    assistant = AssistantMessage(
        message_id="assistant-mixed",
        content=[],
        tool_calls=[
            ToolCall(
                call_id="call-presented",
                tool_name="fake_agent_pause",
                arguments={"status": "input_required"},
            ),
            ToolCall(
                call_id="call-pending",
                tool_name="fake_agent_echo",
                arguments={"value": "ok"},
            ),
        ],
        finish_reason="tool_calls",
        usage=None,
        created_at=NOW,
    )
    interaction_message = ToolInteractionMessage(
        message_id="interaction:call-presented:fp-1",
        call_id="call-presented",
        tool_name="fake_agent_pause",
        interaction_id="interaction-1",
        interaction_fingerprint="fp-1",
        questions=[
            ToolInteractionQuestion(
                question_id="q1", prompt="Which?", answer_kind="text"
            )
        ],
        artifact_refs=[],
        agent_label="Fake Pausing Agent",
        created_at=NOW,
    )
    run = run.model_copy(
        update={
            "transcript": [*run.transcript, assistant, interaction_message],
            "active_internal_turn_id": "turn-1",
        }
    )
    kernel, store, _, _ = await make_kernel([], run=run)

    stored = await store.load(next(iter(store.runs)))
    assert stored is not None
    recovered = await kernel._ensure_tool_batch(stored, assistant)
    # Reconstruction succeeds for a batch whose presented call is already
    # surfaced (ToolInteractionMessage) and whose other call is still unresolved.
    assert any(
        batch.assistant_message_id == assistant.message_id
        for batch in recovered.tool_batches
    )


@pytest.mark.asyncio
async def test_restart_mid_join_redispatch_is_idempotent():
    """A join entry checkpointed before dispatch re-dispatches on re-entry.

    The join invocation is durable without an acceptance (join dispatch never
    goes through ToolRuntime.accept). Re-entering ``_execute_tool_batch`` after
    a restart must not raise ``KernelConflict`` and must re-dispatch the SAME
    invocation through ``dispatch_model_reply``.
    """
    from execution.orchestrator.models import (
        AssistantMessage,
        ToolBatchEntry,
        ToolCall,
        ToolCallBatch,
        ToolInteractionMessage,
    )

    runtime = InteractionRuntime()
    runtime.model_replies["call-2"] = ToolResult(
        call_id="call-2",
        tool_name="fake_agent_pause",
        status="completed",
        content=[TextPart(text="done")],
        artifact_refs=[],
    )

    run = make_run()
    assistant = AssistantMessage(
        message_id="assistant-join",
        content=[],
        tool_calls=[
            ToolCall(
                call_id="call-1",
                tool_name="fake_agent_pause",
                arguments={"status": "input_required"},
            ),
            ToolCall(
                call_id="call-2",
                tool_name="fake_agent_pause",
                arguments={"status": "input_required"},
            ),
        ],
        finish_reason="tool_calls",
        usage=None,
        created_at=NOW,
    )
    interaction_message = ToolInteractionMessage(
        message_id="interaction:call-1:fp-1",
        call_id="call-1",
        tool_name="fake_agent_pause",
        interaction_id="interaction-1",
        interaction_fingerprint="fp-1",
        questions=[
            ToolInteractionQuestion(
                question_id="q1", prompt="Which cloud provider?", answer_kind="text"
            )
        ],
        artifact_refs=[],
        agent_label="Fake Pausing Agent",
        created_at=NOW,
    )
    join_invocation = _a2a_invocation(run_id=run.run_id, call_id="call-2")
    run = run.model_copy(
        update={
            "transcript": [*run.transcript, assistant, interaction_message],
            "active_internal_turn_id": "turn-1",
            "tool_batches": [
                ToolCallBatch(
                    assistant_message_id="assistant-join",
                    internal_turn_id="turn-1",
                    entries=[
                        ToolBatchEntry(
                            call_id="call-1",
                            assistant_message_id="assistant-join",
                            source_index=0,
                            tool_name="fake_agent_pause",
                            state="input_required",  # type: ignore[arg-type]
                            presented=True,
                            suspended_call_record_id="parent-1",
                            interaction_id="interaction-1",
                            interaction_fingerprint="fp-1",
                            opaque_public_call_id="inv_call-1",
                        ),
                        ToolBatchEntry(
                            call_id="call-2",
                            assistant_message_id="assistant-join",
                            source_index=1,
                            tool_name="fake_agent_pause",
                            state="accepted",  # type: ignore[arg-type]
                            invocation=join_invocation,
                            acceptance=None,
                            opaque_public_call_id="inv_call-2",
                        ),
                    ],
                ),
            ],
        }
    )
    kernel, store, _, _ = await make_kernel([], run=run, tool_runtime=runtime)
    stored = await store.load(run.run_id)
    assert stored is not None

    result = await kernel._execute_tool_batch(
        stored, assistant, signal=NeverCancelled(), lifecycle=None
    )

    # No KernelConflict; the join re-dispatches the same invocation.
    assert runtime.model_reply_calls == [("call-2", "parent-1", None)]
    assert result is None
    recovered = await store.load(run.run_id)
    assert recovered is not None
    batch = next(
        b for b in recovered.tool_batches if b.assistant_message_id == "assistant-join"
    )
    by_call = {entry.call_id: entry for entry in batch.entries}
    assert by_call["call-1"].state == "terminal"
    assert by_call["call-2"].state == "terminal"
    assert by_call["call-2"].buffered_terminal_result is not None
    assert by_call["call-2"].buffered_terminal_result.status == "completed"


@pytest.mark.asyncio
async def test_surface_agent_questions_publishes_and_emits_forwarded_to_user():
    from unittest.mock import AsyncMock

    from execution.orchestrator.models import ToolObservation

    runtime = InteractionRuntime()
    runtime.suspensions["call-1"] = _interaction_suspension("call-1")
    kernel, store, _, _ = await make_kernel(
        [
            tool_events(("call-1", "fake_agent_pause", '{"status":"input_required"}')),
            tool_events(
                (
                    "call-2",
                    "surface_agent_questions",
                    "{}",
                )
            ),
            final_events("done"),
        ],
        tool_runtime=runtime,
        supervisor_hitl=AsyncMock(),
    )
    events: list[tuple[str, dict[str, object]]] = []

    async def lifecycle(event_type, _run, payload):
        events.append((event_type, payload))

    result = await kernel.run(
        next(iter(store.runs)), signal=NeverCancelled(), lifecycle=lifecycle
    )

    assert result.outcome == "awaiting_user"
    assert runtime.published == [("parent-1", "interaction-1")]

    decisions = _decision_payloads(events)
    forwarded = [d for d in decisions if d["decision"] == "forwarded_to_user"]
    assert len(forwarded) == 1
    assert forwarded[0]["agent_label"] == "fake_agent_pause"
    assert forwarded[0]["question_summary"] == "Which cloud provider?"
    public_event_text = repr(events)
    assert "prs_" not in public_event_text
    assert "interaction-1" not in public_event_text
    assert "parent-1" not in public_event_text

    surface = [
        entry
        for batch in result.run.tool_batches
        for entry in batch.entries
        if entry.call_id == "call-2"
    ][0]
    assert surface.state == "input_required"
    assert surface.presented is False
    assert surface.surface_for_call_record_id == "parent-1"
    assert surface.interaction_id == "interaction-1"

    # The user's answer flows through the parent continuation; observing the
    # parent terminal result must close the surface entry and resume the Run.
    answer = ToolObservation(
        observation_id="answer-1",
        invocation_id="call-1",
        outcome=ToolResult(
            call_id="call-1",
            tool_name="fake_agent_pause",
            status="completed",
            content=[TextPart(text="thanks")],
            artifact_refs=[],
        ),
        observed_at=NOW,
    )
    resumed = await kernel.observe_tool(
        next(iter(store.runs)), answer, signal=NeverCancelled(), lifecycle=None
    )
    assert resumed.outcome == "final_answer"
    parent = [
        entry
        for batch in resumed.run.tool_batches
        for entry in batch.entries
        if entry.call_id == "call-1"
    ][0]
    assert parent.state == "terminal"
    surface = [
        entry
        for batch in resumed.run.tool_batches
        for entry in batch.entries
        if entry.call_id == "call-2"
    ][0]
    assert surface.state == "terminal"
    assert surface.buffered_terminal_result is not None


@pytest.mark.asyncio
async def test_cross_batch_hitl_answer_restart_closes_latest_message_and_folds():  # noqa: C901
    from unittest.mock import AsyncMock

    from delivery.snapshot import RoomEventFold
    from execution.orchestrator.kernel import OrchestratorKernel
    from execution.orchestrator.lifecycle import SessionEvent
    from execution.orchestrator.models import ToolObservation
    from execution.orchestrator.public_projection import PublicProjectionTranslator

    runtime = InteractionRuntime()
    runtime.suspensions["call-1"] = _interaction_suspension("call-1").model_copy(
        update={
            "questions": [
                ToolInteractionQuestion(
                    question_id="security_training",
                    prompt="Has security training been rolled out?",
                    answer_kind="text",
                ),
                ToolInteractionQuestion(
                    question_id="cloud_providers",
                    prompt="Which cloud providers are used?",
                    answer_kind="text",
                ),
            ]
        }
    )
    kernel, store, _, _ = await make_kernel(
        [
            tool_events(("call-1", "fake_agent_pause", '{"status":"input_required"}')),
            tool_events(("call-2", "surface_agent_questions", "{}")),
            final_events("placement complete"),
        ],
        tool_runtime=runtime,
        supervisor_hitl=AsyncMock(),
    )
    run_id = next(iter(store.runs))
    initial_run = await store.load(run_id)
    assert initial_run is not None

    translator = PublicProjectionTranslator(lifecycle_family="canonical")
    records_by_id: dict[str, dict[str, object]] = {}
    emission_attempts: dict[str, int] = {}
    private_sequence = 0
    room_sequence = 0
    crash_tool_end = True
    crash_turn_end = True
    cross_turn_id: str | None = None

    class SimulatedRestart(RuntimeError):
        pass

    def append_record(event_id: str, record: dict[str, object]) -> None:
        nonlocal room_sequence
        emission_attempts[event_id] = emission_attempts.get(event_id, 0) + 1
        if event_id in records_by_id:
            return
        room_sequence += 1
        records_by_id[event_id] = {**record, "room_seq": room_sequence}

    async def lifecycle(event_type, run, payload):
        nonlocal private_sequence, crash_tool_end, crash_turn_end
        private_sequence += 1
        projected = translator.translate(
            SessionEvent(
                event_type=event_type,
                session_id=run.session_id,
                run_id=run.run_id,
                causation_id=run.request.user_message_id,
                sequence=private_sequence,
                timestamp=NOW,
                payload=payload,
                room_id=run.room_id,
                user_message_id=run.request.user_message_id,
                client_request_id=run.client_request_id,
                lifecycle_family="canonical",
            ),
            catalog=run.tool_catalog,
        )
        if projected is not None:
            append_record(
                projected.event_id,
                {
                    "room_id": run.room_id,
                    "kind": "run_event",
                    "ts": NOW.isoformat(),
                    "payload_public": {
                        "event_id": projected.event_id,
                        "run_id": projected.run_id,
                        "seq": projected.seq,
                        "type": projected.kind,
                        "payload": projected.payload,
                        "correlation_id": projected.client_request_id,
                    },
                },
            )
        if (
            crash_tool_end
            and event_type == "tool_execution_completed"
            and payload.get("internal_turn_id") == cross_turn_id
        ):
            crash_tool_end = False
            raise SimulatedRestart("after durable Tool end, before private checkpoint")
        if (
            crash_turn_end
            and event_type == "turn_completed"
            and payload.get("internal_turn_id") == cross_turn_id
        ):
            crash_turn_end = False
            raise SimulatedRestart("after durable turn_end, before private checkpoint")

    async def append_run_event(kind: str, payload: dict[str, object]) -> None:
        nonlocal private_sequence
        private_sequence += 1
        event_id = f"public:{run_id}:{kind}:fixture"
        append_record(
            event_id,
            {
                "room_id": initial_run.room_id,
                "kind": "run_event",
                "ts": NOW.isoformat(),
                "payload_public": {
                    "event_id": event_id,
                    "run_id": run_id,
                    "seq": private_sequence,
                    "type": kind,
                    "payload": payload,
                    "correlation_id": initial_run.client_request_id,
                },
            },
        )

    def append_hitl_event(
        kind: str, *, request_id: str, interaction_id: str, index: int
    ) -> None:
        event_id = f"{kind}:{interaction_id}:{request_id}"
        payload: dict[str, object] = {
            "request_id": request_id,
            "message_id": f"orchestrator:{run_id}:inv_parent",
            "source": "agent",
            "interaction_id": interaction_id,
            "question_index": index,
            "question_count": 2,
            "run_id": run_id,
            "related_user_message_id": initial_run.request.user_message_id,
            "client_request_id": initial_run.client_request_id,
        }
        if kind == "hitl_request":
            payload.update(
                {
                    "prompt": request_id.replace("_", " "),
                    "prompt_type": "text",
                }
            )
        else:
            payload.update(
                {
                    "status": "responded",
                    "answer_ref": f"answer-{request_id}",
                }
            )
        append_record(
            event_id,
            {
                "room_id": initial_run.room_id,
                "kind": kind,
                "ts": NOW.isoformat(),
                "payload_public": payload,
            },
        )

    await lifecycle("run_started", initial_run, {"mode": "ultimate"})
    waiting = await kernel.run(run_id, signal=NeverCancelled(), lifecycle=lifecycle)
    assert waiting.outcome == "awaiting_user"
    turn_ids = {
        batch.internal_turn_id
        for batch in waiting.run.tool_batches
        if batch.internal_turn_id is not None
    }
    assert len(turn_ids) == 1
    cross_turn_id = next(iter(turn_ids))
    parent_batch, surface_batch = waiting.run.tool_batches
    assert parent_batch.assistant_message_id != surface_batch.assistant_message_id

    interaction_id = "cyber-broker:typed-questions"
    for index, request_id in enumerate(("security_training", "cloud_providers")):
        append_hitl_event(
            "hitl_request",
            request_id=request_id,
            interaction_id=interaction_id,
            index=index,
        )
    await append_run_event(
        "run_waiting_input",
        {
            "interaction_id": interaction_id,
            "request_ids": ["security_training", "cloud_providers"],
            "requested_at": NOW.isoformat(),
        },
    )
    for index, request_id in enumerate(("security_training", "cloud_providers")):
        append_hitl_event(
            "hitl_response",
            request_id=request_id,
            interaction_id=interaction_id,
            index=index,
        )
    await append_run_event(
        "run_resumed",
        {
            "interaction_id": interaction_id,
            "resolved_request_ids": ["security_training", "cloud_providers"],
            "resumed_at": NOW.isoformat(),
        },
    )

    real_publish = kernel._publish_checkpointed_tool_terminals

    async def crash_before_public_terminals(*args, **kwargs):
        raise SimulatedRestart("after answer checkpoint, before public Tool ends")

    kernel._publish_checkpointed_tool_terminals = crash_before_public_terminals  # type: ignore[method-assign]
    answer = ToolObservation(
        observation_id="typed-answer-terminal",
        invocation_id="call-1",
        outcome=ToolResult(
            call_id="call-1",
            tool_name="fake_agent_pause",
            status="completed",
            content=[TextPart(text="draft revision 2")],
            artifact_refs=[],
        ),
        observed_at=NOW,
    )
    with pytest.raises(SimulatedRestart, match="after answer checkpoint"):
        await kernel.observe_tool(
            run_id, answer, signal=NeverCancelled(), lifecycle=lifecycle
        )
    kernel._publish_checkpointed_tool_terminals = real_publish  # type: ignore[method-assign]
    checkpointed = await store.load(run_id)
    assert checkpointed is not None
    assert all(
        entry.state == "terminal"
        for batch in checkpointed.tool_batches
        for entry in batch.entries
    )

    async def read_canonical_events(_room_id: str, _run_id: str):
        return list(records_by_id.values())

    def restarted_kernel() -> OrchestratorKernel:
        return OrchestratorKernel(
            run_store=store,
            model_runtime=kernel.model_runtime,
            tool_runtime=kernel.tool_runtime,
            tool_catalog=kernel.tool_catalog,
            context_compiler=kernel.context_compiler,
            budget_policy=kernel.budget_policy,
            projection_driver=kernel.projection_driver,
            clock=kernel.clock,
            id_factory=kernel.id_factory,
            supervisor_hitl=kernel.supervisor_hitl,
            canonical_event_reader=read_canonical_events,
        )

    completed = None
    restart_count = 0
    while completed is None:
        restart_count += 1
        assert restart_count <= 3
        try:
            completed = await restarted_kernel().run(
                run_id, signal=NeverCancelled(), lifecycle=lifecycle
            )
        except SimulatedRestart:
            continue
    assert restart_count == 3
    assert completed.outcome == "final_answer"

    final_message_id = completed.run.proposed_final_message_id
    assert final_message_id is not None
    append_record(
        f"agent_response:{final_message_id}",
        {
            "room_id": completed.run.room_id,
            "kind": "agent_response",
            "ts": NOW.isoformat(),
            "payload_public": {
                "message_id": final_message_id,
                "content": "placement complete",
                "client_request_id": completed.run.client_request_id,
                "related_message_id": completed.run.request.user_message_id,
            },
        },
    )
    await append_run_event(
        "run_settled",
        {
            "status": "completed",
            "started_at": completed.run.created_at.isoformat(),
            "settled_at": completed.run.updated_at.isoformat(),
            "duration_ms": 0,
            "final_message_id": final_message_id,
        },
    )

    projected = [
        record["payload_public"]
        for record in records_by_id.values()
        if record["kind"] == "run_event"
    ]
    cross_turn_ends = [
        event
        for event in projected
        if event["type"] == "turn_end"
        and event["payload"].get("internal_turn_id") == cross_turn_id
    ]
    assert len(cross_turn_ends) == 1
    assert cross_turn_ends[0]["payload"]["message_id"] == (
        surface_batch.assistant_message_id
    )
    assert cross_turn_ends[0]["payload"]["tool_call_ids"] == [
        parent_batch.entries[0].opaque_public_call_id,
        surface_batch.entries[0].opaque_public_call_id,
    ]
    cross_tool_ends = [
        event
        for event in projected
        if event["type"] == "tool_execution_end"
        and event["payload"].get("internal_turn_id") == cross_turn_id
    ]
    assert len(cross_tool_ends) == 2
    retried_end_id = next(
        event["event_id"]
        for event in cross_tool_ends
        if event["payload"]["tool_call_id"]
        == parent_batch.entries[0].opaque_public_call_id
    )
    assert emission_attempts[retried_end_id] == 2
    assert len(records_by_id) == len(set(records_by_id))

    fold = RoomEventFold()
    for record in records_by_id.values():
        assert fold.apply(record), (record, fold.state(room_seq=room_sequence))
    state = fold.state(room_seq=room_sequence)
    turn = state["turns"][0]
    assert turn["state"] == "completed"
    assert turn["active_interaction_id"] is None
    interaction = next(
        item
        for item in turn["hitl_interactions"]
        if item["interaction_id"] == interaction_id
    )
    assert interaction["state"] == "resumed"
    assert [request["status"] for request in interaction["requests"]] == [
        "responded",
        "responded",
    ]


@pytest.mark.asyncio
async def test_follow_up_interaction_gets_fresh_model_first_presentation():
    from unittest.mock import AsyncMock

    from execution.orchestrator.models import ToolObservation

    runtime = InteractionRuntime()
    runtime.suspensions["call-1"] = _interaction_suspension("call-1")
    kernel, store, _, _ = await make_kernel(
        [
            tool_events(("call-1", "fake_agent_pause", '{"status":"input_required"}')),
            tool_events(("call-2", "surface_agent_questions", "{}")),
            tool_events(("call-3", "surface_agent_questions", "{}")),
        ],
        tool_runtime=runtime,
        supervisor_hitl=AsyncMock(),
    )
    run_id = next(iter(store.runs))

    first = await kernel.run(run_id, signal=NeverCancelled(), lifecycle=None)
    assert first.outcome == "awaiting_user"
    first_parent = next(
        entry
        for batch in first.run.tool_batches
        for entry in batch.entries
        if entry.call_id == "call-1"
    )
    first_presentation = first_parent.presentation_id

    follow_up = ToolObservation(
        observation_id="follow-up-2",
        invocation_id="call-1",
        outcome=_interaction_suspension(
            "call-1",
            interaction_id="interaction-2",
            fingerprint="fp-2",
            prompt="Which region?",
        ),
        observed_at=NOW,
    )
    second = await kernel.observe_tool(
        run_id, follow_up, signal=NeverCancelled(), lifecycle=None
    )

    assert second.outcome == "awaiting_user"
    assert runtime.published == [
        ("parent-1", "interaction-1"),
        ("parent-1", "interaction-2"),
    ]
    parent = next(
        entry
        for batch in second.run.tool_batches
        for entry in batch.entries
        if entry.call_id == "call-1"
    )
    assert parent.interaction_id == "interaction-2"
    assert parent.presentation_id is not None
    assert parent.presentation_id != first_presentation
    surfaces = {
        entry.call_id: entry
        for batch in second.run.tool_batches
        for entry in batch.entries
        if entry.surface_for_call_record_id == "parent-1"
    }
    assert surfaces["call-2"].state == "terminal"
    assert surfaces["call-3"].state == "input_required"
    interaction_messages = [
        message
        for message in second.run.transcript
        if isinstance(message, ToolInteractionMessage)
    ]
    assert [message.interaction_id for message in interaction_messages] == [
        "interaction-1",
        "interaction-2",
    ]


@pytest.mark.asyncio
async def test_real_continuation_ingress_preserves_follow_up_for_kernel_surface():
    from execution.orchestrator.a2a_runtime.ingress import A2AObservationProcessor
    from execution.orchestrator.a2a_runtime.models import (
        A2ADispatchReceipt,
        NormalizedA2AObservation,
    )
    from tests.test_orchestrator_a2a_hitl_recovery_auth import (
        Dispatch,
        questionnaire_answers,
        questionnaire_spec,
        setup_waiting,
    )

    second_spec = questionnaire_spec("interaction-2")
    dispatch = Dispatch()
    coordinator, ledger, hitl, _, dispatch, call, route = await setup_waiting(
        dispatch=dispatch
    )
    current = await ledger.load_by_record_id(call.call_record_id)
    assert current is not None
    dispatch.interaction_receipt = A2ADispatchReceipt(
        outcome="interaction",
        task_id=current.a2a_task_id,
        context_id=current.a2a_context_id,
        interaction_observation=NormalizedA2AObservation(
            observation_id="obs-real-second-round",
            call_record_id=current.call_record_id,
            source_kind="direct",
            source_identity="direct:endpoint:task-1:input_required:real-second",
            binding_scope=current.endpoint_scope_digest,
            event_kind="input_required",
            observed_at=NOW,
            task_id=current.a2a_task_id,
            context_id=current.a2a_context_id,
            agent_id=current.agent_id,
            content=[],
            artifact_refs=[],
            interaction_spec=second_spec.model_dump(mode="json"),
        ),
    )

    runtime = InteractionRuntime()
    runtime.suspensions["call-1"] = _interaction_suspension(
        "call-1",
        call_record_id=call.call_record_id,
        interaction_id="interaction-1",
        fingerprint=current.interaction_fingerprint or "",
        prompt="Which option?",
    )
    real_publish = runtime.publish_parked_interaction

    async def publish_exact_interaction(
        *, call_record_id: str, interaction_id: str
    ) -> None:
        await real_publish(call_record_id=call_record_id, interaction_id=interaction_id)
        assert await hitl.publish(interaction_id, call_record_id=call_record_id) in {
            "accepted",
            "replayed",
        }

    runtime.publish_parked_interaction = publish_exact_interaction  # type: ignore[method-assign]
    kernel, store, _, _ = await make_kernel(
        [
            tool_events(("call-1", "fake_agent_pause", '{"status":"input_required"}')),
            tool_events(("call-2", "surface_agent_questions", "{}")),
            tool_events(("call-3", "surface_agent_questions", "{}")),
        ],
        tool_runtime=runtime,
        supervisor_hitl=object(),
    )
    run_id = next(iter(store.runs))
    first = await kernel.run(run_id, signal=NeverCancelled(), lifecycle=None)
    assert first.outcome == "awaiting_user"
    first_parent = next(
        entry
        for batch in first.run.tool_batches
        for entry in batch.entries
        if entry.call_id == "call-1"
    )
    first_presentation = first_parent.presentation_id

    assert (
        await coordinator.resume(
            call_record_id=call.call_record_id,
            interaction_id="interaction-1",
            interaction_revision=1,
            route_fingerprint=route.fingerprint,
            answers=questionnaire_answers(),
            authenticated_answerer_id="user-1",
        )
        == "input_required"
    )
    first_command = dispatch.commands[0]
    second_record = await ledger.load_by_record_id(call.call_record_id)
    assert second_record is not None
    assert second_record.pending_interaction_id == "interaction-2"
    assert await hitl.get_published_interactions(call.room_id) == []

    class Artifacts:
        async def materialize(self, *args, **kwargs):
            return []

        async def materialize_inbound_artifacts(self, **kwargs):
            return list(kwargs["artifact_refs"])

    class Checkpoints:
        async def is_suspension_checkpointed(self, _run_id, _invocation_id, status):
            return status == "input_required"

        async def is_acceptance_checkpointed(self, *args):
            return True

    class Outcomes:
        processed = False

        async def is_run_terminal(self, *args):
            return False

        async def has_processed_observation(self, *args):
            return self.processed

        async def is_outcome_checkpointed(self, *args):
            return self.processed

    outcomes = Outcomes()
    delivered = []

    class KernelSink:
        async def deliver(self, delivered_run_id, observation):
            assert delivered_run_id == run_id
            delivered.append(observation)
            result = await kernel.observe_tool(
                delivered_run_id,
                observation,
                signal=NeverCancelled(),
                lifecycle=None,
            )
            assert result.outcome == "awaiting_user"
            outcomes.processed = True

    ingress = coordinator.observations
    processor = A2AObservationProcessor(
        inbox=ingress.inbox,
        conflicts=ingress.conflicts,
        ledger=ledger,
        room_epochs=coordinator.room_epochs,
        artifacts=Artifacts(),
        hitl=hitl,
        sink=KernelSink(),
        checkpoint_reader=Checkpoints(),
        outcome_reader=outcomes,
    )
    assert await processor.process("obs-real-second-round") == "accepted"
    assert len(delivered) == 1
    suspension = delivered[0].outcome
    assert suspension.call_record_id == call.call_record_id
    assert suspension.interaction_id == "interaction-2"
    assert suspension.interaction_fingerprint == second_record.interaction_fingerprint
    assert [question.question_id for question in suspension.questions] == ["q1"]

    surfaced = await store.load(run_id)
    parent = next(
        entry
        for batch in surfaced.tool_batches
        for entry in batch.entries
        if entry.call_id == "call-1"
    )
    assert parent.interaction_id == "interaction-2"
    assert parent.presentation_id is not None
    assert parent.presentation_id != first_presentation
    assert [
        interaction.interaction_id
        for interaction, _route, _fingerprint in await hitl.get_published_interactions(
            call.room_id
        )
    ] == ["interaction-2"]

    dispatch.interaction_receipt = None
    second_stored = hitl.read_interaction_for_test("interaction-2")
    assert second_stored is not None
    second_state = await coordinator.resume(
        call_record_id=call.call_record_id,
        interaction_id="interaction-2",
        interaction_revision=1,
        route_fingerprint=second_stored[1].fingerprint,
        answers=questionnaire_answers(),
        authenticated_answerer_id="user-1",
    )
    final = await ledger.load_by_record_id(call.call_record_id)
    assert final is not None
    assert second_state == final.state == "working"
    assert len(dispatch.commands) == 2
    assert dispatch.commands[1].command_id != first_command.command_id
    assert dispatch.commands[1].task_id == first_command.task_id == "task-1"
    assert dispatch.commands[1].context_id == first_command.context_id == "context-1"


@pytest.mark.asyncio
async def test_surface_agent_questions_unknown_interaction_rejected():
    from unittest.mock import AsyncMock

    runtime = InteractionRuntime()
    runtime.suspensions["call-1"] = _interaction_suspension("call-1")
    kernel, store, _, _ = await make_kernel(
        [
            tool_events(("call-1", "fake_agent_pause", '{"status":"input_required"}')),
            tool_events(
                ("call-2", "surface_agent_questions", '{"presentation_id":"missing"}')
            ),
            final_events("done"),
        ],
        tool_runtime=runtime,
        supervisor_hitl=AsyncMock(),
    )

    result = await kernel.run(
        next(iter(store.runs)), signal=NeverCancelled(), lifecycle=None
    )

    surface = [
        entry
        for batch in result.run.tool_batches
        for entry in batch.entries
        if entry.call_id == "call-2"
    ][0]
    assert surface.state == "terminal"
    assert surface.buffered_terminal_result is not None
    assert surface.buffered_terminal_result.error_code == "invalid_tool_call"
    # The surface tool did not publish the (unknown) target. Any later F5
    # degrade publication is a separate decision-turn path, not this rejection.


@pytest.mark.asyncio
async def test_request_user_input_placeholder_choices_rejected():
    from unittest.mock import AsyncMock

    supervisor_hitl = AsyncMock()
    runtime = InteractionRuntime()
    runtime.suspensions["call-1"] = _interaction_suspension("call-1")
    kernel, store, _, _ = await make_kernel(
        [
            tool_events(("call-1", "fake_agent_pause", '{"status":"input_required"}')),
            tool_events(
                (
                    "call-2",
                    "request_user_input",
                    '{"question":"Which one?","choices":["Training: yes; Cloud: ...","Other / partial"]}',
                )
            ),
            final_events("done"),
        ],
        tool_runtime=runtime,
        supervisor_hitl=supervisor_hitl,
    )

    result = await kernel.run(
        next(iter(store.runs)), signal=NeverCancelled(), lifecycle=None
    )

    # Placeholder choices are rejected as an invalid declaration; the supervisor
    # HITL port is never called and the Run can continue to a final answer.
    supervisor_hitl.assert_not_awaited()
    ask = [
        entry
        for batch in result.run.tool_batches
        for entry in batch.entries
        if entry.call_id == "call-2"
    ][0]
    assert ask.state == "terminal"
    assert ask.buffered_terminal_result is not None
    assert ask.buffered_terminal_result.error_code == "invalid_tool_call"


@pytest.mark.asyncio
async def test_request_user_input_rejected_while_agent_questions_pending():
    from unittest.mock import AsyncMock

    supervisor_hitl = AsyncMock()
    runtime = InteractionRuntime()
    runtime.suspensions["call-1"] = _interaction_suspension("call-1")
    kernel, store, _, _ = await make_kernel(
        [
            tool_events(("call-1", "fake_agent_pause", '{"status":"input_required"}')),
            tool_events(
                (
                    "call-2",
                    "request_user_input",
                    '{"question":"Please reply with both answers."}',
                )
            ),
            final_events("done"),
        ],
        tool_runtime=runtime,
        supervisor_hitl=supervisor_hitl,
    )
    events: list[tuple[str, dict[str, object]]] = []

    async def lifecycle(event_type, _run, payload):
        events.append((event_type, payload))

    result = await kernel.run(
        next(iter(store.runs)), signal=NeverCancelled(), lifecycle=lifecycle
    )

    # A composed single-question ask is rejected while the Agent's questions
    # are parked; the supervisor HITL port is never reached.
    supervisor_hitl.assert_not_awaited()
    ask = [
        entry
        for batch in result.run.tool_batches
        for entry in batch.entries
        if entry.call_id == "call-2"
    ][0]
    assert ask.state == "terminal"
    assert ask.buffered_terminal_result is not None
    assert ask.buffered_terminal_result.error_code == "invalid_tool_call"

    # No public tool row was opened for the rejected declaration.
    started_calls = [
        payload.get("call_id")
        for event_type, payload in events
        if event_type == "tool_execution_started"
    ]
    assert "call-2" not in started_calls

    # The parked Agent question is still open and unanswered.
    parent = [
        entry
        for batch in result.run.tool_batches
        for entry in batch.entries
        if entry.call_id == "call-1"
    ][0]
    assert parent.state == "input_required"
    assert parent.presented is True


@pytest.mark.asyncio
async def test_rejected_ask_then_surface_forward_accepted():
    from unittest.mock import AsyncMock

    supervisor_hitl = AsyncMock()
    runtime = InteractionRuntime()
    runtime.suspensions["call-1"] = _interaction_suspension("call-1")
    kernel, store, _, _ = await make_kernel(
        [
            tool_events(("call-1", "fake_agent_pause", '{"status":"input_required"}')),
            tool_events(
                (
                    "call-2",
                    "request_user_input",
                    '{"question":"Please reply with both answers."}',
                )
            ),
            tool_events(
                (
                    "call-3",
                    "surface_agent_questions",
                    "{}",
                )
            ),
            final_events("done"),
        ],
        tool_runtime=runtime,
        supervisor_hitl=supervisor_hitl,
    )
    events: list[tuple[str, dict[str, object]]] = []

    async def lifecycle(event_type, _run, payload):
        events.append((event_type, payload))

    result = await kernel.run(
        next(iter(store.runs)), signal=NeverCancelled(), lifecycle=lifecycle
    )

    # The rejected merged ask never reached the HITL port; the retry via the
    # verbatim forward tool is accepted and publishes the Agent's questions.
    supervisor_hitl.assert_not_awaited()
    assert result.outcome == "awaiting_user"
    assert runtime.published == [("parent-1", "interaction-1")]
    ask = [
        entry
        for batch in result.run.tool_batches
        for entry in batch.entries
        if entry.call_id == "call-2"
    ][0]
    assert ask.state == "terminal"
    assert ask.buffered_terminal_result.error_code == "invalid_tool_call"
    surface = [
        entry
        for batch in result.run.tool_batches
        for entry in batch.entries
        if entry.call_id == "call-3"
    ][0]
    assert surface.state == "input_required"
    assert surface.surface_for_call_record_id == "parent-1"

    decisions = _decision_payloads(events)
    assert any(d["decision"] == "forwarded_to_user" for d in decisions)
    # The rejected local declaration remains a private diagnostic. The parked
    # parent keeps one canonical internal turn open through the valid retry.
    assert len([payload for kind, payload in events if kind == "turn_started"]) == 1
    assert [payload for kind, payload in events if kind == "turn_completed"] == []
    owned_turn_ids = {
        batch.internal_turn_id
        for batch in result.run.tool_batches
        if batch.internal_turn_id is not None
    }
    assert len(owned_turn_ids) == 1

    # Exercise the production private→public translator and fail-closed
    # snapshot fold over the complete incident event stream.
    from delivery.snapshot import RoomEventFold
    from execution.orchestrator.lifecycle import SessionEvent
    from execution.orchestrator.public_projection import PublicProjectionTranslator

    translator = PublicProjectionTranslator(lifecycle_family="canonical")
    fold = RoomEventFold()
    started = translator.translate(
        SessionEvent(
            event_type="run_started",
            session_id=result.run.session_id,
            run_id=result.run.run_id,
            causation_id=result.run.request.user_message_id,
            sequence=1,
            timestamp=NOW,
            payload={"mode": "ultimate"},
            room_id=result.run.room_id,
            user_message_id=result.run.request.user_message_id,
            client_request_id=result.run.client_request_id,
            lifecycle_family="canonical",
        ),
        catalog=result.run.tool_catalog,
    )
    assert started is not None
    assert fold.apply(
        {
            "room_id": result.run.room_id,
            "room_seq": 1,
            "kind": "run_event",
            "ts": NOW.isoformat(),
            "payload_public": {
                "event_id": started.event_id,
                "run_id": started.run_id,
                "seq": started.seq,
                "type": started.kind,
                "payload": started.payload,
                "correlation_id": started.client_request_id,
            },
        }
    )
    room_seq = 1
    for sequence, (event_type, payload) in enumerate(events, start=2):
        projected = translator.translate(
            SessionEvent(
                event_type=event_type,
                session_id=result.run.session_id,
                run_id=result.run.run_id,
                causation_id=result.run.request.user_message_id,
                sequence=sequence,
                timestamp=NOW,
                payload=payload,
                room_id=result.run.room_id,
                user_message_id=result.run.request.user_message_id,
                client_request_id=result.run.client_request_id,
                lifecycle_family="canonical",
            ),
            catalog=result.run.tool_catalog,
        )
        if projected is None:
            continue
        room_seq += 1
        assert fold.apply(
            {
                "room_id": result.run.room_id,
                "room_seq": room_seq,
                "kind": "run_event",
                "ts": NOW.isoformat(),
                "payload_public": {
                    "event_id": projected.event_id,
                    "run_id": projected.run_id,
                    "seq": projected.seq,
                    "type": projected.kind,
                    "payload": projected.payload,
                    "correlation_id": projected.client_request_id,
                },
            }
        )


@pytest.mark.asyncio
async def test_request_user_input_unaffected_without_presented_interactions():
    from unittest.mock import AsyncMock

    supervisor_hitl = AsyncMock()
    runtime = InteractionRuntime()
    kernel, store, _, _ = await make_kernel(
        [
            tool_events(("call-1", "request_user_input", '{"question":"Which city?"}')),
            final_events("done"),
        ],
        tool_runtime=runtime,
        supervisor_hitl=supervisor_hitl,
    )

    result = await kernel.run(
        next(iter(store.runs)), signal=NeverCancelled(), lifecycle=None
    )

    # With no parked Agent interaction, request_user_input behaves as today.
    assert result.outcome == "awaiting_user"
    supervisor_hitl.assert_awaited_once()


@pytest.mark.asyncio
async def test_placeholder_ask_rejection_leaves_no_open_tool_row():
    from unittest.mock import AsyncMock

    supervisor_hitl = AsyncMock()
    runtime = InteractionRuntime()
    runtime.suspensions["call-1"] = _interaction_suspension("call-1")
    kernel, store, _, _ = await make_kernel(
        [
            tool_events(("call-1", "fake_agent_pause", '{"status":"input_required"}')),
            tool_events(
                (
                    "call-2",
                    "request_user_input",
                    '{"question":"Which one?","choices":["Training: yes; Cloud: ...","Other / partial"]}',
                )
            ),
            final_events("done"),
        ],
        tool_runtime=runtime,
        supervisor_hitl=supervisor_hitl,
    )
    events: list[tuple[str, dict[str, object]]] = []

    async def lifecycle(event_type, _run, payload):
        events.append((event_type, payload))

    result = await kernel.run(
        next(iter(store.runs)), signal=NeverCancelled(), lifecycle=lifecycle
    )

    supervisor_hitl.assert_not_awaited()
    # The rejected declaration never opens a public tool row, so the fold's
    # turn_end inventory (open_calls) can close without a phantom "running" ask.
    started_calls = [
        payload.get("call_id")
        for event_type, payload in events
        if event_type == "tool_execution_started"
    ]
    assert "call-2" not in started_calls

    ask = [
        entry
        for batch in result.run.tool_batches
        for entry in batch.entries
        if entry.call_id == "call-2"
    ][0]
    assert ask.state == "terminal"
    assert ask.buffered_terminal_result is not None
    assert ask.buffered_terminal_result.error_code == "invalid_tool_call"


@pytest.mark.asyncio
async def test_surface_unknown_interaction_rejection_leaves_no_open_tool_row():
    from unittest.mock import AsyncMock

    runtime = InteractionRuntime()
    runtime.suspensions["call-1"] = _interaction_suspension("call-1")
    kernel, store, _, _ = await make_kernel(
        [
            tool_events(("call-1", "fake_agent_pause", '{"status":"input_required"}')),
            tool_events(
                ("call-2", "surface_agent_questions", '{"presentation_id":"missing"}')
            ),
            final_events("done"),
        ],
        tool_runtime=runtime,
        supervisor_hitl=AsyncMock(),
    )
    events: list[tuple[str, dict[str, object]]] = []

    async def lifecycle(event_type, _run, payload):
        events.append((event_type, payload))

    result = await kernel.run(
        next(iter(store.runs)), signal=NeverCancelled(), lifecycle=lifecycle
    )

    # The unknown target was never forwarded; any later publish is the
    # separate F5 degrade path publishing the real parked parent.
    assert ("parent-1", "missing") not in runtime.published
    started_calls = [
        payload.get("call_id")
        for event_type, payload in events
        if event_type == "tool_execution_started"
    ]
    assert "call-2" not in started_calls

    surface = [
        entry
        for batch in result.run.tool_batches
        for entry in batch.entries
        if entry.call_id == "call-2"
    ][0]
    assert surface.state == "terminal"
    assert surface.buffered_terminal_result is not None
    assert surface.buffered_terminal_result.error_code == "invalid_tool_call"


@pytest.mark.asyncio
async def test_surface_publication_failure_leaves_no_open_tool_row():
    from unittest.mock import AsyncMock

    runtime = InteractionRuntime()
    runtime.suspensions["call-1"] = _interaction_suspension("call-1")

    async def fail_publish(*, call_record_id, interaction_id):
        raise RuntimeError("publish boom")

    runtime.publish_parked_interaction = fail_publish  # type: ignore[method-assign]

    kernel, store, _, _ = await make_kernel(
        [
            tool_events(("call-1", "fake_agent_pause", '{"status":"input_required"}')),
            tool_events(
                (
                    "call-2",
                    "surface_agent_questions",
                    "{}",
                )
            ),
            final_events("done"),
        ],
        tool_runtime=runtime,
        supervisor_hitl=AsyncMock(),
    )
    events: list[tuple[str, dict[str, object]]] = []

    async def lifecycle(event_type, _run, payload):
        events.append((event_type, payload))

    result = await kernel.run(
        next(iter(store.runs)), signal=NeverCancelled(), lifecycle=lifecycle
    )

    started_calls = [
        payload.get("call_id")
        for event_type, payload in events
        if event_type == "tool_execution_started"
    ]
    assert "call-2" not in started_calls

    surface = [
        entry
        for batch in result.run.tool_batches
        for entry in batch.entries
        if entry.call_id == "call-2"
    ][0]
    assert surface.state == "terminal"
    assert surface.buffered_terminal_result is not None
    assert surface.buffered_terminal_result.error_code == "surface_publication_failed"

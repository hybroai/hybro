from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import TypeAdapter, ValidationError

from approval.exceptions import HITLConflictError
from common.dto.delivery import DeliveryEmitStatus
from common.dto.hitl import A2AInteractionSpec, HITLQuestionAnswer, HITLRouteSnapshot
from execution.orchestrator.a2a_runtime.hitl import A2AContinuationCoordinator
from execution.orchestrator.a2a_runtime.interaction_outcome import (
    emit_hitl_request_events,
    emit_hitl_resolved_events,
)
from execution.orchestrator.a2a_runtime.models import (
    HITLRouteSnapshotUnion,
    HITLRouteSnapshotV2,
    NormalizedA2AObservation,
)
from execution.orchestrator_routing import DualRuntimeRouter, _map_interaction_answers
from tests.orchestrator_harness.a2a_in_memory import InMemoryHITLApplicationPort

from ._orchestrator_a2a_helpers import ledger_record


def interaction():
    return A2AInteractionSpec.model_validate(
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


def test_legacy_batch_requires_every_typed_question_with_safe_conflict():
    spec = A2AInteractionSpec.model_validate(
        {
            "schema_version": 1,
            "interaction_id": "interaction-2",
            "questions": [
                {
                    "question_id": "security_training",
                    "interaction_kind": "questionnaire",
                    "prompt": "Is training in place?",
                    "answer_kind": "text",
                },
                {
                    "question_id": "cloud_providers",
                    "interaction_kind": "questionnaire",
                    "prompt": "Which cloud providers?",
                    "answer_kind": "text",
                },
            ],
        }
    )

    with pytest.raises(HITLConflictError, match="changed before submission"):
        _map_interaction_answers(
            spec,
            [{"request_id": "cloud_providers", "user_input": "AWS, Azure"}],
        )

    mapped = _map_interaction_answers(
        spec,
        [
            {"request_id": "security_training", "user_input": "Yes"},
            {"request_id": "cloud_providers", "user_input": "AWS, Azure"},
        ],
    )
    assert [answer.question_id for answer in mapped] == [
        "security_training",
        "cloud_providers",
    ]


async def test_terminal_continuation_proof_clears_initial_delivery_uncertainty():
    spec = A2AInteractionSpec.model_validate(
        {
            "schema_version": 1,
            "interaction_id": "interaction-1",
            "questions": [
                {
                    "question_id": "q1",
                    "interaction_kind": "questionnaire",
                    "prompt": "Answer",
                    "answer_kind": "text",
                }
            ],
        }
    )
    route = HITLRouteSnapshotV2(
        orchestration_run_id="run-1",
        call_record_id="record-1",
        invocation_id="call-1",
        conversation_id="room-1",
        room_epoch=1,
        binding_id="binding-1",
        agent_id="agent-1",
        task_id="task-1",
        context_id="context-1",
        interaction_revision=1,
        interaction_fingerprint="fingerprint",
    )
    waiting_call = SimpleNamespace(
        state="delivery_uncertain",
        recent_observation_ids=["old-input-required"],
        answer_applied=SimpleNamespace(
            interaction_id="interaction-1",
            interaction_revision=1,
        ),
    )
    completed_call = SimpleNamespace(
        state="completed",
        recent_observation_ids=["terminal-with-artifacts"],
        answer_applied=waiting_call.answer_applied,
    )
    terminal_inbox = SimpleNamespace(
        observation_id="terminal-with-artifacts",
        observation=SimpleNamespace(
            call_record_id="record-1",
            event_kind="terminal",
            observed_at="2026-08-27T00:00:00Z",
        ),
    )
    runtime = SimpleNamespace(
        run_store=SimpleNamespace(
            load=AsyncMock(return_value=SimpleNamespace(status="running"))
        ),
        hitl_port=SimpleNamespace(
            read_interaction=AsyncMock(return_value=(spec, route, "fingerprint"))
        ),
        continuation=SimpleNamespace(
            resume=AsyncMock(return_value="delivery_uncertain")
        ),
        call_ledger=SimpleNamespace(
            load_by_record_id=AsyncMock(side_effect=[waiting_call, completed_call])
        ),
        observation_inbox=SimpleNamespace(
            list_due_for_call=AsyncMock(return_value=[terminal_inbox])
        ),
        observation_processor=SimpleNamespace(process=AsyncMock()),
    )
    router = DualRuntimeRouter(runtime=runtime)

    result = await router.route_hitl_answer(
        interaction_id="interaction-1",
        answers=[{"request_id": "q1", "user_input": "yes"}],
        responder_id="user-1",
        conversation_id="room-1",
    )

    assert result == "completed"
    assert runtime.call_ledger.load_by_record_id.await_count == 2
    runtime.observation_processor.process.assert_awaited_once_with(
        "terminal-with-artifacts"
    )


@pytest.mark.parametrize(
    ("pending_interaction_id", "interaction_revision"),
    [("interaction-2", 1), ("interaction-1", 2)],
)
async def test_follow_up_challenge_proves_continuation_delivery(
    pending_interaction_id, interaction_revision
):
    """A new parked round after the answer proves the answer reached the Agent."""
    spec = A2AInteractionSpec.model_validate(
        {
            "schema_version": 1,
            "interaction_id": "interaction-1",
            "questions": [
                {
                    "question_id": "q1",
                    "interaction_kind": "questionnaire",
                    "prompt": "Answer",
                    "answer_kind": "text",
                }
            ],
        }
    )
    route = HITLRouteSnapshotV2(
        orchestration_run_id="run-1",
        call_record_id="record-1",
        invocation_id="call-1",
        conversation_id="room-1",
        room_epoch=1,
        binding_id="binding-1",
        agent_id="agent-1",
        task_id="task-1",
        context_id="context-1",
        interaction_revision=1,
        interaction_fingerprint="fingerprint",
    )
    follow_up = SimpleNamespace(
        state="input_required",
        pending_interaction_id=pending_interaction_id,
        interaction_revision=interaction_revision,
        recent_observation_ids=[],
        answer_applied=None,
    )
    runtime = SimpleNamespace(
        run_store=SimpleNamespace(
            load=AsyncMock(return_value=SimpleNamespace(status="awaiting_user"))
        ),
        hitl_port=SimpleNamespace(
            read_interaction=AsyncMock(return_value=(spec, route, "fingerprint"))
        ),
        continuation=SimpleNamespace(
            resume=AsyncMock(return_value="delivery_uncertain")
        ),
        call_ledger=SimpleNamespace(
            load_by_record_id=AsyncMock(return_value=follow_up)
        ),
        observation_inbox=SimpleNamespace(list_due_for_call=AsyncMock(return_value=[])),
        observation_processor=SimpleNamespace(process=AsyncMock()),
    )
    router = DualRuntimeRouter(runtime=runtime)

    result = await router.route_hitl_answer(
        interaction_id="interaction-1",
        answers=[{"request_id": "q1", "user_input": "yes"}],
        responder_id="user-1",
        conversation_id="room-1",
    )

    assert result == "input_required"


async def test_unproven_continuation_still_reports_delivery_uncertain():
    from approval.exceptions import HITLDeliveryUncertainError

    spec = interaction()
    route = HITLRouteSnapshotV2(
        orchestration_run_id="run-1",
        call_record_id="record-1",
        invocation_id="call-1",
        conversation_id="room-1",
        room_epoch=1,
        binding_id="binding-1",
        agent_id="agent-1",
        task_id="task-1",
        context_id="context-1",
        interaction_revision=1,
        interaction_fingerprint="fingerprint",
    )
    unproven = SimpleNamespace(
        state="delivery_uncertain",
        pending_interaction_id="interaction-1",
        interaction_revision=1,
        recent_observation_ids=[],
        answer_applied=SimpleNamespace(
            interaction_id="interaction-1", interaction_revision=1
        ),
    )
    runtime = SimpleNamespace(
        run_store=SimpleNamespace(
            load=AsyncMock(return_value=SimpleNamespace(status="awaiting_user"))
        ),
        hitl_port=SimpleNamespace(
            read_interaction=AsyncMock(return_value=(spec, route, "fingerprint"))
        ),
        continuation=SimpleNamespace(
            resume=AsyncMock(return_value="delivery_uncertain")
        ),
        call_ledger=SimpleNamespace(load_by_record_id=AsyncMock(return_value=unproven)),
        observation_inbox=SimpleNamespace(list_due_for_call=AsyncMock(return_value=[])),
        observation_processor=SimpleNamespace(process=AsyncMock()),
    )
    router = DualRuntimeRouter(runtime=runtime)

    with pytest.raises(HITLDeliveryUncertainError):
        await router.route_hitl_answer(
            interaction_id="interaction-1",
            answers=[{"request_id": "q1", "user_input": "a"}],
            responder_id="user-1",
            conversation_id="room-1",
        )


async def test_hitl_wake_delivers_distinct_follow_up_interaction_to_kernel():
    runtime = SimpleNamespace(
        call_ledger=SimpleNamespace(
            load_by_record_id=AsyncMock(
                return_value=SimpleNamespace(
                    state="input_required",
                    pending_interaction_id="interaction-2",
                    recent_observation_ids=["observation-2"],
                )
            )
        ),
        observation_inbox=SimpleNamespace(list_due_for_call=AsyncMock(return_value=[])),
        observation_processor=SimpleNamespace(process=AsyncMock()),
    )

    await DualRuntimeRouter(runtime=runtime)._wake_after_hitl_resume(
        "record-1", answered_interaction_id="interaction-1"
    )

    runtime.observation_processor.process.assert_awaited_once_with("observation-2")


async def test_hitl_wake_inbox_read_failure_is_best_effort():
    runtime = SimpleNamespace(
        call_ledger=SimpleNamespace(
            load_by_record_id=AsyncMock(
                return_value=SimpleNamespace(
                    state="delivery_uncertain",
                    recent_observation_ids=[],
                )
            )
        ),
        observation_inbox=SimpleNamespace(
            list_due_for_call=AsyncMock(side_effect=RuntimeError("inbox unavailable"))
        ),
        observation_processor=SimpleNamespace(process=AsyncMock()),
    )

    await DualRuntimeRouter(runtime=runtime)._wake_after_hitl_resume("record-1")

    runtime.observation_processor.process.assert_not_awaited()


def test_v1_hitl_route_round_trips_unchanged():
    route = HITLRouteSnapshot(route="supervisor_run", orchestration_run_id="legacy-run")
    restored = TypeAdapter(HITLRouteSnapshotUnion).validate_json(
        route.model_dump_json()
    )
    assert restored == route
    assert restored.schema_version == 1


def test_v2_route_is_invocation_owned_and_rejects_provisional_aliases():
    route = HITLRouteSnapshotV2(
        orchestration_run_id="run-1",
        call_record_id="record-1",
        invocation_id="call-1",
        conversation_id="room-1",
        room_epoch=1,
        binding_id="binding-1",
        agent_id="agent-1",
        task_id="task-1",
        context_id="context-1",
        interaction_revision=1,
        interaction_fingerprint="fingerprint",
    )
    restored = TypeAdapter(HITLRouteSnapshotUnion).validate_json(
        route.model_dump_json()
    )
    assert restored == route
    assert route.fingerprint == restored.fingerprint
    with pytest.raises(ValidationError, match="authoritative"):
        route.model_copy(update={"task_id": "relay-pending-1"}).model_dump()
        HITLRouteSnapshotV2.model_validate(
            {**route.model_dump(), "task_id": "relay-pending-1"}
        )


async def test_typed_answers_validate_exact_question_inventory_and_replay():
    owner = InMemoryHITLApplicationPort()
    call = ledger_record().model_copy(
        update={"a2a_task_id": "task-1", "a2a_context_id": "context-1"}
    )
    spec = interaction()
    interaction_id = await owner.create_or_replay(
        call=call,
        interaction=spec,
        interaction_fingerprint="fingerprint",
    )
    assert owner.read_interaction_for_test(interaction_id) is None
    assert (
        await owner.activate(
            interaction_id,
            call_record_id=call.call_record_id,
            interaction_fingerprint="fingerprint",
        )
        == "accepted"
    )
    _, route, _ = owner.read_interaction_for_test(interaction_id)
    assert (
        await owner.publish(interaction_id, call_record_id=call.call_record_id)
        == "accepted"
    )
    assert len(await owner.get_published_interactions(call.conversation_id)) == 1
    answers = [
        HITLQuestionAnswer.model_validate(
            {
                "question_id": "q1",
                "answer": {"kind": "single_choice", "choice": "a"},
            }
        )
    ]
    first = await owner.answer(
        interaction_id=interaction_id,
        interaction_revision=1,
        route_fingerprint=route.fingerprint,
        answers=answers,
        authenticated_answerer_id="user-1",
        verified_auth_reference_digests=[],
        verified_auth_references=[],
    )
    replay = await owner.answer(
        interaction_id=interaction_id,
        interaction_revision=1,
        route_fingerprint=route.fingerprint,
        answers=answers,
        authenticated_answerer_id="user-1",
        verified_auth_reference_digests=[],
        verified_auth_references=[],
    )
    assert first == replay
    assert await owner.get_published_interactions(call.conversation_id) == []
    with pytest.raises(ValueError, match="inventory"):
        await owner.answer(
            interaction_id=interaction_id,
            interaction_revision=1,
            route_fingerprint=route.fingerprint,
            answers=[],
            authenticated_answerer_id="user-1",
            verified_auth_reference_digests=[],
            verified_auth_references=[],
        )


async def test_cas_losing_interaction_observation_is_not_published_under_winner_id():
    delivery = SimpleNamespace(emit_checked=AsyncMock())
    control = AsyncMock()
    coordinator = A2AContinuationCoordinator(
        ledger=SimpleNamespace(),
        bindings=SimpleNamespace(),
        hitl=SimpleNamespace(),
        room_epochs=SimpleNamespace(),
        authorization=SimpleNamespace(),
        auth_references=SimpleNamespace(),
        dispatch=SimpleNamespace(),
        observations=SimpleNamespace(mark_ledger_applied=AsyncMock()),
        hitl_delivery=delivery,
        run_store=SimpleNamespace(load=AsyncMock()),
        canonical_hitl_control=control,
    )
    persisted = ledger_record(run_id="run-1", call_id="call-1").model_copy(
        update={
            "state": "input_required",
            "pending_interaction_id": "interaction-winner",
            "interaction_fingerprint": "winner-fingerprint",
        }
    )
    observation = NormalizedA2AObservation(
        observation_id="observation-loser",
        call_record_id=persisted.call_record_id,
        source_kind="inspection",
        source_identity="inspection:loser",
        binding_scope=persisted.endpoint_scope_digest,
        event_kind="input_required",
        observed_at=datetime.now(UTC),
        interaction_spec=interaction().model_dump(mode="json"),
    )

    await coordinator._after_typed_park(
        persisted,
        observation,
        prior_interaction_id="interaction-winner",
    )

    coordinator.observations.mark_ledger_applied.assert_not_awaited()
    delivery.emit_checked.assert_not_awaited()
    control.assert_not_awaited()
    coordinator.run_store.load.assert_not_awaited()


@pytest.mark.asyncio
async def test_emit_hitl_request_events_close_the_canonical_turn_contract():
    emitted: list[object] = []
    delivery = SimpleNamespace(
        emit=AsyncMock(side_effect=lambda event: emitted.append(event) or True)
    )
    run = SimpleNamespace(
        request=SimpleNamespace(user_message_id="user-1"),
        client_request_id="cr-1",
        tool_batches=[
            SimpleNamespace(
                entries=[
                    SimpleNamespace(
                        call_id="call-1",
                        opaque_public_call_id="inv_travel_0001",
                    )
                ]
            )
        ],
        tool_catalog=SimpleNamespace(
            entries=[
                SimpleNamespace(
                    definition=SimpleNamespace(
                        name="agent_abc",
                        label="Travel Planner Agent - itinerary",
                    ),
                    agent_display_name="Travel Planner Agent",
                )
            ]
        ),
    )
    run_store = SimpleNamespace(load=AsyncMock(return_value=run))
    control = AsyncMock()
    record = ledger_record(run_id="run-1", call_id="call-1")

    await emit_hitl_request_events(
        record=record,
        interaction=interaction(),
        interaction_id="interaction-1",
        hitl_delivery=delivery,
        run_store=run_store,
        canonical_control=control,
    )

    assert len(emitted) == 1
    event = emitted[0]
    assert event.run_id == "run-1"
    assert event.message_id == "orchestrator:run-1:inv_travel_0001"
    assert event.related_user_message_id == "user-1"
    assert event.related_message_id is None
    assert event.agent_label == "Travel Planner Agent"
    assert event.agent_id is None
    assert event.source_step_id is None
    assert event.client_request_id == "cr-1"
    control.assert_awaited_once_with(
        "run_waiting_input",
        "run-1",
        "interaction-1",
        ["q1"],
    )


@pytest.mark.asyncio
async def test_canonical_hitl_request_stops_before_control_when_delivery_fails():
    delivery = SimpleNamespace(
        emit_checked=AsyncMock(return_value=DeliveryEmitStatus.FAILED)
    )
    run = SimpleNamespace(
        request=SimpleNamespace(user_message_id="user-1"),
        client_request_id="cr-1",
        tool_batches=[
            SimpleNamespace(
                entries=[
                    SimpleNamespace(
                        call_id="call-1",
                        opaque_public_call_id="inv_travel_0001",
                    )
                ]
            )
        ],
        tool_catalog=SimpleNamespace(
            entries=[
                SimpleNamespace(
                    definition=SimpleNamespace(
                        name="agent_abc", label="Travel Planner Agent"
                    ),
                    agent_display_name="Travel Planner Agent",
                )
            ]
        ),
    )
    control = AsyncMock()

    with pytest.raises(RuntimeError, match="not durably delivered"):
        await emit_hitl_request_events(
            record=ledger_record(run_id="run-1", call_id="call-1"),
            interaction=interaction(),
            interaction_id="interaction-1",
            hitl_delivery=delivery,
            run_store=SimpleNamespace(load=AsyncMock(return_value=run)),
            canonical_control=control,
        )

    control.assert_not_awaited()


@pytest.mark.asyncio
async def test_emit_hitl_resolved_events_precede_canonical_run_resume():
    emitted: list[object] = []
    delivery = SimpleNamespace(
        emit=AsyncMock(side_effect=lambda event: emitted.append(event) or True)
    )
    run = SimpleNamespace(
        status="running",
        request=SimpleNamespace(user_message_id="user-1"),
        client_request_id="cr-1",
        tool_batches=[
            SimpleNamespace(
                entries=[
                    SimpleNamespace(
                        call_id="call-1",
                        opaque_public_call_id="inv_travel_0001",
                    )
                ]
            )
        ],
    )
    run_store = SimpleNamespace(load=AsyncMock(return_value=run))
    order: list[str] = []

    async def control(*args):
        order.append("control")
        assert args == ("run_resumed", "run-1", "interaction-1", ["q1"])

    async def emit(event):
        emitted.append(event)
        order.append("response")
        return True

    delivery.emit.side_effect = emit
    await emit_hitl_resolved_events(
        record=ledger_record(run_id="run-1", call_id="call-1"),
        interaction=interaction(),
        interaction_id="interaction-1",
        status="responded",
        hitl_delivery=delivery,
        run_store=run_store,
        canonical_control=control,
        answer_ref="answer-digest",
    )

    assert order == ["response", "control"]
    assert emitted[0].run_id == "run-1"
    assert emitted[0].message_id == "orchestrator:run-1:inv_travel_0001"
    assert emitted[0].related_user_message_id == "user-1"
    assert emitted[0].answer_ref == "answer-digest"


@pytest.mark.asyncio
# The owner's cancel by default, or the session close's own cause.
@pytest.mark.parametrize("cause", [None, "room_closed"])
async def test_router_full_cancellation_claims_run_before_descendant_cleanup(cause):
    record = ledger_record(run_id="run-1", call_id="call-1").model_copy(
        update={"state": "input_required", "pending_interaction_id": "interaction-1"}
    )
    route = SimpleNamespace(
        orchestration_run_id="run-1",
        call_record_id=record.call_record_id,
        interaction_revision=1,
    )
    run = SimpleNamespace(
        run_id="run-1",
        conversation_id="room-1",
        status="running",
        state_version=1,
        cancellation_command_id=None,
        request=SimpleNamespace(user_message_id="user-1"),
        client_request_id="cr-1",
        tool_batches=[
            SimpleNamespace(
                entries=[
                    SimpleNamespace(
                        call_id="call-1", opaque_public_call_id="inv_travel_0001"
                    )
                ]
            )
        ],
    )
    canceling_run = SimpleNamespace(
        **{
            **run.__dict__,
            "status": "canceling",
            "state_version": 2,
            "cancellation_command_id": f"cancel:run-1:{cause or 'user_requested'}",
        }
    )
    order: list[str] = []

    claimed: list[tuple[str, str]] = []

    async def request_cancellation(*_args, **kwargs):
        claimed.append((kwargs["cause"], kwargs["command_id"]))
        order.append("cas")
        return SimpleNamespace(outcome="accepted", run=canceling_run)

    async def emit_checked(_event):
        order.append("hitl_response")
        return DeliveryEmitStatus.DELIVERED

    async def cancel_run(*_args, **_kwargs):
        order.append("cancel_calls")
        return {"call-1": "canceled"}

    async def signal_run_cancellation(*_args):
        order.append("signal")

    async def reconcile_cancellation(_run):
        order.append("reconcile")
        return SimpleNamespace(run=SimpleNamespace(status="canceled"))

    router = DualRuntimeRouter.__new__(DualRuntimeRouter)
    router._runtime = SimpleNamespace(
        hitl_port=SimpleNamespace(
            get_eligible_interactions=AsyncMock(
                return_value=[(interaction(), route, "fingerprint")]
            ),
            abandon=AsyncMock(return_value="accepted"),
        ),
        hitl_delivery=SimpleNamespace(emit_checked=emit_checked),
        run_store=SimpleNamespace(
            load_by_user_message_id=AsyncMock(return_value=run),
            load=AsyncMock(return_value=canceling_run),
            request_cancellation=request_cancellation,
        ),
        call_ledger=SimpleNamespace(load_by_record_id=AsyncMock(return_value=record)),
        continuation=SimpleNamespace(canonical_hitl_control=AsyncMock()),
        cancellation_coordinator=SimpleNamespace(cancel_run=cancel_run),
        session_host=SimpleNamespace(
            signal_run_cancellation=signal_run_cancellation,
            reconcile_cancellation=reconcile_cancellation,
        ),
    )

    await router.route_cancellation_by_user_message(
        "user-1", reason="user:user-1", **({"cause": cause} if cause else {})
    )

    assert order == [
        "cas",
        "cancel_calls",
        "hitl_response",
        "reconcile",
        "signal",
    ]
    expected = cause or "user_requested"
    assert claimed == [(expected, f"cancel:run-1:{expected}")]


@pytest.mark.asyncio
# The owner's cancel by default, or the session close's own cause.
@pytest.mark.parametrize("cause", [None, "room_closed"])
async def test_router_direct_hitl_cancellation_aborts_the_owning_run(cause):
    record = ledger_record(run_id="run-1", call_id="call-1").model_copy(
        update={"state": "input_required", "pending_interaction_id": "interaction-1"}
    )
    route = SimpleNamespace(
        conversation_id="room-1",
        orchestration_run_id="run-1",
        call_record_id=record.call_record_id,
        interaction_revision=1,
    )
    run = SimpleNamespace(
        run_id="run-1",
        conversation_id="room-1",
        status="running",
        state_version=1,
        cancellation_command_id=None,
        request=SimpleNamespace(user_message_id="user-1"),
        client_request_id="cr-1",
        tool_batches=[
            SimpleNamespace(
                entries=[
                    SimpleNamespace(
                        call_id="call-1", opaque_public_call_id="inv_travel_0001"
                    )
                ]
            )
        ],
    )
    canceling_run = SimpleNamespace(
        **{
            **run.__dict__,
            "status": "canceling",
            "state_version": 2,
            "cancellation_command_id": f"cancel:run-1:{cause or 'user_requested'}",
        }
    )
    order: list[str] = []

    claimed: list[tuple[str, str]] = []

    async def request_cancellation(*_args, **kwargs):
        claimed.append((kwargs["cause"], kwargs["command_id"]))
        order.append("cas")
        return SimpleNamespace(outcome="accepted", run=canceling_run)

    async def emit_checked(_event):
        order.append("hitl_response")
        return DeliveryEmitStatus.DELIVERED

    async def cancel_run(*_args, **_kwargs):
        order.append("cancel_calls")
        return {"call-1": "canceled"}

    async def signal_run_cancellation(*_args):
        order.append("signal")

    async def reconcile_cancellation(_run):
        order.append("reconcile")
        return SimpleNamespace(run=SimpleNamespace(status="canceled"))

    router = DualRuntimeRouter.__new__(DualRuntimeRouter)
    router._runtime = SimpleNamespace(
        hitl_port=SimpleNamespace(
            read_interaction=AsyncMock(
                return_value=(interaction(), route, "fingerprint")
            ),
            abandon=AsyncMock(return_value="accepted"),
        ),
        hitl_delivery=SimpleNamespace(emit_checked=emit_checked),
        run_store=SimpleNamespace(
            load=AsyncMock(return_value=run),
            request_cancellation=request_cancellation,
        ),
        call_ledger=SimpleNamespace(load_by_record_id=AsyncMock(return_value=record)),
        continuation=SimpleNamespace(canonical_hitl_control=AsyncMock()),
        cancellation_coordinator=SimpleNamespace(cancel_run=cancel_run),
        session_host=SimpleNamespace(
            signal_run_cancellation=signal_run_cancellation,
            reconcile_cancellation=reconcile_cancellation,
        ),
    )

    version = await router.cancel_hitl_interaction(
        conversation_id="room-1",
        interaction_id="interaction-1",
        expected_version=1,
        **({"cause": cause} if cause else {}),
    )

    assert version == 1
    assert order == [
        "cas",
        "cancel_calls",
        "hitl_response",
        "reconcile",
        "signal",
    ]
    expected = cause or "user_requested"
    assert claimed == [(expected, f"cancel:run-1:{expected}")]


@pytest.mark.asyncio
async def test_router_pending_hitl_uses_public_activity_message_id():
    record = ledger_record(run_id="run-1", call_id="call-1").model_copy(
        update={
            "state": "input_required",
            "pending_interaction_id": "interaction-1",
        }
    )
    router = DualRuntimeRouter.__new__(DualRuntimeRouter)
    router._runtime = SimpleNamespace(
        hitl_port=SimpleNamespace(
            get_published_interactions=AsyncMock(
                return_value=[
                    (
                        interaction(),
                        SimpleNamespace(
                            orchestration_run_id="run-1",
                            call_record_id=record.call_record_id,
                            invocation_id="call-1",
                            agent_id="agent-1",
                            task_id="task-1",
                            context_id="context-1",
                            interaction_revision=1,
                        ),
                        "fingerprint",
                    )
                ]
            )
        ),
        run_store=SimpleNamespace(
            load=AsyncMock(
                return_value=SimpleNamespace(
                    request=SimpleNamespace(user_message_id="user-1"),
                    client_request_id="cr-1",
                    tool_batches=[
                        SimpleNamespace(
                            entries=[
                                SimpleNamespace(
                                    call_id="call-1",
                                    opaque_public_call_id="inv_travel_0001",
                                )
                            ]
                        )
                    ],
                    tool_catalog=SimpleNamespace(
                        entries=[
                            SimpleNamespace(
                                definition=SimpleNamespace(
                                    name="agent_abc", label="Travel Planner Agent"
                                ),
                                agent_display_name="Travel Planner Agent",
                            )
                        ]
                    ),
                )
            )
        ),
        call_ledger=SimpleNamespace(load_by_record_id=AsyncMock(return_value=record)),
        public_secret_values=(),
    )

    pending = await router.get_pending_hitl("room-1")

    assert len(pending) == 1
    assert pending[0].message_id == "orchestrator:run-1:inv_travel_0001"
    assert pending[0].display_message_id == "orchestrator:run-1:inv_travel_0001"
    assert pending[0].agent_name == "Travel Planner Agent"
    # The stored kind travels, so a session decides from it and not from text.
    assert pending[0].interaction_kind == "questionnaire"
    assert pending[0].agent_id is None
    assert pending[0].source_step_id is None
    assert pending[0].a2a_task_id is None
    assert pending[0].a2a_context_id is None


@pytest.mark.asyncio
async def test_hitl_cancel_produces_complete_foldable_canonical_snapshot():
    from common.dto.delivery import RunEventNotification
    from delivery.snapshot import RoomEventFold
    from delivery.translator import to_sse_frame
    from execution.adapters.session_host import RoomSessionHost
    from execution.orchestrator.a2a_runtime.cancellation import (
        A2ACancellationCoordinator,
    )
    from execution.orchestrator.a2a_runtime.catalog import FrozenToolCatalog
    from execution.orchestrator.a2a_runtime.models import A2ADispatchReceipt
    from execution.orchestrator.lifecycle import SessionEvent
    from execution.orchestrator.models import (
        FrozenToolCatalogEntry,
        FrozenToolCatalogSnapshot,
    )
    from execution.orchestrator.projection import (
        ProjectionOutboxWorker,
        SettlingProjectionDriver,
    )
    from execution.orchestrator.public_projection import (
        PublicProjectionTranslator,
        canonical_settlement_payload,
    )
    from tests._orchestrator_a2a_helpers import invocation
    from tests._orchestrator_helpers import (
        NOW,
        FixedClock,
        NeverCancelled,
        make_kernel,
        make_run,
        tool_events,
    )
    from tests.test_orchestrator_a2a_runtime import setup

    # Real acceptance, parking, publication and cancellation; only transport,
    # provider and persistence are in-memory substitutes.
    a2a, ledger, _, dispatch, _ = await setup()
    resolved = invocation().tool
    catalog = FrozenToolCatalogSnapshot(
        catalog_id="catalog-1",
        entries=[
            FrozenToolCatalogEntry(
                definition=resolved.definition,
                binding=resolved.binding,
                agent_display_name="Agent",
            )
        ],
        created_at=NOW,
    )
    run = make_run().model_copy(update={"tool_catalog": catalog})
    dispatch.receipt = A2ADispatchReceipt(
        outcome="interaction",
        task_id="task-1",
        context_id="context-1",
        interaction_observation=NormalizedA2AObservation(
            observation_id="typed-input",
            source_kind="direct",
            source_identity="direct:typed-input",
            binding_scope="endpoint",
            event_kind="input_required",
            observed_at=NOW,
            task_id="task-1",
            context_id="context-1",
            interaction_spec=interaction().model_dump(mode="json"),
        ),
    )
    kernel, store, model, _ = await make_kernel(
        [
            tool_events(("call-1", "agent_abc", '{"task":"do work"}')),
            tool_events(("call-2", "surface_agent_questions", "{}")),
        ],
        run=run,
        tool_runtime=a2a,
        supervisor_hitl=AsyncMock(),
    )
    kernel.tool_catalog = FrozenToolCatalog(catalog)
    kernel.projection_driver = SettlingProjectionDriver(store)
    a2a.run_store = store
    records = []
    translator = PublicProjectionTranslator()

    async def emit_checked(event):
        frame = to_sse_frame(event, timestamp=NOW, room_seq=len(records) + 1)
        records.append(
            {
                "room_id": frame["conversation_id"],
                "room_seq": len(records) + 1,
                "kind": frame["type"],
                "ts": frame["timestamp"],
                "payload_public": frame["data"],
            }
        )
        return DeliveryEmitStatus.DELIVERED

    async def listener(event):
        projected = translator.translate(event, catalog=catalog)
        if projected is not None:
            await emit_checked(
                RunEventNotification(
                    conversation_id=run.conversation_id,
                    event_id=projected.event_id,
                    run_id=run.run_id,
                    seq=projected.seq,
                    run_event_type=projected.kind,
                    payload=projected.payload,
                    correlation_id=run.client_request_id,
                )
            )

    async def lifecycle(event_type, current, payload):
        await listener(
            SessionEvent(
                event_type=event_type,
                session_id=current.session_id,
                run_id=current.run_id,
                causation_id=current.request.user_message_id,
                sequence=current.state_version,
                timestamp=NOW,
                payload=payload,
                conversation_id=current.conversation_id,
                user_message_id=current.request.user_message_id,
                client_request_id=current.client_request_id,
            )
        )

    async def control(kind, run_id, interaction_id, request_ids):
        await emit_checked(
            RunEventNotification(
                conversation_id=run.conversation_id,
                event_id=f"public:{run_id}:{kind}:{interaction_id}",
                run_id=run_id,
                seq=len(records) + 1,
                run_event_type=kind,
                payload={
                    "interaction_id": interaction_id,
                    "request_ids": request_ids,
                    "requested_at": NOW,
                },
                correlation_id=run.client_request_id,
            )
        )

    a2a.hitl_delivery = SimpleNamespace(emit_checked=emit_checked)
    a2a.canonical_hitl_control = control
    kernel.canonical_event_reader = AsyncMock(side_effect=lambda *_: list(records))
    await lifecycle("run_started", run, {"mode": "ultimate"})
    waiting = await kernel.run(run.run_id, signal=NeverCancelled(), lifecycle=lifecycle)
    assert waiting.outcome == "awaiting_user"
    child = await ledger.load(run.run_id, "call-1")
    assert child.state == "input_required"
    assert len(await a2a.hitl.get_published_interactions(run.conversation_id)) == 1
    waiting_fold = RoomEventFold()
    for record in records:
        assert waiting_fold.apply(record), record
    assert waiting_fold.state(room_seq=len(records))["turns"][0]["state"] == (
        "awaiting_input"
    )

    host = RoomSessionHost(
        kernel_factory=lambda _: kernel,
        run_store=store,
        epoch_store=a2a.room_epochs,
        listener=listener,
        clock=FixedClock(),
    )
    router = DualRuntimeRouter(
        runtime=SimpleNamespace(
            hitl_port=a2a.hitl,
            hitl_delivery=a2a.hitl_delivery,
            run_store=store,
            call_ledger=ledger,
            continuation=SimpleNamespace(canonical_hitl_control=control),
            cancellation_coordinator=A2ACancellationCoordinator(
                ledger=ledger,
                room_epochs=a2a.room_epochs,
                dispatch=dispatch,
                observations=a2a.observations,
                hitl=a2a.hitl,
            ),
            session_host=host,
        )
    )
    assert (
        await router.cancel_hitl_interaction(
            conversation_id=run.conversation_id,
            interaction_id="interaction-1",
            expected_version=1,
        )
        == 1
    )
    terminal = await store.load(run.run_id)
    assert terminal.status == "canceled"
    assert (await ledger.load(run.run_id, "call-1")).state == "canceled"
    assert a2a.hitl.is_abandoned_for_test("interaction-1")
    assert await a2a.hitl.get_eligible_interactions(run.conversation_id) == []
    assert all(
        entry.state == "terminal" and entry.public_terminal_emitted
        for batch in terminal.tool_batches
        for entry in batch.entries
    )

    async def project_terminal_status(_intent, current):
        await emit_checked(
            RunEventNotification(
                conversation_id=current.conversation_id,
                event_id=f"public:{current.run_id}:run_settled",
                run_id=current.run_id,
                seq=current.state_version,
                run_event_type="run_settled",
                payload=canonical_settlement_payload(current),
                correlation_id=current.client_request_id,
            )
        )
        return "accepted"

    worker = ProjectionOutboxWorker(
        run_store=store,
        projectors={
            "append_orchestrator_event": AsyncMock(return_value="accepted"),
            "project_terminal_run_status": project_terminal_status,
        },
    )
    assert await worker.run_once(due_at=NOW) == 2
    assert (await store.load(run.run_id)).projection_state == "settled"
    assert await worker.run_once(due_at=NOW) == 0

    # Fold the actual newly emitted history from scratch, as refresh does.
    fold = RoomEventFold()
    for record in records:
        assert fold.apply(record), record
    snapshot = fold.state(room_seq=len(records))
    turn = snapshot["turns"][0]
    assert turn["state"] == "canceled"
    assert turn["active_interaction_id"] is None
    assert turn["hitl_interactions"][0]["state"] == "canceled"
    assert turn["hitl_interactions"][0]["requests"][0]["status"] == "canceled"
    assert turn["internal_turns"][0]["status"] == "aborted"
    assert [item["status"] for item in turn["activity"] if item["kind"] == "tool"] == [
        "canceled",
        "canceled",
    ]
    assert turn["terminal_code"] == "user_requested"
    closure = [
        record["payload_public"].get("type", record["kind"])
        for record in records
        if record["kind"] == "hitl_response"
        or record["payload_public"].get("type")
        in {"tool_execution_end", "turn_end", "run_settled"}
    ]
    assert closure == [
        "hitl_response",
        "tool_execution_end",
        "tool_execution_end",
        "turn_end",
        "run_settled",
    ]
    assert len(dispatch.commands) == 1
    assert len(model.requests) == 2

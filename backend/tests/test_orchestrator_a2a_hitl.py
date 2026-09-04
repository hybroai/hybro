from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import TypeAdapter, ValidationError

from common.dto.delivery import DeliveryEmitStatus
from common.dto.hitl import (
    A2AInteractionSpec,
    HITLQuestionAnswer,
    HITLRouteSnapshot,
    HITLRouteSnapshotUnion,
    HITLRouteSnapshotV2,
)
from execution.hitl.exceptions import HITLConflictError
from execution.orchestrator.a2a_runtime.hitl import (
    A2AContinuationCoordinator,
    InMemoryHITLApplicationPort,
)
from execution.orchestrator.a2a_runtime.interaction_outcome import (
    emit_hitl_request_events,
    emit_hitl_resolved_events,
)
from execution.orchestrator.a2a_runtime.models import NormalizedA2AObservation
from execution.orchestrator_routing import DualRuntimeRouter, _map_legacy_answers

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
        _map_legacy_answers(
            spec,
            [{"request_id": "cloud_providers", "user_input": "AWS, Azure"}],
        )

    mapped = _map_legacy_answers(
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
        room_id="room-1",
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
        room_id="room-1",
    )

    assert result == "completed"
    assert runtime.call_ledger.load_by_record_id.await_count == 2
    runtime.observation_processor.process.assert_awaited_once_with(
        "terminal-with-artifacts"
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
        room_id="room-1",
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
    assert len(await owner.get_published_interactions(call.room_id)) == 1
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
    assert await owner.get_published_interactions(call.room_id) == []
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
        lifecycle_family="canonical",
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
        lifecycle_family="canonical",
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
        lifecycle_family="canonical",
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
async def test_router_full_cancellation_claims_run_before_descendant_cleanup():
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
        room_id="room-1",
        lifecycle_family="canonical",
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
            "cancellation_command_id": "cancel:run-1:user_requested",
        }
    )
    order: list[str] = []

    async def request_cancellation(*_args, **_kwargs):
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

    await router.route_cancellation_by_user_message("user-1", reason="user:user-1")

    assert order == [
        "cas",
        "cancel_calls",
        "hitl_response",
        "reconcile",
        "signal",
    ]


@pytest.mark.asyncio
async def test_router_direct_hitl_cancellation_aborts_the_owning_run():
    record = ledger_record(run_id="run-1", call_id="call-1").model_copy(
        update={"state": "input_required", "pending_interaction_id": "interaction-1"}
    )
    route = SimpleNamespace(
        room_id="room-1",
        orchestration_run_id="run-1",
        call_record_id=record.call_record_id,
        interaction_revision=1,
    )
    run = SimpleNamespace(
        run_id="run-1",
        room_id="room-1",
        lifecycle_family="canonical",
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
            "cancellation_command_id": "cancel:run-1:user_requested",
        }
    )
    order: list[str] = []

    async def request_cancellation(*_args, **_kwargs):
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
        room_id="room-1",
        interaction_id="interaction-1",
        expected_version=1,
    )

    assert version == 1
    assert order == [
        "cas",
        "cancel_calls",
        "hitl_response",
        "reconcile",
        "signal",
    ]


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
                    lifecycle_family="canonical",
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
    assert pending[0].agent_id is None
    assert pending[0].source_step_id is None
    assert pending[0].a2a_task_id is None
    assert pending[0].a2a_context_id is None

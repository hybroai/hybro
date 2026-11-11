from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

from approval.application import HITLApplicationCoordinator
from approval.exceptions import HITLRoutingFailedError
from approval.repository import HITLRequestStore
from approval.service import HITLService
from common.dto.hitl import HITLApplicationRoute, HITLRouteSnapshot
from common.utils.time import utcnow
from models.hitl import (
    HITLInteraction,
    HITLInteractionStatus,
    HITLStatus,
)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _request_doc(**updates):
    doc = {
        "schema_version": 3,
        "request_id": "request-1",
        "interaction_id": "interaction-1",
        "question_index": 0,
        "question_count": 1,
        "room_id": "room-1",
        "user_message_id": "user-message-1",
        "application_route": "supervisor_run",
        "public_source": "supervisor",
        "evidence_origin": "supervisor",
        "prompt": "Choose a market",
        "status": "pending",
        "expires_at": utcnow() + timedelta(hours=1),
        "orchestration_run_id": "run-1",
    }
    source = updates.pop("source", "supervisor")
    doc.update(updates)
    default_index = 1 if doc["request_id"] == "request-2" else doc["question_index"]
    doc["question_index"] = int(doc.pop("group_index", default_index))
    default_count = 2 if doc["request_id"] == "request-2" else doc["question_count"]
    doc["question_count"] = int(doc.pop("group_total", default_count))
    doc.pop("group_id", None)
    doc["public_source"] = source
    doc["evidence_origin"] = source
    doc["application_route"] = "supervisor_run"
    if source == "agent":
        doc.setdefault("agent_id", "agent-1")
        doc.setdefault("a2a_task_id", "remote-task")
        doc.setdefault("a2a_context_id", "remote-context")
        doc.setdefault("continuation_message_id", "agent-message")
        doc.setdefault("display_message_id", "agent-message")
    return doc


def _interaction(**updates):
    source = updates.pop("source", "supervisor")
    creation_prompts = updates.pop("creation_prompts", None)
    snapshot = HITLRouteSnapshot(
        route=HITLApplicationRoute.SUPERVISOR_RUN,
        orchestration_run_id="run-1",
    )
    doc = {
        "schema_version": 3,
        "interaction_id": "interaction-1",
        "room_id": "room-1",
        "user_message_id": "user-message-1",
        "orchestration_run_id": "run-1",
        "application_route": snapshot.route.value,
        "public_source": source,
        "evidence_origin": source,
        "route_snapshot": snapshot.model_dump(mode="python"),
        "route_fingerprint": snapshot.fingerprint,
        "request_ids": ["request-1"],
        "required_request_ids": ["request-1"],
        "expected_request_count": 1,
        "status": "open",
        "version": 2,
        "application_revision": 0,
    }
    doc.update(updates)
    if doc.get("status") == "applying" and not doc.get("application_claim_id"):
        doc["application_claim_id"] = "claim"
    if "creation_inventory" not in doc:
        prompts = creation_prompts or [
            "Choose a market" for _request_id in doc["request_ids"]
        ]
        doc["creation_inventory"] = []
        for index, request_id in enumerate(doc["request_ids"]):
            member = {"request_id": request_id, "prompt": prompts[index]}
            if source == "agent":
                member.update(
                    agent_id="agent-1",
                    continuation_message_id="agent-message",
                    display_message_id="agent-message",
                )
            doc["creation_inventory"].append(member)
    return doc


def test_interaction_model_rejects_impossible_applied_state():
    with pytest.raises(ValidationError):
        snapshot = HITLRouteSnapshot(
            route=HITLApplicationRoute.SUPERVISOR_RUN,
            orchestration_run_id="run-1",
        )
        HITLInteraction(
            interaction_id="interaction-1",
            conversation_id="room-1",
            user_message_id="user-message-1",
            application_route=HITLApplicationRoute.SUPERVISOR_RUN,
            public_source="supervisor",
            evidence_origin="supervisor",
            route_snapshot=snapshot,
            route_fingerprint=snapshot.fingerprint,
            creation_inventory=[{"request_id": "request-1", "prompt": "Question?"}],
            request_ids=["request-1"],
            required_request_ids=["request-1"],
            expected_request_count=1,
            answer_request_ids=["request-1"],
            status=HITLInteractionStatus.APPLIED,
        )


@pytest.mark.asyncio
async def test_deadline_is_part_of_claim_and_pending_queries_without_truncation():
    collection = MagicMock()
    collection.find_one_and_update = AsyncMock(return_value=None)
    collection.find = AsyncMock(return_value=[])
    part = HITLRequestStore(hitl_requests=collection, message_writer=MagicMock())

    await part.claim_hitl_request("request-1", status="answer_recorded")
    claim_query = collection.find_one_and_update.await_args.args[0]
    assert claim_query["$and"][0] == {
        "request_id": "request-1",
        "status": "pending",
    }
    assert {
        "expires_at": {"$gt": claim_query["$and"][1]["$or"][0]["expires_at"]["$gt"]}
    } in claim_query["$and"][1]["$or"]

    await part.get_pending_hitl_requests("room-1")
    pending_kwargs = collection.find.await_args.kwargs
    assert "limit" not in pending_kwargs


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "interaction_status",
    ["answers_recorded", "applying", "delivery_uncertain"],
)
async def test_pending_projection_restores_nonterminal_application_states(
    interaction_status: str,
):
    row = _request_doc(
        status="answer_recorded",
        user_input="Market A",
        answer_digest=_sha("Market A"),
    )
    persistence = MagicMock()
    persistence.get_pending_hitl_requests_strict = AsyncMock(return_value=[row])
    persistence.get_hitl_request = AsyncMock(return_value=row)
    lifecycle = MagicMock()
    lifecycle.get_interaction_strict = AsyncMock(
        return_value=_interaction(
            status=interaction_status,
            answer_request_ids=["request-1"],
            answer_refs=[{"request_id": "request-1", "digest": _sha("Market A")}],
            application_claim_id=(
                "claim-1" if interaction_status == "applying" else None
            ),
            application_error=(
                "Answer delivery is uncertain"
                if interaction_status == "delivery_uncertain"
                else None
            ),
        )
    )
    service = HITLService(lifecycle=lifecycle)
    service._persistence = persistence

    pending = await service.get_pending_requests("room-1")

    assert len(pending) == 1
    assert pending[0].interaction_status.value == interaction_status
    assert pending[0].application_status == interaction_status
    if interaction_status == "delivery_uncertain":
        assert pending[0].application_error == "Answer delivery is uncertain"


@pytest.mark.asyncio
async def test_pending_projection_validates_complete_inventory_then_hides_responded():
    responded = _request_doc(
        request_id="request-1",
        interaction_id="group-1",
        group_total=2,
        group_index=0,
        status="responded",
        user_input="A",
    )
    pending = _request_doc(
        request_id="request-2",
        interaction_id="group-1",
        group_total=2,
        group_index=1,
    )
    interaction = _interaction(
        interaction_id="group-1",
        request_ids=["request-1", "request-2"],
        required_request_ids=["request-1", "request-2"],
        expected_request_count=2,
        status=HITLInteractionStatus.PARTIALLY_ANSWERED.value,
    )
    persistence = MagicMock()
    # The storage pending scan intentionally omits the already-responded member.
    persistence.get_pending_hitl_requests_strict = AsyncMock(return_value=[pending])
    persistence.get_hitl_request = AsyncMock(
        side_effect=lambda request_id: (
            responded if request_id == "request-1" else pending
        )
    )
    lifecycle = MagicMock()
    lifecycle.get_interaction_strict = AsyncMock(return_value=interaction)
    service = HITLService(lifecycle=lifecycle)
    service._persistence = persistence

    result = await service.get_pending_requests("room-1")

    assert [request.request_id for request in result] == ["request-2"]
    assert persistence.get_hitl_request.await_count == 2


@pytest.mark.asyncio
async def test_last_group_answer_applies_one_combined_payload():
    rows = [
        _request_doc(
            request_id="request-1",
            interaction_id="group-1",
            group_id="group-1",
            group_total=2,
            group_index=0,
            prompt="First?",
            user_input="A",
            answer_digest=_sha("A"),
            status="answer_recorded",
        ),
        _request_doc(
            request_id="request-2",
            interaction_id="group-1",
            group_id="group-1",
            group_total=2,
            group_index=1,
            prompt="Second?",
            user_input="B",
            answer_digest=_sha("B"),
            status="answer_recorded",
        ),
    ]
    applying = _interaction(
        interaction_id="group-1",
        request_ids=["request-1", "request-2"],
        required_request_ids=["request-1", "request-2"],
        answer_request_ids=["request-1", "request-2"],
        expected_request_count=2,
        creation_prompts=["First?", "Second?"],
        status="applying",
        application_claim_id="claim",
        application_revision=1,
        answer_refs=[
            {"request_id": "request-1", "digest": _sha("A")},
            {"request_id": "request-2", "digest": _sha("B")},
        ],
    )
    applied = {**applying, "status": "applied", "version": 4}
    persistence = MagicMock()
    persistence.get_hitl_request = AsyncMock(
        side_effect=lambda rid: next(row for row in rows if row["request_id"] == rid)
    )
    lifecycle = MagicMock()
    lifecycle.record_interaction_answer = AsyncMock(
        return_value={
            **applying,
            "status": "answers_recorded",
            "application_claim_id": None,
            "application_revision": 0,
        }
    )
    lifecycle.claim_interaction_application = AsyncMock(return_value=applying)
    lifecycle.mark_interaction_application_state = AsyncMock(return_value=applied)
    lifecycle.get_resume_command_for_interaction_strict = AsyncMock(return_value=None)
    coordinator = HITLApplicationCoordinator(lifecycle=lifecycle)
    order: list[str] = []

    async def finalize(*_args, reconcile_terminal=True, **_kwargs):
        order.append("terminal_reconcile" if reconcile_terminal else "hitl_response")
        return True

    async def emit_resumed(_interaction):
        order.append("run_resumed")

    async def apply_supervisor(*_args, **_kwargs):
        order.append("route_effect")
        return {}

    coordinator.finalize_applied = AsyncMock(side_effect=finalize)
    coordinator._apply_supervisor = AsyncMock(side_effect=apply_supervisor)
    service = SimpleNamespace(
        persistence=persistence,
        emit_canonical_resumed_control=emit_resumed,
    )

    result = await coordinator.apply_interaction(
        service,
        {**applying, "status": "answers_recorded", "application_revision": 0},
    )

    assert result["status"] == "applied"
    coordinator._apply_supervisor.assert_awaited_once()
    assert coordinator._apply_supervisor.await_args.kwargs["user_input"] == (
        "Q: First?\nA: A\n\nQ: Second?\nA: B"
    )
    lifecycle.claim_interaction_application.assert_awaited_once()
    assert coordinator.finalize_applied.await_count == 2
    assert coordinator.finalize_applied.await_args_list[0].kwargs == {
        "reconcile_terminal": False
    }
    assert coordinator.finalize_applied.await_args_list[1].kwargs == {}
    assert order == [
        "hitl_response",
        "run_resumed",
        "route_effect",
        "terminal_reconcile",
    ]


@pytest.mark.asyncio
async def test_incomplete_response_projection_blocks_resume_and_route_effect():
    row = _request_doc(
        status="answer_recorded",
        user_input="Shanghai",
        answer_digest=_sha("Shanghai"),
    )
    claimed = _interaction(
        status="applying",
        application_revision=1,
        answer_request_ids=["request-1"],
        answer_refs=[{"request_id": "request-1", "digest": _sha("Shanghai")}],
    )
    lifecycle = MagicMock()
    lifecycle.record_interaction_answer = AsyncMock(
        return_value={
            **claimed,
            "status": "answers_recorded",
            "application_claim_id": None,
            "application_revision": 0,
        }
    )
    lifecycle.claim_interaction_application = AsyncMock(return_value=claimed)
    lifecycle.mark_interaction_application_state = AsyncMock(return_value=claimed)
    persistence = MagicMock()
    persistence.get_hitl_request = AsyncMock(return_value=row)
    coordinator = HITLApplicationCoordinator(lifecycle=lifecycle)
    coordinator.finalize_applied = AsyncMock(return_value=False)
    coordinator._apply_supervisor = AsyncMock(return_value={})
    service = SimpleNamespace(
        persistence=persistence,
        emit_canonical_resumed_control=AsyncMock(),
    )

    with pytest.raises(HITLRoutingFailedError, match="projection remains incomplete"):
        await coordinator.apply_interaction(
            service,
            {**claimed, "status": "answers_recorded", "application_revision": 0},
        )

    service.emit_canonical_resumed_control.assert_not_awaited()
    coordinator._apply_supervisor.assert_not_awaited()


@pytest.mark.asyncio
async def test_batch_records_every_answer_before_single_application():
    rows = {
        "request-1": _request_doc(
            request_id="request-1",
            interaction_id="group-1",
            group_id="group-1",
            group_total=2,
            group_index=0,
        ),
        "request-2": _request_doc(
            request_id="request-2",
            interaction_id="group-1",
            group_id="group-1",
            group_total=2,
            group_index=1,
        ),
    }
    state = _interaction(
        interaction_id="group-1",
        request_ids=["request-1", "request-2"],
        required_request_ids=["request-1", "request-2"],
        expected_request_count=2,
    )
    lifecycle = MagicMock()
    lifecycle.get_interaction_strict = AsyncMock(side_effect=lambda _id: state)

    async def record(_interaction_id, *, request_id, answer_digest):
        state["answer_request_ids"] = [
            *state.get("answer_request_ids", []),
            request_id,
        ]
        state["answer_refs"] = [
            *state.get("answer_refs", []),
            {"request_id": request_id, "digest": answer_digest},
        ]
        state["status"] = (
            "answers_recorded"
            if len(state["answer_request_ids"]) == 2
            else "partially_answered"
        )
        return dict(state)

    lifecycle.record_interaction_answer = AsyncMock(side_effect=record)
    persistence = MagicMock()
    persistence.get_hitl_request = AsyncMock(
        side_effect=lambda request_id: rows[request_id]
    )

    async def claim(request_id, **updates):
        previous = dict(rows[request_id])
        rows[request_id].update(updates)
        return previous

    persistence.claim_hitl_request = AsyncMock(side_effect=claim)
    coordinator = HITLApplicationCoordinator(lifecycle=lifecycle)

    async def apply(_service, interaction):
        assert interaction["status"] == "answers_recorded"
        assert set(interaction["answer_request_ids"]) == {"request-1", "request-2"}
        return {"status": "applied", "request_id": "request-1"}

    coordinator.apply_interaction = AsyncMock(side_effect=apply)
    result = await coordinator.handle_batch_response(
        SimpleNamespace(persistence=persistence),
        conversation_id="room-1",
        interaction_id="group-1",
        answers=[
            {"request_id": "request-1", "user_input": "A"},
            {"request_id": "request-2", "user_input": "B"},
        ],
        user_id="user-1",
    )

    assert result["status"] == "applied"
    assert persistence.claim_hitl_request.await_count == 2
    assert lifecycle.record_interaction_answer.await_count == 2
    coordinator.apply_interaction.assert_awaited_once()


@pytest.mark.asyncio
async def test_batch_records_answer_without_application_until_aggregate_ready():
    request = _request_doc()
    persistence = MagicMock()
    persistence.get_hitl_request = AsyncMock(return_value=request)
    persistence.claim_hitl_request = AsyncMock(return_value=request)
    partially_answered = _interaction(
        answer_request_ids=["request-1"],
        status=HITLInteractionStatus.PARTIALLY_ANSWERED.value,
    )
    lifecycle = MagicMock()
    lifecycle.get_interaction_strict = AsyncMock(
        side_effect=[_interaction(), partially_answered]
    )
    lifecycle.record_interaction_answer = AsyncMock(return_value=partially_answered)
    coordinator = HITLApplicationCoordinator(lifecycle=lifecycle)
    coordinator.apply_interaction = AsyncMock()

    with pytest.raises(HITLRoutingFailedError, match="application is not ready"):
        await coordinator.handle_batch_response(
            SimpleNamespace(persistence=persistence),
            conversation_id="room-1",
            interaction_id="interaction-1",
            answers=[{"request_id": "request-1", "user_input": "Market A"}],
            user_id="user-1",
        )

    assert persistence.claim_hitl_request.await_args.kwargs["status"] == (
        HITLStatus.ANSWER_RECORDED.value
    )
    coordinator.apply_interaction.assert_not_awaited()


@pytest.mark.asyncio
async def test_batch_failed_answer_recording_never_marks_request_responded():
    request = _request_doc()
    persistence = MagicMock()
    persistence.get_hitl_request = AsyncMock(return_value=request)
    persistence.claim_hitl_request = AsyncMock(return_value=request)
    lifecycle = MagicMock()
    lifecycle.get_interaction_strict = AsyncMock(return_value=_interaction())
    lifecycle.record_interaction_answer = AsyncMock(return_value=None)
    coordinator = HITLApplicationCoordinator(lifecycle=lifecycle)
    coordinator.apply_interaction = AsyncMock()

    with pytest.raises(HITLRoutingFailedError, match="Failed to record"):
        await coordinator.handle_batch_response(
            SimpleNamespace(persistence=persistence),
            conversation_id="room-1",
            interaction_id="interaction-1",
            answers=[{"request_id": "request-1", "user_input": "Market A"}],
            user_id="user-1",
        )

    assert persistence.claim_hitl_request.await_args.kwargs["status"] == (
        HITLStatus.ANSWER_RECORDED.value
    )
    assert all(
        call.kwargs.get("status") != HITLStatus.RESPONDED.value
        for call in persistence.mock_calls
    )
    coordinator.apply_interaction.assert_not_awaited()


@pytest.mark.asyncio
async def test_batch_rejects_mismatched_client_request_id_before_writes():
    row = _request_doc(client_request_id="authoritative-client")
    persistence = MagicMock()
    persistence.get_hitl_request = AsyncMock(return_value=row)
    persistence.claim_hitl_request = AsyncMock()
    lifecycle = MagicMock()
    lifecycle.get_interaction_strict = AsyncMock(return_value=_interaction())
    coordinator = HITLApplicationCoordinator(lifecycle=lifecycle)

    with pytest.raises(Exception, match="client_request_id does not match"):
        await coordinator.handle_batch_response(
            SimpleNamespace(persistence=persistence),
            conversation_id="room-1",
            interaction_id="interaction-1",
            answers=[{"request_id": "request-1", "user_input": "Market A"}],
            user_id="user-1",
            client_request_id="wrong-client",
        )

    persistence.claim_hitl_request.assert_not_awaited()


@pytest.mark.asyncio
async def test_materializing_interaction_is_not_emitted_before_exact_inventory_opens():
    persistence = MagicMock()
    persistence.get_room_user_message_by_message_id = AsyncMock(return_value=None)
    persistence.resolve_client_request_id_for_message_id = AsyncMock(return_value=None)
    persistence.count_hitl_requests_for_message = AsyncMock(return_value=0)
    persistence.create_hitl_request = AsyncMock(return_value=True)
    created: dict[str, dict] = {}

    async def create_request(doc):
        created[doc["request_id"]] = doc
        return True

    persistence.create_hitl_request.side_effect = create_request
    persistence.get_hitl_request = AsyncMock(
        side_effect=lambda request_id: created.get(request_id)
    )
    lifecycle = MagicMock()
    lifecycle.materialize_interaction = AsyncMock(side_effect=lambda doc: doc)
    lifecycle.attach_interaction_request = AsyncMock(
        return_value={"status": HITLInteractionStatus.MATERIALIZING.value}
    )
    lifecycle.get_interaction_strict = AsyncMock(
        return_value={"status": HITLInteractionStatus.MATERIALIZING.value}
    )
    # A configured deadline must persist as a BSON datetime, not a string.
    service = HITLService(lifecycle=lifecycle, approval_deadline_hours=24.0)
    service._persistence = persistence
    service.recover_open_interaction_projection = AsyncMock()

    snapshot = HITLRouteSnapshot(
        route=HITLApplicationRoute.SUPERVISOR_RUN,
        orchestration_run_id="run-1",
    )
    result = await service.request_interaction(
        conversation_id="room-1",
        user_message_id="user-message-1",
        interaction_id="interaction-1",
        application_route=HITLApplicationRoute.SUPERVISOR_RUN,
        public_source="supervisor",
        evidence_origin="supervisor",
        route_snapshot=snapshot,
        questions=[{"prompt": "First?"}, {"prompt": "Second?"}],
        orchestration_run_id="run-1",
    )

    assert result is not None and len(result) == 2
    assert lifecycle.attach_interaction_request.await_count == 2
    service.recover_open_interaction_projection.assert_not_awaited()
    assert all(isinstance(doc["expires_at"], datetime) for doc in created.values())

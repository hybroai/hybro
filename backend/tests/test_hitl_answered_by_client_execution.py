"""An Agent's own question answered by a granted client.

The answer is applied as the owner and its record keeps the client's id. The
id is part of the answer's identity: when the owner and a client answer the
same question at once with the same text, the second is refused instead of
replaying the first, and the Panel's own retries replay as before.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from approval.exceptions import HITLConflictError
from execution.adapters.hitl import DurableHITLApplicationPort, answers_identical
from execution.orchestrator_routing import DualRuntimeRouter
from tests.orchestrator_harness.a2a_in_memory import InMemoryHITLApplicationStore
from tests.test_execution_facade import _make_facade
from tests.test_orchestrator_a2a_hitl_recovery_auth import (
    questionnaire_answers,
    setup_waiting,
)
from tests.test_orchestrator_hitl_adapter import _answer, _interaction, _waiting_call


async def _resume(coordinator, call, route, *, client_id):
    return await coordinator.resume(
        call_record_id=call.call_record_id,
        interaction_id="interaction-1",
        interaction_revision=1,
        route_fingerprint=route.fingerprint,
        answers=questionnaire_answers(),
        authenticated_answerer_id="user-1",
        answered_by_client_id=client_id,
    )


async def test_the_answer_record_keeps_the_answering_client():
    coordinator, _ledger, hitl, _refs, _dispatch, call, route = await setup_waiting()

    assert await _resume(coordinator, call, route, client_id="client_ci") == "working"

    record = await hitl.read_answer_record("interaction-1", 1)
    assert record.authenticated_answerer_id == "user-1"
    assert record.answered_by_client_id == "client_ci"


@pytest.mark.parametrize(
    ("winner", "loser"),
    [(None, "client_ci"), ("client_ci", None), ("client_ci", "client_other")],
)
async def test_the_same_answer_from_another_answerer_is_refused(winner, loser):
    coordinator, _ledger, _hitl, _refs, dispatch, call, route = await setup_waiting()
    assert await _resume(coordinator, call, route, client_id=winner) == "working"

    with pytest.raises(PermissionError, match="changed challenge identity"):
        await _resume(coordinator, call, route, client_id=loser)

    assert len(dispatch.commands) == 1


@pytest.mark.parametrize("answerer", [None, "client_ci"])
async def test_a_retry_by_the_same_answerer_replays(answerer):
    coordinator, _ledger, _hitl, _refs, dispatch, call, route = await setup_waiting()
    assert await _resume(coordinator, call, route, client_id=answerer) == "working"

    assert await _resume(coordinator, call, route, client_id=answerer) == "working"
    assert len(dispatch.commands) == 1


async def test_the_store_refuses_a_racing_answer_from_another_answerer():
    """The loser can miss the winner's record and reach the store write."""
    port = DurableHITLApplicationPort(hitl_store=InMemoryHITLApplicationStore())
    await port.create_or_replay(
        call=_waiting_call(),
        interaction=_interaction(),
        interaction_fingerprint="fingerprint-1",
    )
    route_fingerprint = (await port.read_interaction("interaction-1"))[1].fingerprint

    async def answer(client_id):
        return await port.answer(
            interaction_id="interaction-1",
            interaction_revision=1,
            route_fingerprint=route_fingerprint,
            answers=[_answer()],
            authenticated_answerer_id="user-1",
            answered_by_client_id=client_id,
            verified_auth_reference_digests=[],
            verified_auth_references=[],
        )

    digest = await answer("client_ci")
    assert await answer("client_ci") == digest
    with pytest.raises(ValueError, match="identity conflict"):
        await answer(None)
    record = await port.read_answer_record("interaction-1", 1)
    assert record.answered_by_client_id == "client_ci"


async def test_answer_identity_includes_the_answering_client():
    coordinator, _ledger, hitl, _refs, _dispatch, call, route = await setup_waiting()
    await _resume(coordinator, call, route, client_id="client_ci")
    record = await hitl.read_answer_record("interaction-1", 1)

    assert answers_identical(record, record)
    assert not answers_identical(
        record, record.model_copy(update={"answered_by_client_id": None})
    )


# -- The router and the execution facade carry the client ----------------------


def _router_with(read, run_status="awaiting_user"):
    spec, route = read
    runtime = SimpleNamespace(
        hitl_port=SimpleNamespace(
            read_interaction=AsyncMock(return_value=(spec, route, "fingerprint"))
        ),
        run_store=SimpleNamespace(
            load=AsyncMock(return_value=SimpleNamespace(status=run_status))
        ),
        continuation=SimpleNamespace(resume=AsyncMock(return_value="working")),
        call_ledger=SimpleNamespace(load_by_record_id=AsyncMock(return_value=None)),
    )
    return DualRuntimeRouter(runtime=runtime), runtime


def _spec_and_route():
    from tests.test_orchestrator_a2a_hitl import interaction

    spec = interaction()
    route = SimpleNamespace(
        conversation_id="room-1",
        orchestration_run_id="run-1",
        call_record_id="record-1",
        interaction_revision=1,
        fingerprint="route-fingerprint",
    )
    return spec, route


async def test_the_router_passes_the_answering_client_to_the_continuation():
    router, runtime = _router_with(_spec_and_route())
    router._wake_after_hitl_resume = AsyncMock()

    await router.route_hitl_answer(
        interaction_id="interaction-1",
        answers=[{"request_id": "q1", "user_input": "a"}],
        responder_id="user-1",
        conversation_id="room-1",
        answered_by_client_id="client_ci",
    )

    kwargs = runtime.continuation.resume.await_args.kwargs
    assert kwargs["authenticated_answerer_id"] == "user-1"
    assert kwargs["answered_by_client_id"] == "client_ci"


async def test_the_facade_passes_the_answering_client_to_both_paths():
    facade, deps = _make_facade()
    router = SimpleNamespace(
        route_hitl_answer=AsyncMock(return_value="working"),
        get_pending_hitl=AsyncMock(return_value=[]),
    )
    facade.bind_orchestrator_router(router)

    await facade.resolve_hitl_batch(
        "room-1",
        "orch-1",
        [{"request_id": "q1", "user_input": "Approve"}],
        "user-1",
        client_request_id="cr-1",
        answered_by_client_id="client_ci",
    )
    assert router.route_hitl_answer.await_args.kwargs["answered_by_client_id"] == (
        "client_ci"
    )

    router.route_hitl_answer = AsyncMock(side_effect=KeyError("interaction-2"))
    deps["hitl_manager"].handle_batch_response.return_value = {
        "request_id": "interaction-2",
        "status": "accepted",
        "responder_id": "user-1",
    }
    await facade.resolve_hitl_batch(
        "room-1",
        "interaction-2",
        [{"request_id": "q1", "user_input": "Approve"}],
        "user-1",
        client_request_id="cr-1",
        answered_by_client_id="client_ci",
    )
    manager_kwargs = deps["hitl_manager"].handle_batch_response.await_args.kwargs
    assert manager_kwargs["answered_by_client_id"] == "client_ci"
    assert manager_kwargs["user_id"] == "user-1"


async def test_a_client_answer_to_an_ended_run_is_refused():
    facade, _deps = _make_facade()
    router = SimpleNamespace(
        route_hitl_answer=AsyncMock(return_value="canceled"),
        get_pending_hitl=AsyncMock(return_value=[]),
    )
    facade.bind_orchestrator_router(router)

    with pytest.raises(HITLConflictError):
        await facade.resolve_hitl_batch(
            "room-1",
            "orch-1",
            [{"request_id": "q1", "user_input": "Approve"}],
            "user-1",
            answered_by_client_id="client_ci",
        )


async def test_the_panels_answer_to_an_ended_run_reads_as_before():
    facade, _deps = _make_facade()
    router = SimpleNamespace(
        route_hitl_answer=AsyncMock(return_value="canceled"),
        get_pending_hitl=AsyncMock(return_value=[]),
    )
    facade.bind_orchestrator_router(router)

    response = await facade.resolve_hitl_batch(
        "room-1", "orch-1", [{"request_id": "q1", "user_input": "Approve"}], "user-1"
    )

    assert response.status == "accepted"
    assert router.route_hitl_answer.await_args.kwargs["answered_by_client_id"] is None

"""Supervisor approval deadline is configurable and absent by default."""

from __future__ import annotations

from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from approval.factory import create_hitl_service
from approval.repository import HITLLifecycleStore
from approval.service import HITLService
from common.dto.hitl import HITLApplicationRoute, HITLRouteSnapshot
from common.utils.time import utcnow


def _service(**kwargs) -> tuple[HITLService, dict, dict]:
    documents: dict[str, dict] = {}
    aggregate: dict = {}

    async def create_request(doc):
        documents[doc["request_id"]] = deepcopy(doc)
        return True

    persistence = MagicMock()
    persistence.create_hitl_request = AsyncMock(side_effect=create_request)
    persistence.get_hitl_request = AsyncMock(
        side_effect=lambda request_id: deepcopy(documents.get(request_id))
    )
    persistence.get_room_user_message_by_message_id = AsyncMock(return_value=None)
    persistence.resolve_client_request_id_for_message_id = AsyncMock(return_value=None)
    persistence.count_hitl_requests_for_message = AsyncMock(return_value=0)

    async def materialize(doc):
        aggregate.update(deepcopy(doc))
        return deepcopy(aggregate)

    async def attach(_interaction_id, *, request_id, **_kwargs):
        aggregate.setdefault("request_ids", []).append(request_id)
        aggregate["status"] = "open"
        return deepcopy(aggregate)

    lifecycle = MagicMock()
    lifecycle.materialize_interaction = AsyncMock(side_effect=materialize)
    lifecycle.attach_interaction_request = AsyncMock(side_effect=attach)
    lifecycle.get_interaction_strict = AsyncMock(
        side_effect=lambda _id: deepcopy(aggregate)
    )
    service = create_hitl_service(lifecycle=lifecycle, **kwargs)
    service._persistence = persistence
    service.recover_open_interaction_projection = AsyncMock(return_value=1)
    return service, aggregate, documents


async def _ask(service: HITLService) -> None:
    result = await service.request_interaction(
        conversation_id="room-1",
        user_message_id="message-1",
        interaction_id="interaction-1",
        application_route=HITLApplicationRoute.SUPERVISOR_RUN,
        public_source="supervisor",
        evidence_origin="supervisor",
        route_snapshot=HITLRouteSnapshot(
            route=HITLApplicationRoute.SUPERVISOR_RUN,
            orchestration_run_id="run-1",
        ),
        questions=[{"prompt": "Proceed?"}],
        orchestration_run_id="run-1",
    )
    assert result is not None and len(result) == 1


async def test_supervisor_approval_has_no_deadline_by_default():
    service, aggregate, documents = _service()

    await _ask(service)

    assert aggregate["expires_at"] is None
    (request,) = documents.values()
    assert request.get("expires_at") is None


@pytest.mark.parametrize("hours", [48, 48.5])
async def test_configured_supervisor_approval_deadline_is_applied(hours):
    service, aggregate, documents = _service(approval_deadline_hours=hours)

    before = utcnow()
    await _ask(service)
    after = utcnow()

    expected_low = before + timedelta(hours=hours)
    expected_high = after + timedelta(hours=hours)
    assert expected_low <= aggregate["expires_at"] <= expected_high
    (request,) = documents.values()
    assert request["expires_at"] == aggregate["expires_at"]


def test_service_constructor_accepts_the_deadline_directly():
    assert HITLService()._approval_deadline_hours is None
    assert HITLService(approval_deadline_hours=2.5)._approval_deadline_hours == 2.5


async def test_undated_interactions_are_never_due_for_expiry():
    interactions = MagicMock()
    interactions.find = AsyncMock(return_value=[])
    store = HITLLifecycleStore(
        interactions=interactions,
        resume_commands=MagicMock(),
        hitl_requests=MagicMock(),
    )

    rows = [row async for row in store.iter_due_interactions(utcnow())]

    assert rows == []
    query = interactions.find.await_args.args[0]
    assert query["expires_at"]["$ne"] is None


# -- container: failed canonical HITL terminalization -----------------------


def _orchestrator_runtime(order: list[str], *, run_status: str = "awaiting_user"):
    run = SimpleNamespace(
        run_id="run-1",
        conversation_id="room-1",
        status=run_status,
        tool_catalog=object(),
        cancellation_command_id=None,
        client_request_id="request-1",
        request=SimpleNamespace(user_message_id="message-1"),
        state_version=1,
    )

    async def cancel_run(run_id, **kwargs):
        order.append(f"cancel_run:{run_id}:{kwargs}")
        return {"call-1": "canceled"}

    async def terminalize(run_id, **kwargs):
        order.append(f"terminalize:{run_id}:{kwargs['status']}")

    return SimpleNamespace(
        run_store=SimpleNamespace(load=AsyncMock(return_value=run)),
        cancellation_coordinator=SimpleNamespace(cancel_run=cancel_run),
        kernel_factory=lambda _catalog: SimpleNamespace(terminalize=terminalize),
        session_host=SimpleNamespace(),
    )


async def test_failed_hitl_terminalization_cancels_parallel_calls_locally_first():
    from container import terminalize_canonical_hitl_run

    order: list[str] = []
    handled = await terminalize_canonical_hitl_run(
        _orchestrator_runtime(order),
        SimpleNamespace(orchestration_run_id="run-1", request_id="request-1"),
        terminal_status="expired",
        reason="Human input deadline expired",
        session_listener=AsyncMock(),
    )

    assert handled is True
    # The Run ends for good, so each Agent with a remote task is told to stop.
    assert order == [
        "cancel_run:run-1:{'reason': 'canonical_hitl_failed', 'notify_remote': True}",
        "terminalize:run-1:failed",
    ]


async def test_canceled_hitl_terminalization_tells_the_agents_to_stop():
    from container import terminalize_canonical_hitl_run

    order: list[str] = []
    runtime = _orchestrator_runtime(order, run_status="canceling")
    run = await runtime.run_store.load("run-1")
    run.cancellation_command_id = "cancel:run-1:user_requested"
    runtime.session_host = SimpleNamespace(
        reconcile_cancellation=AsyncMock(), signal_run_cancellation=AsyncMock()
    )

    handled = await terminalize_canonical_hitl_run(
        runtime,
        SimpleNamespace(orchestration_run_id="run-1", request_id="request-1"),
        terminal_status="canceled",
        reason="The owner canceled the question",
        session_listener=AsyncMock(),
    )

    assert handled is True
    assert order == [
        "cancel_run:run-1:{'reason': 'canonical_hitl_canceled', 'notify_remote': True}",
    ]


async def test_failed_hitl_terminalization_defers_while_local_cancel_is_pending():
    from container import terminalize_canonical_hitl_run

    order: list[str] = []
    runtime = _orchestrator_runtime(order)

    async def pending(run_id, **kwargs):
        order.append("cancel_run")
        return {"call-1": "cancel_pending"}

    runtime.cancellation_coordinator.cancel_run = pending

    with pytest.raises(RuntimeError, match="local Agent-call cancellation"):
        await terminalize_canonical_hitl_run(
            runtime,
            SimpleNamespace(orchestration_run_id="run-1", request_id="request-1"),
            terminal_status="expired",
            reason="Human input deadline expired",
            session_listener=AsyncMock(),
        )
    assert order == ["cancel_run"]


async def test_hitl_terminalization_ignores_missing_runtime():
    from container import terminalize_canonical_hitl_run

    assert (
        await terminalize_canonical_hitl_run(
            None,
            SimpleNamespace(orchestration_run_id="run-1", request_id="request-1"),
            terminal_status="expired",
            reason="expired",
            session_listener=AsyncMock(),
        )
        is False
    )

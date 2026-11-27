"""
Unit tests for HITL Service.

Tests cover:
- Creating HITL requests
- Handling user responses
- Getting pending requests
- Canceling requests
- Max rounds enforcement
- SSE event emission
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from approval.exceptions import HITLConflictError
from approval.service import HITLService
from common.dto.hitl import HITLPublicSource
from models.hitl import (
    HITLEventType,
    HITLPromptType,
    HITLStatus,
)

# =============================================================================
# HITL Service Fixtures
# =============================================================================


@pytest.fixture
def hitl_service():
    """Create a fresh HITLService instance for testing."""
    service = HITLService()
    # Reset lazy-loaded dependencies
    service._persistence = None
    service._delivery = None
    service._agent_reply = None
    return service


@pytest.fixture
def mock_hitl_db_service():
    """Create mock database service for HITL operations."""
    mock = MagicMock()
    mock.create_hitl_request = AsyncMock(return_value=True)
    mock.get_hitl_request = AsyncMock(return_value=None)
    mock.update_hitl_request = AsyncMock(return_value=True)
    mock.get_pending_hitl_requests = AsyncMock(return_value=[])
    mock.get_pending_hitl_requests_for_message = AsyncMock(return_value=[])
    mock.count_hitl_requests_for_message = AsyncMock(return_value=0)
    mock.find_pending_hitl_request_for_agent_message = AsyncMock(return_value=None)
    mock.create_or_reuse_pending_hitl_request = AsyncMock(return_value=None)
    mock.persist_pending_hitl_on_agent_message = AsyncMock(return_value=True)
    mock.update_agent_message_task_state = AsyncMock(return_value=True)
    mock.persist_hitl_request_id_on_message = AsyncMock(return_value=True)
    mock.persist_hitl_user_answer = AsyncMock(return_value=True)
    mock.persist_hitl_interaction_metadata = AsyncMock(return_value=True)
    mock.claim_hitl_request = AsyncMock(return_value=None)
    mock.fenced_update_hitl_request = AsyncMock(return_value=True)
    mock.cas_update_hitl_request = AsyncMock(return_value=True)
    mock.claim_hitl_open_projection = AsyncMock(return_value=None)
    mock.complete_hitl_open_projection = AsyncMock(return_value=None)
    mock.release_hitl_open_projection = AsyncMock(return_value=True)
    mock.reset_last_notified_state = AsyncMock()
    return mock


@pytest.fixture
def mock_hitl_delivery():
    """Create mock typed delivery port for HITL events."""
    mock = MagicMock()
    mock.emit = AsyncMock()
    return mock


# =============================================================================
# Request Input Tests
# =============================================================================


@pytest.mark.parametrize(
    ("prompt_type", "choices"),
    [
        (HITLPromptType.SINGLE_CHOICE, ["a", "b"]),
        (HITLPromptType.MULTI_CHOICE, ["a", "b"]),
        (HITLPromptType.CONFIRMATION, None),
        (HITLPromptType.APPROVAL, None),
        (HITLPromptType.AUTHENTICATION, None),
    ],
)
def test_agent_pending_hydration_preserves_typed_controls(
    sample_hitl_request,
    prompt_type,
    choices,
):
    from approval.service import _public_hitl_request_from_doc

    doc = sample_hitl_request.model_copy(
        update={
            "public_source": HITLPublicSource.AGENT,
            "prompt": "Typed question?",
            "prompt_type": prompt_type,
            "choices": choices,
        }
    ).model_dump(mode="python")

    hydrated = _public_hitl_request_from_doc(doc)

    assert hydrated.prompt_type == prompt_type
    assert hydrated.choices == choices


def test_hitl_request_translator_preserves_pending_api_shape(sample_hitl_request):
    from approval.translators import model_hitl_request_to_common

    sample_hitl_request.display_message_id = "display-msg-1"
    sample_hitl_request.interaction_id = "group-1"
    sample_hitl_request.question_count = 2
    sample_hitl_request.question_index = 1
    sample_hitl_request.client_request_id = "cr-hitl-1"

    common = model_hitl_request_to_common(sample_hitl_request)

    assert common.request_id == sample_hitl_request.request_id
    assert common.message_id == "display-msg-1"
    assert common.client_request_id == "cr-hitl-1"
    assert common.interaction_id == "group-1"
    assert common.question_count == 2
    assert common.question_index == 1


def test_hitl_response_translator_preserves_route_dict_shape():
    from approval.translators import hitl_response_dict_to_common

    response = hitl_response_dict_to_common(
        {
            "status": "ok",
            "request_id": "req-1",
            "reclaimed": True,
            "error": None,
        }
    )

    assert response.status == "ok"
    assert response.request_id == "req-1"
    assert response.reclaimed is True


@pytest.mark.asyncio
async def test_canonical_interaction_cancel_claims_run_before_hitl_mutation():
    order: list[str] = []
    interaction = {
        "interaction_id": "interaction-1",
        "room_id": "room-1",
        "orchestration_run_id": "run-1",
        "status": "materializing",
        "version": 1,
        "request_ids": [],
    }
    lifecycle = MagicMock()
    lifecycle.get_interaction_strict = AsyncMock(
        side_effect=[interaction, {**interaction, "status": "canceled", "version": 2}]
    )

    async def terminalize(*_args, **_kwargs):
        order.append("hitl")
        return {**interaction, "status": "canceled", "version": 2}

    lifecycle.terminalize_interaction = terminalize

    async def request_cancellation(run_id, *, cause):
        assert (run_id, cause) == ("run-1", "user_requested")
        order.append("run")
        return "canceling"

    service = HITLService(
        lifecycle=lifecycle,
        canonical_run_state_reader=AsyncMock(return_value="running"),
        canonical_cancellation_requester=request_cancellation,
    )

    version = await service.cancel_interaction_by_user(
        conversation_id="room-1", interaction_id="interaction-1", expected_version=1
    )

    assert version == 2
    assert order == ["run", "hitl"]


@pytest.mark.asyncio
@pytest.mark.parametrize("cause", ["user_requested", "room_closed"])
async def test_canonical_interaction_cancel_claims_the_run_with_its_cause(cause):
    """A check rule's question, canceled through the service, claims its Run
    with the caller's cause (a session close passes room_closed)."""
    from container import request_canonical_hitl_cancellation

    interaction = {
        "interaction_id": "interaction-1",
        "room_id": "room-1",
        "orchestration_run_id": "run-1",
        "status": "materializing",
        "version": 1,
        "request_ids": [],
    }
    lifecycle = MagicMock()
    lifecycle.get_interaction_strict = AsyncMock(
        side_effect=[interaction, {**interaction, "status": "canceled", "version": 2}]
    )
    lifecycle.terminalize_interaction = AsyncMock(
        return_value={**interaction, "status": "canceled", "version": 2}
    )
    run = SimpleNamespace(run_id="run-1", state_version=3, status="awaiting_user")
    run_store = SimpleNamespace(
        load=AsyncMock(return_value=run),
        request_cancellation=AsyncMock(
            return_value=SimpleNamespace(
                outcome="accepted",
                run=SimpleNamespace(**{**run.__dict__, "status": "canceling"}),
            )
        ),
    )
    runtime = SimpleNamespace(run_store=run_store)
    service = HITLService(
        lifecycle=lifecycle,
        canonical_run_state_reader=AsyncMock(return_value="awaiting_user"),
        canonical_cancellation_requester=(
            lambda run_id, *, cause: request_canonical_hitl_cancellation(
                runtime, run_id, cause=cause
            )
        ),
    )

    version = await service.cancel_interaction_by_user(
        conversation_id="room-1",
        interaction_id="interaction-1",
        expected_version=1,
        cause=cause,
    )

    assert version == 2
    claim = run_store.request_cancellation.await_args
    assert claim.args == ("run-1",)
    assert claim.kwargs["cause"] == cause
    assert claim.kwargs["command_id"] == f"cancel:run-1:{cause}"
    assert claim.kwargs["expected_state_version"] == 3


@pytest.mark.asyncio
async def test_canonical_interaction_cancel_loses_to_completed_run():
    interaction = {
        "interaction_id": "interaction-1",
        "room_id": "room-1",
        "orchestration_run_id": "run-1",
        "status": "materializing",
        "version": 1,
        "request_ids": [],
    }
    lifecycle = MagicMock()
    lifecycle.get_interaction_strict = AsyncMock(return_value=interaction)
    lifecycle.terminalize_interaction = AsyncMock()
    service = HITLService(
        lifecycle=lifecycle,
        canonical_run_state_reader=AsyncMock(return_value="running"),
        canonical_cancellation_requester=AsyncMock(return_value="completed"),
    )

    with pytest.raises(HITLConflictError, match="lifecycle winner"):
        await service.cancel_interaction_by_user(
            conversation_id="room-1", interaction_id="interaction-1", expected_version=1
        )

    lifecycle.terminalize_interaction.assert_not_awaited()


class TestEmitHitlEvent:
    """Tests for _emit_hitl_event method."""

    @pytest.mark.asyncio
    async def test_emits_input_requested_event(
        self,
        hitl_service,
        mock_hitl_db_service,
        mock_hitl_delivery,
        sample_hitl_request,
    ):
        """Should emit correct data for INPUT_REQUESTED event."""
        hitl_service._persistence = mock_hitl_db_service
        hitl_service._delivery = mock_hitl_delivery

        await hitl_service._emit_hitl_event(
            conversation_id=sample_hitl_request.conversation_id,
            event_type=HITLEventType.INPUT_REQUESTED,
            request=sample_hitl_request,
        )

        mock_hitl_delivery.emit.assert_awaited_once()
        event = mock_hitl_delivery.emit.await_args.args[0]

        assert event.conversation_id == sample_hitl_request.conversation_id
        assert event.event_type == "hitl_request"
        assert event.request_id == sample_hitl_request.request_id
        assert event.prompt == sample_hitl_request.prompt
        assert event.source == sample_hitl_request.public_source.value

    @pytest.mark.asyncio
    async def test_emits_status_update_event(
        self,
        hitl_service,
        mock_hitl_db_service,
        mock_hitl_delivery,
        sample_hitl_request,
    ):
        """Should emit correct data for status update events."""
        hitl_service._persistence = mock_hitl_db_service
        hitl_service._delivery = mock_hitl_delivery

        await hitl_service._emit_hitl_event(
            conversation_id=sample_hitl_request.conversation_id,
            event_type=HITLEventType.INPUT_RECEIVED,
            request=sample_hitl_request,
        )

        event = mock_hitl_delivery.emit.await_args.args[0]
        assert event.event_type == "hitl_resolved"
        assert event.status == HITLStatus.RESPONDED.value

    @pytest.mark.asyncio
    async def test_includes_error_message_on_error_event(
        self,
        hitl_service,
        mock_hitl_db_service,
        mock_hitl_delivery,
        sample_hitl_request,
    ):
        """Should include error message for ERROR events."""
        hitl_service._persistence = mock_hitl_db_service
        hitl_service._delivery = mock_hitl_delivery

        await hitl_service._emit_hitl_event(
            conversation_id=sample_hitl_request.conversation_id,
            event_type=HITLEventType.ERROR,
            request=sample_hitl_request,
            error="Something went wrong",
        )

        event = mock_hitl_delivery.emit.await_args.args[0]
        assert event.error_message == "Something went wrong"

    @pytest.mark.asyncio
    async def test_resolves_client_request_id_from_message_id_when_user_row_missing(
        self,
        hitl_service,
        mock_hitl_db_service,
        mock_hitl_delivery,
        sample_hitl_request,
    ):
        """SSE payload should include client_request_id via DB resolver on message_id."""
        mock_hitl_db_service.get_room_user_message_by_message_id = AsyncMock(
            return_value=None
        )
        mock_hitl_db_service.resolve_client_request_id_for_message_id = AsyncMock(
            return_value="cr-resolved-via-message-id"
        )
        hitl_service._persistence = mock_hitl_db_service
        hitl_service._delivery = mock_hitl_delivery

        req = sample_hitl_request.model_copy(
            update={"display_message_id": "test-agent-msg-001"}
        )

        await hitl_service._emit_hitl_event(
            conversation_id=req.conversation_id,
            event_type=HITLEventType.INPUT_REQUESTED,
            request=req,
        )

        event = mock_hitl_delivery.emit.await_args.args[0]
        assert event.message_id == "test-agent-msg-001"
        assert event.client_request_id == "cr-resolved-via-message-id"
        mock_hitl_db_service.resolve_client_request_id_for_message_id.assert_called_once_with(
            "test-agent-msg-001"
        )

    @pytest.mark.asyncio
    async def test_prefers_user_message_client_request_id_over_resolver(
        self,
        hitl_service,
        mock_hitl_db_service,
        mock_hitl_delivery,
        sample_hitl_request,
    ):
        """When user row already has client_request_id, do not replace with resolver."""
        user_row = MagicMock()
        user_row.client_request_id = "cr-from-user-row"
        mock_hitl_db_service.get_room_user_message_by_message_id = AsyncMock(
            return_value=user_row
        )
        mock_hitl_db_service.resolve_client_request_id_for_message_id = AsyncMock(
            return_value="cr-from-resolver"
        )
        hitl_service._persistence = mock_hitl_db_service
        hitl_service._delivery = mock_hitl_delivery

        req = sample_hitl_request.model_copy(
            update={"display_message_id": "test-agent-msg-001"}
        )

        await hitl_service._emit_hitl_event(
            conversation_id=req.conversation_id,
            event_type=HITLEventType.INPUT_REQUESTED,
            request=req,
        )

        event = mock_hitl_delivery.emit.await_args.args[0]
        assert event.client_request_id == "cr-from-user-row"
        mock_hitl_db_service.resolve_client_request_id_for_message_id.assert_not_called()

    @pytest.mark.asyncio
    async def test_emitted_hitl_events_include_related_message_id(
        self,
        hitl_service,
        mock_hitl_db_service,
        mock_hitl_delivery,
        sample_hitl_request,
    ):
        """HITL events should include related_message_id for frontend resume correlation."""
        hitl_service._persistence = mock_hitl_db_service
        hitl_service._delivery = mock_hitl_delivery

        await hitl_service._emit_hitl_event(
            conversation_id=sample_hitl_request.conversation_id,
            event_type=HITLEventType.INPUT_REQUESTED,
            request=sample_hitl_request,
        )
        request_event = mock_hitl_delivery.emit.await_args.args[0]
        assert request_event.related_message_id == sample_hitl_request.user_message_id

        await hitl_service._emit_hitl_event(
            conversation_id=sample_hitl_request.conversation_id,
            event_type=HITLEventType.INPUT_RECEIVED,
            request=sample_hitl_request,
        )
        response_event = mock_hitl_delivery.emit.await_args.args[0]
        assert response_event.related_message_id == sample_hitl_request.user_message_id

"""
Unit tests for Room Center API endpoints.

Tests cover:
- Room creation
- Room settings inquiry
- Room ownership verification
- Message creation and retrieval
- Authorization checks
"""

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from agent.protocols import AgentSuggestion, AgentSuggestionResult
from api_gateway.routes.room_routes import (
    AgentSuggestionBody,
    CreateRoomBody,
    create_new_room,
    inquiry_active_runs,
    inquiry_room_messages,
    inquiry_room_setting,
    suggest_agents,
    verify_room_ownership,
)
from chat import ChatService
from common.types import (
    Artifact,
    DataPart,
    Message,
    MessageRole,
    Part,
    Task,
    TaskState,
    TaskStatus,
    TextPart,
)
from conversation.dto import RoomTimelineEntry, RoomTimelinePage
from conversation.runtime import ConversationRuntime
from conversation.timeline_projection import RoomTimelineProjector
from models.response import (
    RoomCenterRoomMessageResponse,
    RoomCenterRoomSettingResponse,
)
from models.room import MessageContent, RoomAgentMessage, RoomUserMessage

# =============================================================================
# Room Ownership Verification Tests
# =============================================================================


def _bind_timeline_projector(
    runtime: ConversationRuntime,
    *,
    hitl_reader=None,
) -> None:
    if hitl_reader is None:
        hitl_reader = SimpleNamespace(get_hitl_request=AsyncMock(return_value=None))
    attachment_reader = SimpleNamespace(get_for_room_file=AsyncMock(return_value=None))
    runtime.bind_timeline_projector(
        RoomTimelineProjector(
            hitl_reader=hitl_reader,
            attachment_metadata_reader=attachment_reader,
        )
    )


def _chat_rooms_reading(runtime: ConversationRuntime) -> ChatService:
    service = ChatService()
    service.bind_message_reader(runtime.inquiry_room_messages_by_room_id)
    return service


class TestChatRoomsService:
    @pytest.mark.asyncio
    async def test_fails_before_bind_when_service_unbound(self):
        with pytest.raises(
            RuntimeError,
            match=r"ChatService\.bind_facade\(\) not called - startup incomplete",
        ):
            await ChatService().create_room(
                owner_id="owner",
                owner_name="Owner",
                room_name="Room",
                extend_info=None,
                membership_seed_input=None,
                room_agent_ids=None,
                seed_group_id=None,
                seed_all_current_agents=None,
            )

    @pytest.mark.asyncio
    async def test_message_reads_fail_before_reader_is_bound(self):
        with pytest.raises(RuntimeError, match="message reader has not been bound"):
            await ChatService().read_messages("room-1", limit=None, cursor=None)

    @pytest.mark.asyncio
    async def test_message_reads_delegate_to_the_bound_reader(self):
        reader = AsyncMock(return_value="messages")
        service = ChatService()
        service.bind_message_reader(reader)

        assert await service.read_messages("room-1", limit=5, cursor="c1") == "messages"
        forwarded = reader.await_args.args[0]
        assert (forwarded.conversation_id, forwarded.limit, forwarded.cursor) == (
            "room-1",
            5,
            "c1",
        )


class TestVerifyRoomOwnership:
    """Tests for verify_room_ownership helper function."""

    @pytest.mark.asyncio
    async def test_raises_400_when_room_id_empty(self, mock_user):
        """Should raise 400 when conversation_id is empty."""
        with pytest.raises(HTTPException) as exc_info:
            await verify_room_ownership("", mock_user, MagicMock())

        assert exc_info.value.status_code == 400
        assert "conversation_id is required" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_raises_404_when_room_not_found(self, mock_user, mock_db_service):
        """Should raise 404 when room doesn't exist."""
        mock_db_service.get_room_by_room_id.return_value = None

        with pytest.raises(HTTPException) as exc_info:
            await verify_room_ownership("nonexistent-room", mock_user, mock_db_service)

        assert exc_info.value.status_code == 404
        assert "Room not found" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_raises_403_when_user_not_owner(
        self, mock_user, mock_user_2, mock_db_service, sample_room
    ):
        """Should raise 403 when user is not the room owner."""
        # Room owned by mock_user, but mock_user_2 is trying to access
        mock_db_service.get_room_by_room_id.return_value = sample_room

        with pytest.raises(HTTPException) as exc_info:
            await verify_room_ownership(
                sample_room.conversation_id, mock_user_2, mock_db_service
            )

        assert exc_info.value.status_code == 403
        assert "permission" in exc_info.value.detail.lower()

    @pytest.mark.asyncio
    async def test_passes_when_user_is_owner(
        self, mock_user, mock_db_service, sample_room
    ):
        """Should pass without exception when user is the owner."""
        mock_db_service.get_room_by_room_id.return_value = sample_room

        # Should not raise
        await verify_room_ownership(
            sample_room.conversation_id, mock_user, mock_db_service
        )


# =============================================================================
# Room Creation Tests
# =============================================================================


class TestCreateNewRoom:
    """Tests for create_new_room endpoint."""

    @pytest.mark.asyncio
    async def test_creates_room_with_user_as_owner(self, mock_user, mock_chat_rooms):
        """Should create room with authenticated user as owner."""
        payload = CreateRoomBody(
            room_name="Test Room",
            room_owner_name="Test User",
            room_agent_ids=["agent-1"],
            extend_info={"debateMode": True, "use_supervisor": True},
        )

        expected_response = RoomCenterRoomSettingResponse(
            success=True,
            conversation_id="new-room-id",
            status_code=200,
        )
        mock_chat_rooms.create_room.return_value = expected_response

        response = await create_new_room(payload, mock_user, rooms=mock_chat_rooms)

        assert response.success is True
        assert response.conversation_id == "new-room-id"

        # Verify the request was made with user's ID as owner
        mock_chat_rooms.create_room.assert_awaited_once_with(
            owner_id=mock_user.user_id,
            owner_name="Test User",
            room_name="Test Room",
            extend_info={"debateMode": True, "use_supervisor": True},
            membership_seed_input=None,
            room_agent_ids=["agent-1"],
            seed_group_id=None,
            seed_all_current_agents=None,
        )

    @pytest.mark.asyncio
    async def test_creates_room_with_seed_group(self, mock_user, mock_chat_rooms):
        """Should forward seed_group_id when specified."""
        payload = CreateRoomBody(
            room_name="Group Room",
            room_owner_name="Test User",
            seed_group_id="group-123",
        )

        expected_response = RoomCenterRoomSettingResponse(
            success=True, conversation_id="new-room-id"
        )
        mock_chat_rooms.create_room.return_value = expected_response

        await create_new_room(payload, mock_user, rooms=mock_chat_rooms)

        call_kwargs = mock_chat_rooms.create_room.await_args.kwargs
        assert call_kwargs["seed_group_id"] == "group-123"


# =============================================================================
# Room Settings Inquiry Tests
# =============================================================================


class TestInquiryRoomSetting:
    """Tests for inquiry_room_setting endpoint."""

    @pytest.mark.asyncio
    async def test_returns_room_settings_for_owner(
        self, mock_user, sample_room, patch_chat_rooms_deps
    ):
        from common.dto import RunInfo
        from models.response import ActiveRunRef

        patch_chat_rooms_deps[
            "db_service"
        ].get_room_by_room_id.return_value = sample_room
        expected_response = RoomCenterRoomSettingResponse(
            success=True,
            conversation_id=sample_room.conversation_id,
            room=sample_room,
        )
        patch_chat_rooms_deps["chat_rooms"].get_room.return_value = expected_response
        patch_chat_rooms_deps["execution_engine"].get_runs_for_room.return_value = [
            RunInfo(
                run_id="run-1",
                conversation_id=sample_room.conversation_id,
                state="processing",
                trigger_message_id="m1",
                agent_id="a1",
                seq=1,
            )
        ]

        response = await inquiry_room_setting(
            sample_room.conversation_id,
            mock_user,
            store=patch_chat_rooms_deps["db_service"],
            engine=patch_chat_rooms_deps["execution_engine"],
            rooms=patch_chat_rooms_deps["chat_rooms"],
        )

        assert response.success is True
        assert response.conversation_id == sample_room.conversation_id
        assert response.active_runs == [
            ActiveRunRef(
                state="processing",
                trigger_message_id="m1",
                agent_id="a1",
            )
        ]
        patch_chat_rooms_deps[
            "execution_engine"
        ].get_runs_for_room.assert_awaited_once_with(sample_room.conversation_id)

    @pytest.mark.asyncio
    async def test_inquiry_room_setting_degrades_when_active_run_lookup_fails(
        self, mock_user, sample_room, patch_chat_rooms_deps
    ):

        patch_chat_rooms_deps[
            "db_service"
        ].get_room_by_room_id.return_value = sample_room
        patch_chat_rooms_deps[
            "chat_rooms"
        ].get_room.return_value = RoomCenterRoomSettingResponse(
            success=True,
            conversation_id=sample_room.conversation_id,
            room=sample_room,
        )
        patch_chat_rooms_deps[
            "execution_engine"
        ].get_runs_for_room.side_effect = RuntimeError("runs unavailable")

        response = await inquiry_room_setting(
            sample_room.conversation_id,
            mock_user,
            store=patch_chat_rooms_deps["db_service"],
            engine=patch_chat_rooms_deps["execution_engine"],
            rooms=patch_chat_rooms_deps["chat_rooms"],
        )

        assert response.success is True
        assert response.conversation_id == sample_room.conversation_id
        assert response.active_runs == []

    @pytest.mark.asyncio
    async def test_inquiry_room_setting_uses_requested_room_id_for_active_run_lookup(
        self, mock_user, sample_room, patch_chat_rooms_deps
    ):
        from common.dto import RunInfo

        patch_chat_rooms_deps[
            "db_service"
        ].get_room_by_room_id.return_value = sample_room
        patch_chat_rooms_deps[
            "chat_rooms"
        ].get_room.return_value = RoomCenterRoomSettingResponse(
            success=True,
            room=sample_room,
        )
        patch_chat_rooms_deps["execution_engine"].get_runs_for_room.return_value = [
            RunInfo(
                run_id="run-1",
                conversation_id=sample_room.conversation_id,
                state="processing",
                trigger_message_id="m1",
                agent_id="a1",
                seq=1,
            )
        ]

        response = await inquiry_room_setting(
            sample_room.conversation_id,
            mock_user,
            store=patch_chat_rooms_deps["db_service"],
            engine=patch_chat_rooms_deps["execution_engine"],
            rooms=patch_chat_rooms_deps["chat_rooms"],
        )

        assert response.success is True
        assert response.active_runs is not None
        assert response.active_runs[0].trigger_message_id == "m1"
        patch_chat_rooms_deps[
            "execution_engine"
        ].get_runs_for_room.assert_awaited_once_with(sample_room.conversation_id)

    @pytest.mark.asyncio
    async def test_inquiry_room_setting_ignores_mismatched_response_room_id_for_active_run_lookup(
        self, mock_user, sample_room, patch_chat_rooms_deps
    ):
        from common.dto import RunInfo

        patch_chat_rooms_deps[
            "db_service"
        ].get_room_by_room_id.return_value = sample_room
        patch_chat_rooms_deps[
            "chat_rooms"
        ].get_room.return_value = RoomCenterRoomSettingResponse(
            success=True,
            conversation_id="other-room",
            room=sample_room,
        )
        patch_chat_rooms_deps["execution_engine"].get_runs_for_room.return_value = [
            RunInfo(
                run_id="run-1",
                conversation_id=sample_room.conversation_id,
                state="processing",
                trigger_message_id="m1",
                agent_id="a1",
                seq=1,
            )
        ]

        response = await inquiry_room_setting(
            sample_room.conversation_id,
            mock_user,
            store=patch_chat_rooms_deps["db_service"],
            engine=patch_chat_rooms_deps["execution_engine"],
            rooms=patch_chat_rooms_deps["chat_rooms"],
        )

        assert response.success is True
        assert response.active_runs is not None
        assert response.active_runs[0].trigger_message_id == "m1"
        patch_chat_rooms_deps[
            "execution_engine"
        ].get_runs_for_room.assert_awaited_once_with(sample_room.conversation_id)

    @pytest.mark.asyncio
    async def test_raises_403_for_non_owner(
        self, mock_user_2, mock_db_service, sample_room
    ):
        """Should raise 403 when user is not the owner."""

        mock_db_service.get_room_by_room_id.return_value = sample_room

        with pytest.raises(HTTPException) as exc_info:
            await inquiry_room_setting(
                sample_room.conversation_id,
                mock_user_2,
                store=mock_db_service,
                engine=MagicMock(),
                rooms=MagicMock(),
            )

        assert exc_info.value.status_code == 403


# =============================================================================
# Active runs inquiry (lightweight reconcile)
# =============================================================================


class TestInquiryActiveRuns:
    """Tests for inquiry_active_runs endpoint."""

    @pytest.mark.asyncio
    async def test_active_run_payload_does_not_expose_internal_run_id_or_seq(
        self, mock_user, sample_room, patch_chat_rooms_deps
    ):
        from common.dto import RunInfo

        patch_chat_rooms_deps[
            "db_service"
        ].get_room_by_room_id.return_value = sample_room
        patch_chat_rooms_deps["execution_engine"].get_runs_for_room.return_value = [
            RunInfo(
                run_id="private-run-1",
                conversation_id=sample_room.conversation_id,
                state="processing",
                trigger_message_id="msg-active",
                agent_id="a1",
                seq=41,
                updated_at=datetime(2026, 7, 13, 12, 0, tzinfo=UTC),
            )
        ]

        response = await inquiry_active_runs(
            sample_room.conversation_id,
            mock_user,
            store=patch_chat_rooms_deps["db_service"],
            engine=patch_chat_rooms_deps["execution_engine"],
        )

        assert response.active_runs is not None
        public_run = response.model_dump(mode="json")["active_runs"][0]
        assert "run_id" not in public_run
        assert "seq" not in public_run
        assert public_run == {
            "state": "processing",
            "trigger_message_id": "msg-active",
            "agent_id": "a1",
            "updated_at": "2026-07-13T12:00:00Z",
        }

    @pytest.mark.asyncio
    async def test_returns_active_runs_for_owner(
        self, mock_user, sample_room, patch_chat_rooms_deps
    ):
        from common.dto import RunInfo

        patch_chat_rooms_deps[
            "db_service"
        ].get_room_by_room_id.return_value = sample_room
        patch_chat_rooms_deps["execution_engine"].get_runs_for_room.return_value = [
            RunInfo(
                run_id="run-1",
                conversation_id=sample_room.conversation_id,
                state="processing",
                trigger_message_id="msg-active",
                agent_id="a1",
                seq=1,
            )
        ]
        response = await inquiry_active_runs(
            sample_room.conversation_id,
            mock_user,
            store=patch_chat_rooms_deps["db_service"],
            engine=patch_chat_rooms_deps["execution_engine"],
        )

        assert response.success is True
        assert response.conversation_id == sample_room.conversation_id
        assert response.active_runs is not None
        assert len(response.active_runs) == 1
        assert response.active_runs[0].trigger_message_id == "msg-active"
        patch_chat_rooms_deps[
            "execution_engine"
        ].get_runs_for_room.assert_awaited_once_with(sample_room.conversation_id)

    @pytest.mark.asyncio
    async def test_inquiry_active_runs_degrades_when_execution_lookup_fails(
        self, mock_user, sample_room, patch_chat_rooms_deps
    ):

        patch_chat_rooms_deps[
            "db_service"
        ].get_room_by_room_id.return_value = sample_room
        patch_chat_rooms_deps[
            "execution_engine"
        ].get_runs_for_room.side_effect = RuntimeError("runs unavailable")
        response = await inquiry_active_runs(
            sample_room.conversation_id,
            mock_user,
            store=patch_chat_rooms_deps["db_service"],
            engine=patch_chat_rooms_deps["execution_engine"],
        )

        assert response.success is True
        assert response.active_runs == []

    @pytest.mark.asyncio
    async def test_raises_403_for_non_owner(
        self, mock_user_2, mock_db_service, sample_room
    ):

        mock_db_service.get_room_by_room_id.return_value = sample_room

        with pytest.raises(HTTPException) as exc_info:
            await inquiry_active_runs(
                sample_room.conversation_id,
                mock_user_2,
                store=mock_db_service,
                engine=MagicMock(),
            )

        assert exc_info.value.status_code == 403


# =============================================================================
# Message Tests
# =============================================================================


class TestInquiryRoomMessages:
    """Tests for inquiry_room_messages endpoint."""

    @pytest.mark.asyncio
    async def test_ownership_precedes_invalid_cursor_validation(
        self, mock_user_2, sample_room, mock_db_service
    ):
        mock_db_service.get_room_by_room_id.return_value = sample_room
        rooms = MagicMock()

        with pytest.raises(HTTPException) as exc_info:
            await inquiry_room_messages(
                sample_room.conversation_id,
                cursor="not-valid",
                user=mock_user_2,
                store=mock_db_service,
                rooms=rooms,
            )

        assert exc_info.value.status_code == 403
        rooms.read_messages.assert_not_called()

    @pytest.mark.asyncio
    async def test_returns_messages_for_owner(
        self, mock_user, sample_room, patch_chat_rooms_deps
    ):
        """Should return messages when user is owner."""

        patch_chat_rooms_deps[
            "db_service"
        ].get_room_by_room_id.return_value = sample_room
        expected_response = RoomCenterRoomMessageResponse(
            success=True,
            message_list=[],
        )
        patch_chat_rooms_deps[
            "chat_rooms"
        ].read_messages.return_value = expected_response

        response = await inquiry_room_messages(
            sample_room.conversation_id,
            limit=37,
            cursor="opaque-cursor",
            user=mock_user,
            store=patch_chat_rooms_deps["db_service"],
            rooms=patch_chat_rooms_deps["chat_rooms"],
        )

        assert response.success is True
        patch_chat_rooms_deps["chat_rooms"].read_messages.assert_awaited_once_with(
            sample_room.conversation_id, limit=37, cursor="opaque-cursor"
        )

    @pytest.mark.asyncio
    async def test_returns_public_user_history_without_private_orchestration_state(
        self, mock_user, sample_room, patch_chat_rooms_deps
    ):
        private_sentinel = "PRIVATE_SENTINEL_user_history_boundary"
        public_extend_info = {
            "quoted_text": "Public quoted excerpt",
            "quoted_sender_name": "Agent One",
            "quote_id": "quote-public-001",
            "orchestration_status": "failed",
        }
        user_message = RoomUserMessage(
            conversation_id=sample_room.conversation_id,
            message_id="user-msg-privacy-001",
            user_id=mock_user.user_id,
            client_request_id="client-request-top-level-001",
            message_content=MessageContent(message_text="Please review the quote"),
            extend_info={
                **public_extend_info,
                "turn_completion_kind": "synthesis",
                "client_request_id": private_sentinel,
                "orchestration": True,
                "orchestration_run_id": private_sentinel,
                "candidate_scope_snapshot_id": private_sentinel,
                "candidate_agent_ids": [private_sentinel],
                "agent_registry": [{"agent_id": private_sentinel}],
                "conversation_context": private_sentinel,
                "room_config": {"explicit_mentions": [private_sentinel]},
                "dispatch_strategy": private_sentinel,
                "dispatch_payload_refs": {"payload": private_sentinel},
                "resolved_dispatch_resource_payloads": [{"resource": private_sentinel}],
                "orchestration_recovery": {"prompt": private_sentinel},
                "prompt": private_sentinel,
            },
        )
        patch_chat_rooms_deps[
            "db_service"
        ].get_room_by_room_id.return_value = sample_room
        facade = MagicMock()
        facade.get_timeline_page = AsyncMock(
            return_value=RoomTimelinePage(
                entries=[RoomTimelineEntry(source="user", message=user_message)],
                has_more=False,
                next_position=None,
            )
        )
        runtime = ConversationRuntime()
        runtime.bind_facade(facade)
        _bind_timeline_projector(runtime)
        rooms = _chat_rooms_reading(runtime)

        response = await inquiry_room_messages(
            sample_room.conversation_id,
            user=mock_user,
            store=patch_chat_rooms_deps["db_service"],
            rooms=rooms,
        )

        assert response.success is True
        assert response.message_list is not None
        public_user = response.message_list[0]
        assert public_user.message_type == "user"
        assert public_user.client_request_id == "client-request-top-level-001"
        assert public_user.extend_info == public_extend_info
        assert private_sentinel not in json.dumps(response.model_dump(mode="json"))

    @pytest.mark.asyncio
    async def test_returns_public_agent_message_payload_without_private_dispatch_text(
        self, mock_user, sample_room, sample_user_message, patch_chat_rooms_deps
    ):

        private_sentinel = "PRIVATE_SENTINEL_actual_room_runtime_boundary"
        public_label = "Requesting Insurer"
        client_request_id = "cr-insurer-001"
        patch_chat_rooms_deps[
            "db_service"
        ].get_room_by_room_id.return_value = sample_room
        final_artifact = Artifact(
            artifact_id="artifact-final",
            name="response",
            parts=[Part(root=TextPart(text="Public final result"))],
        )
        remote_task = Task(
            id="remote-task",
            status=TaskStatus(
                state=TaskState.completed,
                message=Message(
                    message_id="private-status",
                    role=MessageRole.USER,
                    parts=[Part(root=TextPart(text=private_sentinel))],
                ),
            ),
            history=[
                Message(
                    message_id="private-history",
                    role=MessageRole.USER,
                    parts=[Part(root=TextPart(text=private_sentinel))],
                ),
                Message(
                    message_id="public-history",
                    role=MessageRole.AGENT,
                    parts=[Part(root=TextPart(text="Public final result"))],
                ),
            ],
            artifacts=[final_artifact],
            metadata={
                "hitl_request_id": private_sentinel,
                "prompt": private_sentinel,
                "hitl_prompt": private_sentinel,
                "choices": [private_sentinel],
                "hitl_choices": [private_sentinel],
            },
        )
        local_task = Task(
            id="local-hitl-task",
            contextId="local-hitl-context",
            status=TaskStatus(state=TaskState.completed),
            artifacts=[final_artifact],
            metadata={
                "hitl_request_id": "local-hitl-request",
                "hitl_prompt": "Choose the approved option",
                "hitl_prompt_type": "choice",
                "hitl_choices": ["Approve", "Reject"],
                "user_answer": "Approve",
            },
        )
        supervisor_task = Task(
            id="local-supervisor-hitl-task",
            status=TaskStatus(state=TaskState.completed),
            artifacts=[final_artifact],
            metadata={"hitl_request_id": "local-supervisor-hitl-request"},
        )
        remote_message = RoomAgentMessage(
            conversation_id=sample_room.conversation_id,
            message_id="agent-msg-remote-spoof",
            agent_id="insurer-agent",
            related_message_id=sample_user_message.message_id,
            message_content=MessageContent(
                message_text=private_sentinel,
                message_task=remote_task,
            ),
            task_content=private_sentinel,
        )
        local_message = RoomAgentMessage(
            conversation_id=sample_room.conversation_id,
            message_id="agent-msg-insurer-001",
            agent_id="insurer-agent",
            related_message_id=sample_user_message.message_id,
            client_request_id=client_request_id,
            message_content=MessageContent(message_task=local_task),
            extend_info={"public_task_label": public_label},
        )
        supervisor_message = RoomAgentMessage(
            conversation_id=sample_room.conversation_id,
            message_id="supervisor-msg-clarify-001",
            agent_id="supervisor",
            related_message_id=sample_user_message.message_id,
            message_content=MessageContent(message_task=supervisor_task),
            extend_info={"public_task_label": "Clarifying request"},
        )
        status_text_task = Task(
            id="status-text-task",
            status=TaskStatus(
                state=TaskState.completed,
                message=Message(
                    message_id="status-text-message",
                    role=MessageRole.AGENT,
                    parts=[Part(root=TextPart(text="Quote approved at GBP 42,000."))],
                ),
            ),
            artifacts=[
                Artifact(
                    artifact_id="quote-data",
                    name="cyber_quote_decision",
                    parts=[Part(root=DataPart(data={"currency": "GBP"}))],
                )
            ],
        )
        status_text_message = RoomAgentMessage(
            conversation_id=sample_room.conversation_id,
            message_id="agent-msg-status-text",
            agent_id="insurer-agent",
            related_message_id=sample_user_message.message_id,
            message_content=MessageContent(
                message_text=private_sentinel,
                message_task=status_text_task,
            ),
        )
        facade = MagicMock()
        facade.get_timeline_page = AsyncMock(
            return_value=RoomTimelinePage(
                entries=[
                    RoomTimelineEntry(source="agent", message=message)
                    for message in (
                        remote_message,
                        local_message,
                        supervisor_message,
                        status_text_message,
                    )
                ],
                has_more=False,
                next_position=None,
            )
        )
        runtime = ConversationRuntime()
        runtime.bind_facade(facade)
        hitl_reader = SimpleNamespace(
            get_hitl_request=AsyncMock(
                side_effect=lambda request_id: (
                    {
                        "request_id": "local-hitl-request",
                        "room_id": sample_room.conversation_id,
                        "public_source": "agent",
                        "application_route": "a2a_resume",
                        "agent_id": "insurer-agent",
                        "display_message_id": local_message.message_id,
                        "continuation_message_id": local_message.message_id,
                        "prompt": "Choose the approved option",
                        "prompt_type": "choice",
                        "choices": ["Approve", "Reject"],
                        "a2a_task_id": "local-hitl-task",
                        "a2a_context_id": "local-hitl-context",
                        "status": "responded",
                        "user_input": "Approve",
                    }
                    if request_id == "local-hitl-request"
                    else (
                        {
                            "request_id": "local-supervisor-hitl-request",
                            "room_id": sample_room.conversation_id,
                            "public_source": "supervisor",
                            "application_route": "supervisor_run",
                            "display_message_id": supervisor_message.message_id,
                            "prompt": "Which market should be prioritized?",
                            "prompt_type": "text",
                            "interaction_id": "supervisor-group-1",
                            "question_count": 2,
                            "question_index": 0,
                            "status": "responded",
                            "user_input": "California",
                        }
                        if request_id == "local-supervisor-hitl-request"
                        else None
                    )
                )
            )
        )
        _bind_timeline_projector(runtime, hitl_reader=hitl_reader)
        rooms = _chat_rooms_reading(runtime)

        response = await inquiry_room_messages(
            sample_room.conversation_id,
            user=mock_user,
            store=patch_chat_rooms_deps["db_service"],
            rooms=rooms,
        )

        assert response.success is True
        assert response.message_list is not None
        by_id = {message.message_id: message for message in response.message_list}
        remote_public = by_id[remote_message.message_id]
        local_public = by_id[local_message.message_id]
        supervisor_public = by_id[supervisor_message.message_id]
        status_text_public = by_id[status_text_message.message_id]
        assert remote_public.message_content.message_text == "Public final result"
        assert remote_public.message_content.message_task.metadata is None
        assert local_public.client_request_id == client_request_id
        assert local_public.message_content.message_task.metadata == {
            "hitl_request_id": "local-hitl-request",
            "hitl_prompt": "Choose the approved option",
            "hitl_prompt_type": "choice",
            "hitl_choices": ["Approve", "Reject"],
            "hitl_a2a_task_id": "local-hitl-task",
            "hitl_a2a_context_id": "local-hitl-context",
            "hitl_question_count": 1,
            "hitl_question_index": 0,
            "user_answer": "Approve",
        }
        assert local_public.extend_info == {
            "public_task_label": public_label,
            "hitl_request_id": "local-hitl-request",
        }
        assert supervisor_public.message_content.message_task.metadata == {
            "hitl_request_id": "local-supervisor-hitl-request",
            "hitl_prompt": "Which market should be prioritized?",
            "hitl_prompt_type": "text",
            "hitl_choices": None,
            "hitl_interaction_id": "supervisor-group-1",
            "hitl_question_count": 2,
            "hitl_question_index": 0,
            "user_answer": "California",
        }
        assert supervisor_public.extend_info == {
            "public_task_label": "Clarifying request",
            "hitl_request_id": "local-supervisor-hitl-request",
        }
        assert (
            status_text_public.message_content.message_text
            == "Quote approved at GBP 42,000."
        )
        assert private_sentinel not in json.dumps(response.model_dump(mode="json"))


class TestSuggestAgents:
    """Tests for suggest_agents endpoint."""

    @pytest.mark.asyncio
    async def test_returns_suggestions_for_valid_message(self, mock_user):
        """Should return agent suggestions for valid message."""

        mock_selection_service = MagicMock()
        mock_selection_service.suggest_agents = AsyncMock(
            return_value=AgentSuggestionResult(
                suggested_agents=[
                    AgentSuggestion(
                        agent_id="agent-1",
                        name="Agent 1",
                        reason="Match",
                        score=0.9,
                    ),
                    AgentSuggestion(
                        agent_id="agent-2",
                        name="Agent 2",
                        reason="Match",
                        score=0.8,
                    ),
                ]
            )
        )

        response = await suggest_agents(
            AgentSuggestionBody(message_text="Help me write some code", top_k=3),
            user=mock_user,
            selection_service=mock_selection_service,
        )

        mock_selection_service.suggest_agents.assert_awaited_once_with(
            message_text="Help me write some code",
            top_k=3,
            user_id=mock_user.user_id,
        )
        assert response["success"] is True
        assert response["suggested_agents"] == [
            {
                "agent_id": "agent-1",
                "name": "Agent 1",
                "reason": "Match",
                "score": 0.9,
            },
            {
                "agent_id": "agent-2",
                "name": "Agent 2",
                "reason": "Match",
                "score": 0.8,
            },
        ]

    @pytest.mark.asyncio
    async def test_returns_error_for_empty_message(self, mock_user):
        """Should return error when message_text is empty."""

        mock_selection_service = MagicMock()
        response = await suggest_agents(
            AgentSuggestionBody(message_text="", top_k=3),
            user=mock_user,
            selection_service=mock_selection_service,
        )

        mock_selection_service.suggest_agents.assert_not_called()
        assert response["success"] is False
        assert response["status_code"] == 400

    @pytest.mark.asyncio
    async def test_handles_service_error(self, mock_user, caplog):
        """Should handle errors from agent selection service."""

        mock_selection_service = MagicMock()
        mock_selection_service.suggest_agents = AsyncMock(
            side_effect=Exception("mongodb://internal-host:27017 refused")
        )

        response = await suggest_agents(
            AgentSuggestionBody(message_text="Test message", top_k=3),
            user=mock_user,
            selection_service=mock_selection_service,
        )

        mock_selection_service.suggest_agents.assert_awaited_once_with(
            message_text="Test message",
            top_k=3,
            user_id=mock_user.user_id,
        )
        assert response["success"] is False
        assert response["status_code"] == 500
        assert response["error"] == "Agent suggestion failed."
        assert "internal-host" not in json.dumps(response)
        [record] = [
            r
            for r in caplog.records
            if r.getMessage() == "agent_suggestion_route_failed"
        ]
        assert record.levelname == "ERROR"

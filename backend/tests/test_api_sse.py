"""
Unit tests for SSE (Server-Sent Events) API endpoints.

Tests cover:
- SSE stream connection
- Room status retrieval
- Message cancellation
- Authorization checks
"""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from fastapi.responses import StreamingResponse

from api_gateway.routes.sse_routes import (
    cancel_message,
    get_room_sse_status,
    stream_room_messages,
)
from common.errors import NotFoundError

# =============================================================================
# SSE Stream Tests
# =============================================================================


class TestStreamRoomMessages:
    """Tests for stream_room_messages endpoint."""

    @pytest.mark.asyncio
    async def test_returns_streaming_response(
        self, mock_user, mock_sse_transport, mock_db_service, sample_room
    ):
        """Should return a StreamingResponse for SSE."""
        mock_connection = MagicMock()
        mock_connection.connection_id = "conn-123"
        mock_connection.is_active = False
        mock_connection.get_message = AsyncMock(return_value=None)

        mock_sse_transport.add_connection.return_value = mock_connection
        mock_db_service.get_room_by_room_id.return_value = sample_room

        response = await stream_room_messages(
            sample_room.conversation_id,
            mock_user,
            transport=mock_sse_transport,
            db=mock_db_service,
        )

        assert isinstance(response, StreamingResponse)
        assert response.media_type == "text/event-stream"

    @pytest.mark.asyncio
    async def test_prevents_intermediaries_from_transforming_the_stream(
        self, mock_user, mock_sse_transport, mock_db_service, sample_room
    ):
        """Compression must not withhold frames until an open stream ends."""
        mock_connection = MagicMock()
        mock_connection.connection_id = "conn-123"
        mock_connection.is_active = False
        mock_connection.get_message = AsyncMock(return_value=None)

        mock_sse_transport.add_connection.return_value = mock_connection
        mock_db_service.get_room_by_room_id.return_value = sample_room

        response = await stream_room_messages(
            sample_room.conversation_id,
            mock_user,
            transport=mock_sse_transport,
            db=mock_db_service,
        )

        directives = {
            directive.strip()
            for directive in response.headers["Cache-Control"].split(",")
        }
        assert {"no-cache", "no-transform"} <= directives
        assert response.headers["X-Accel-Buffering"] == "no"

    @pytest.mark.asyncio
    async def test_raises_403_when_stream_user_does_not_own_room(
        self, mock_user_2, sample_room, patch_sse_deps
    ):
        """Should not open an SSE stream for a room owned by another user."""
        deps = patch_sse_deps
        deps["db_service"].get_room_by_room_id.return_value = sample_room

        with pytest.raises(HTTPException) as exc_info:
            await stream_room_messages(
                conversation_id=sample_room.conversation_id,
                user=mock_user_2,
                transport=deps["sse_transport"],
                db=deps["db_service"],
            )

        assert exc_info.value.status_code == 403
        deps["sse_transport"].add_connection.assert_not_called()

    @pytest.mark.asyncio
    async def test_stream_starts_with_final_connected_frame(
        self, mock_user, mock_sse_transport, mock_db_service, sample_room
    ):
        mock_connection = MagicMock()
        mock_connection.connection_id = "conn-123"
        mock_connection.is_active = False
        mock_connection.get_message = AsyncMock(return_value=None)
        mock_sse_transport.add_connection.return_value = mock_connection
        mock_db_service.get_room_by_room_id.return_value = sample_room

        response = await stream_room_messages(
            sample_room.conversation_id,
            mock_user,
            transport=mock_sse_transport,
            db=mock_db_service,
        )

        first_event = await anext(response.body_iterator)
        frame = json.loads(first_event.removeprefix("data: ").strip())

        assert frame == {
            "type": "connected",
            "conversation_id": sample_room.conversation_id,
            "timestamp": frame["timestamp"],
            "data": {"connection_id": "conn-123"},
        }
        assert isinstance(frame["timestamp"], str)


# =============================================================================
# Room SSE Status Tests
# =============================================================================


class TestGetRoomSseStatus:
    """Tests for get_room_sse_status endpoint."""

    @pytest.mark.asyncio
    async def test_returns_room_status(
        self, mock_user, mock_sse_transport, mock_db_service, sample_room
    ):
        """Should return SSE connection status for room."""
        mock_db_service.get_room_by_room_id.return_value = sample_room
        mock_sse_transport.get_room_status.return_value = {
            "conversation_id": sample_room.conversation_id,
            "connections": 2,
            "active": True,
        }

        result = await get_room_sse_status(
            sample_room.conversation_id,
            mock_user,
            transport=mock_sse_transport,
            db=mock_db_service,
        )

        assert result["connections"] == 2
        mock_sse_transport.get_room_status.assert_called_once_with(
            sample_room.conversation_id
        )

    @pytest.mark.asyncio
    async def test_raises_403_when_status_user_does_not_own_room(
        self, mock_user_2, sample_room, patch_sse_deps
    ):
        """Should not disclose SSE status for a room owned by another user."""
        deps = patch_sse_deps
        deps["db_service"].get_room_by_room_id.return_value = sample_room

        with pytest.raises(HTTPException) as exc_info:
            await get_room_sse_status(
                conversation_id=sample_room.conversation_id,
                user=mock_user_2,
                transport=deps["sse_transport"],
                db=deps["db_service"],
            )

        assert exc_info.value.status_code == 403
        deps["sse_transport"].get_room_status.assert_not_called()


# =============================================================================
# Message Cancellation Tests
# =============================================================================


class TestCancelMessage:
    """Tests for cancel_message endpoint."""

    @pytest.mark.asyncio
    async def test_cancels_message_for_owner(
        self, mock_user, sample_room, sample_user_message, patch_sse_deps
    ):
        """Should cancel message when user owns the room."""
        deps = patch_sse_deps
        deps[
            "db_service"
        ].get_room_user_message_by_message_id.return_value = sample_user_message
        deps["db_service"].get_room_by_room_id.return_value = sample_room

        result = await cancel_message(
            sample_user_message.message_id,
            mock_user,
            db=deps["db_service"],
            engine=deps["execution_engine"],
        )

        assert result["success"] is True
        assert result["message_id"] == sample_user_message.message_id
        assert result["status"] == "canceled"
        assert result["outcome"] == "canceled"
        deps["execution_engine"].cancel.assert_awaited_once_with(
            conversation_id=sample_user_message.conversation_id,
            message_id=sample_user_message.message_id,
            requested_by_user_id=mock_user.user_id,
        )

    @pytest.mark.asyncio
    async def test_terminal_message_cancel_is_idempotent(
        self, mock_user, sample_room, sample_user_message, patch_sse_deps
    ):
        deps = patch_sse_deps
        terminal_message = sample_user_message.model_copy(deep=True)
        terminal_message.extend_info = {"orchestration_status": "budget_exhausted"}
        deps[
            "db_service"
        ].get_room_user_message_by_message_id.return_value = terminal_message
        deps["db_service"].get_room_by_room_id.return_value = sample_room

        result = await cancel_message(
            terminal_message.message_id,
            mock_user,
            db=deps["db_service"],
            engine=deps["execution_engine"],
        )

        assert result == {
            "success": True,
            "message_id": terminal_message.message_id,
            "message": "Message processing had already finished",
            "status": "failed",
            "outcome": "already_terminal",
        }
        deps["execution_engine"].cancel.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_canceled_projection_retries_engine_side_effects(
        self, mock_user, sample_room, sample_user_message, patch_sse_deps
    ):
        deps = patch_sse_deps
        canceled_message = sample_user_message.model_copy(deep=True)
        canceled_message.extend_info = {"orchestration_status": "canceled"}
        deps[
            "db_service"
        ].get_room_user_message_by_message_id.return_value = canceled_message
        deps["db_service"].get_room_by_room_id.return_value = sample_room

        result = await cancel_message(
            canceled_message.message_id,
            mock_user,
            db=deps["db_service"],
            engine=deps["execution_engine"],
        )

        assert result["outcome"] == "canceled"
        deps["execution_engine"].cancel.assert_awaited_once_with(
            conversation_id=canceled_message.conversation_id,
            message_id=canceled_message.message_id,
            requested_by_user_id=mock_user.user_id,
        )

    @pytest.mark.asyncio
    async def test_completion_winner_returns_already_terminal_outcome(
        self, mock_user, sample_room, sample_user_message, patch_sse_deps
    ):
        from common.dto import CancellationAck

        deps = patch_sse_deps
        deps[
            "db_service"
        ].get_room_user_message_by_message_id.return_value = sample_user_message
        deps["db_service"].get_room_by_room_id.return_value = sample_room
        deps["execution_engine"].cancel.return_value = CancellationAck(
            status="completed",
            cancellation_applied=False,
            reconciled=True,
        )

        result = await cancel_message(
            sample_user_message.message_id,
            mock_user,
            db=deps["db_service"],
            engine=deps["execution_engine"],
        )

        assert result["status"] == "completed"
        assert result["outcome"] == "already_terminal"

    @pytest.mark.asyncio
    async def test_persisted_marker_with_pending_finalization_is_accepted(
        self, mock_user, sample_room, sample_user_message, patch_sse_deps
    ):
        from common.dto import CancellationAck

        deps = patch_sse_deps
        deps[
            "db_service"
        ].get_room_user_message_by_message_id.return_value = sample_user_message
        deps["db_service"].get_room_by_room_id.return_value = sample_room
        deps["execution_engine"].cancel.return_value = CancellationAck(
            status="cancellation_pending",
            cancellation_applied=False,
            reconciled=False,
        )

        result = await cancel_message(
            sample_user_message.message_id,
            mock_user,
            db=deps["db_service"],
            engine=deps["execution_engine"],
        )

        assert result == {
            "success": True,
            "message_id": sample_user_message.message_id,
            "message": "Message cancellation accepted and pending reconciliation",
            "status": "cancellation_pending",
            "outcome": "pending_reconciliation",
        }

    @pytest.mark.asyncio
    async def test_raises_404_when_message_not_found(self, mock_user, mock_db_service):
        """Should raise 404 when message doesn't exist."""
        mock_db_service.get_room_user_message_by_message_id.return_value = None

        engine = MagicMock()
        engine.cancel = AsyncMock(return_value=True)

        with pytest.raises(HTTPException) as exc_info:
            await cancel_message(
                "nonexistent-message", mock_user, db=mock_db_service, engine=engine
            )

        assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_raises_404_when_room_not_found(
        self, mock_user, mock_db_service, sample_user_message
    ):
        """Should raise 404 when room doesn't exist."""
        mock_db_service.get_room_user_message_by_message_id.return_value = (
            sample_user_message
        )
        mock_db_service.get_room_by_room_id.return_value = None

        engine = MagicMock()
        engine.cancel = AsyncMock(return_value=True)

        with pytest.raises(HTTPException) as exc_info:
            await cancel_message(
                sample_user_message.message_id,
                mock_user,
                db=mock_db_service,
                engine=engine,
            )

        assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_raises_403_when_not_room_owner(
        self, mock_user_2, mock_db_service, sample_room, sample_user_message
    ):
        """Should raise 403 when user doesn't own the room."""
        mock_db_service.get_room_user_message_by_message_id.return_value = (
            sample_user_message
        )
        mock_db_service.get_room_by_room_id.return_value = sample_room

        engine = MagicMock()
        engine.cancel = AsyncMock(return_value=True)

        with pytest.raises(HTTPException) as exc_info:
            await cancel_message(
                sample_user_message.message_id,
                mock_user_2,
                db=mock_db_service,
                engine=engine,
            )

        assert exc_info.value.status_code == 403

    @pytest.mark.asyncio
    async def test_returns_500_when_execution_cancel_returns_false(
        self, mock_user, sample_room, sample_user_message, patch_sse_deps
    ):
        """Should return 500 when execution cancellation persistence fails."""
        deps = patch_sse_deps
        deps[
            "db_service"
        ].get_room_user_message_by_message_id.return_value = sample_user_message
        deps["db_service"].get_room_by_room_id.return_value = sample_room
        deps["execution_engine"].cancel.return_value = False

        with pytest.raises(HTTPException) as exc_info:
            await cancel_message(
                sample_user_message.message_id,
                mock_user,
                db=deps["db_service"],
                engine=deps["execution_engine"],
            )

        assert exc_info.value.status_code == 500
        assert "Failed to persist cancellation" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_returns_404_when_message_has_no_run_or_pending_processing(
        self, mock_user, sample_room, sample_user_message, patch_sse_deps
    ):
        """A message with no Run and no orchestration task has nothing to cancel."""
        deps = patch_sse_deps
        deps[
            "db_service"
        ].get_room_user_message_by_message_id.return_value = sample_user_message
        deps["db_service"].get_room_by_room_id.return_value = sample_room
        deps["execution_engine"].cancel.side_effect = NotFoundError(
            "Message processing", sample_user_message.message_id
        )

        with pytest.raises(HTTPException) as exc_info:
            await cancel_message(
                sample_user_message.message_id,
                mock_user,
                db=deps["db_service"],
                engine=deps["execution_engine"],
            )

        assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_unexpected_cancel_failure_does_not_leak_error_text(
        self, mock_user, sample_room, sample_user_message, patch_sse_deps
    ):
        deps = patch_sse_deps
        deps[
            "db_service"
        ].get_room_user_message_by_message_id.return_value = sample_user_message
        deps["db_service"].get_room_by_room_id.return_value = sample_room
        deps["execution_engine"].cancel.side_effect = RuntimeError(
            "mongodb://internal-host:27017 timed out"
        )

        with pytest.raises(HTTPException) as exc_info:
            await cancel_message(
                sample_user_message.message_id,
                mock_user,
                db=deps["db_service"],
                engine=deps["execution_engine"],
            )

        assert exc_info.value.status_code == 500
        assert "internal-host" not in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_execution_cancel_receives_audit_user(
        self, mock_user, sample_room, sample_user_message, patch_sse_deps
    ):
        """Should pass room, message, and requesting user to Execution."""
        deps = patch_sse_deps
        deps[
            "db_service"
        ].get_room_user_message_by_message_id.return_value = sample_user_message
        deps["db_service"].get_room_by_room_id.return_value = sample_room

        await cancel_message(
            sample_user_message.message_id,
            mock_user,
            db=deps["db_service"],
            engine=deps["execution_engine"],
        )

        deps["execution_engine"].cancel.assert_awaited_once_with(
            conversation_id=sample_user_message.conversation_id,
            message_id=sample_user_message.message_id,
            requested_by_user_id=mock_user.user_id,
        )

    @pytest.mark.asyncio
    async def test_route_does_not_call_sse_cancel_directly(
        self, mock_user, sample_room, sample_user_message, patch_sse_deps
    ):
        """SSE route delegates cancellation internals to Execution."""
        deps = patch_sse_deps
        deps[
            "db_service"
        ].get_room_user_message_by_message_id.return_value = sample_user_message
        deps["db_service"].get_room_by_room_id.return_value = sample_room

        await cancel_message(
            sample_user_message.message_id,
            mock_user,
            db=deps["db_service"],
            engine=deps["execution_engine"],
        )

        deps["sse_transport"].cancel_message_and_broadcast.assert_not_called()

    @pytest.mark.asyncio
    async def test_paused_agent_cleanup_failure_does_not_block_root_cancellation(
        self,
        mock_user,
        sample_room,
        sample_user_message,
        sample_agent_message_with_task,
        patch_sse_deps,
    ):
        """Paused-agent cleanup is best-effort after root cancellation is cleared."""
        deps = patch_sse_deps
        deps[
            "db_service"
        ].get_room_user_message_by_message_id.return_value = sample_user_message
        deps["db_service"].get_room_by_room_id.return_value = sample_room
        deps[
            "db_service"
        ].get_room_agent_messages_by_related_message_id.return_value = [
            sample_agent_message_with_task
        ]
        deps["execution_engine"].cancel.return_value = True

        result = await cancel_message(
            sample_user_message.message_id,
            mock_user,
            db=deps["db_service"],
            engine=deps["execution_engine"],
        )

        assert result["success"] is True
        deps["execution_engine"].cancel.assert_awaited_once_with(
            conversation_id=sample_user_message.conversation_id,
            message_id=sample_user_message.message_id,
            requested_by_user_id=mock_user.user_id,
        )

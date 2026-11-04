from common.observability import events as E
from common.utils.logger import get_logger
from conversation.timeline import (
    TimelineCursorError,
    decode_timeline_cursor,
    encode_timeline_cursor,
)
from conversation.timeline_projection import RoomTimelineProjector
from models.request import (
    RoomCenterRoomMessageRequest,
)
from models.response import (
    RoomCenterRoomMessageResponse,
)

logger = get_logger(__name__)


class RoomMessageReader:
    """Read one page of a room's public timeline."""

    def __init__(self, *, facade, timeline_projector: RoomTimelineProjector | None):
        self._facade = facade
        self._timeline_projector = timeline_projector

    def _require_facade(self):
        if self._facade is None:
            raise RuntimeError(
                "ConversationRuntime.bind_facade() not called - startup incomplete"
            )
        return self._facade

    def _require_timeline_projector(self) -> RoomTimelineProjector:
        if self._timeline_projector is None:
            raise RuntimeError(
                "ConversationRuntime timeline projector has not been bound"
            )
        return self._timeline_projector

    async def inquiry_room_messages_by_room_id(
        self, request: RoomCenterRoomMessageRequest
    ) -> RoomCenterRoomMessageResponse:
        """
        Retrieve all messages in a room, including user messages and agent messages.
        For user messages: return message_text from message_content
        For agent messages: return text from sanitized completed artifacts.
        Sort by creation time and return
        """
        if request.conversation_id is None:
            return RoomCenterRoomMessageResponse(
                message_list=None,
                success=False,
                error="Room id is required",
                status_code=400,
            )

        try:
            conversation_id = request.conversation_id

            limit = request.limit if request.limit is not None else 200
            if (
                isinstance(limit, bool)
                or not isinstance(limit, int)
                or not 1 <= limit <= 200
            ):
                return RoomCenterRoomMessageResponse(
                    conversation_id=conversation_id,
                    message_list=None,
                    success=False,
                    error="limit must be an integer between 1 and 200",
                    status_code=400,
                )
            try:
                before = (
                    decode_timeline_cursor(
                        request.cursor, conversation_id=conversation_id
                    )
                    if request.cursor is not None
                    else None
                )
            except TimelineCursorError:
                return RoomCenterRoomMessageResponse(
                    conversation_id=conversation_id,
                    message_list=None,
                    success=False,
                    error="Invalid timeline cursor",
                    status_code=400,
                )

            page = await self._require_facade().get_timeline_page(
                conversation_id,
                limit=limit,
                before=before,
            )
            combined_messages = await self._require_timeline_projector().project(page)
            next_cursor = (
                encode_timeline_cursor(conversation_id, page.next_position)
                if page.has_more and page.next_position is not None
                else None
            )
            return RoomCenterRoomMessageResponse(
                conversation_id=conversation_id,
                message_list=combined_messages,
                has_more=page.has_more,
                next_cursor=next_cursor,
                success=True,
                error=None,
                status_code=200,
            )

        except Exception as exc:
            logger.emit(
                E.ROOM_TIMELINE_READ_FAILED,
                exc=exc,
                conversation_id=request.conversation_id,
            )
            return RoomCenterRoomMessageResponse(
                conversation_id=request.conversation_id,
                message_list=None,
                success=False,
                error="Failed to retrieve room timeline",
                status_code=500,
            )


__all__ = ["RoomMessageReader"]

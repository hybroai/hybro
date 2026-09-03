from __future__ import annotations

from typing import Protocol, runtime_checkable

from models.request import (
    RoomCenterRoomMessageRequest,
    RoomCenterRoomSettingRequest,
)
from models.response import (
    RoomCenterActiveRunsResponse,
    RoomCenterRoomMessageResponse,
    RoomCenterRoomSettingResponse,
)
from models.room import Room, RoomAgentMessage, RoomUserMessage


@runtime_checkable
class A2ATaskReaderCompatibility(Protocol):
    async def get_pending_task_messages_for_user(
        self, user_id: str, states: list[str]
    ) -> list[RoomAgentMessage]: ...
    async def get_room_agent_message_by_message_id(
        self, message_id: str
    ) -> RoomAgentMessage | None: ...
    async def get_room_by_room_id(self, room_id: str) -> Room | None: ...
    async def get_room_user_message_by_message_id(
        self, message_id: str
    ) -> RoomUserMessage | None: ...
    async def get_task_messages_for_room(
        self, room_id: str, *, limit: int = 50
    ) -> list[RoomAgentMessage]: ...


@runtime_checkable
class RoomCenterCompatibility(Protocol):
    async def create_new_room(
        self, request: RoomCenterRoomSettingRequest
    ) -> RoomCenterRoomSettingResponse: ...
    async def inquiry_rooms_by_room_owner_id(
        self, request: RoomCenterRoomSettingRequest
    ) -> RoomCenterRoomSettingResponse: ...
    async def inquiry_room_history_by_owner_id(
        self, request: RoomCenterRoomSettingRequest
    ) -> RoomCenterRoomSettingResponse: ...
    async def inquiry_room_messages_by_room_id(
        self, request: RoomCenterRoomMessageRequest
    ) -> RoomCenterRoomMessageResponse: ...
    async def inquiry_room_setting(
        self, request: RoomCenterRoomSettingRequest
    ) -> RoomCenterRoomSettingResponse: ...
    async def inquiry_active_runs(
        self, request: RoomCenterRoomSettingRequest
    ) -> RoomCenterActiveRunsResponse: ...
    async def delete_room_by_room_id(
        self, request: RoomCenterRoomSettingRequest
    ) -> RoomCenterRoomSettingResponse: ...
    async def update_room_agent_set(
        self, request: RoomCenterRoomSettingRequest
    ) -> RoomCenterRoomSettingResponse: ...
    async def update_room_name(
        self, request: RoomCenterRoomSettingRequest
    ) -> RoomCenterRoomSettingResponse: ...
    async def update_room_history_fields(
        self, request: RoomCenterRoomSettingRequest
    ) -> RoomCenterRoomSettingResponse: ...
    async def update_room_default_mode(
        self,
        room_id: str,
        *,
        use_supervisor: bool,
    ) -> bool: ...


__all__ = ["A2ATaskReaderCompatibility", "RoomCenterCompatibility"]

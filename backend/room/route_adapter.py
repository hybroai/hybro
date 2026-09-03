from __future__ import annotations

from models.request import (
    RoomCenterAgentMessageRequest,
    RoomCenterRoomMessageRequest,
    RoomCenterRoomSettingRequest,
    RoomCenterUserMessageRequest,
)
from models.response import (
    RoomCenterActiveRunsResponse,
    RoomCenterAgentMessageResponse,
    RoomCenterRoomMessageResponse,
    RoomCenterRoomSettingResponse,
    RoomCenterUserMessageResponse,
)


class RoomRouteAdapter:
    def __init__(
        self,
        bound_room_runtime=None,
        room_services=None,
        bound_room_services=None,
    ):
        self.room_runtime = bound_room_runtime
        if self.room_runtime is None:
            self.room_runtime = room_services
        if self.room_runtime is None:
            self.room_runtime = bound_room_services

    def bind_facade(self, facade) -> None:
        from room.compat.runtime import room_runtime

        room_runtime.bind_facade(facade)
        self.room_runtime = room_runtime

    def bind_room_runtime(self, bound_room_runtime) -> None:
        self.room_runtime = bound_room_runtime

    bind_room_services = bind_room_runtime

    def _require_room_services(self):
        if self.room_runtime is None or not getattr(self.room_runtime, "_bound", False):
            raise RuntimeError(
                "RoomRouteAdapter.bind_facade() not called - startup incomplete"
            )
        return self.room_runtime

    async def create_new_room(
        self, request: RoomCenterRoomSettingRequest
    ) -> RoomCenterRoomSettingResponse:
        return await self._require_room_services().create_new_room(request)

    async def inquiry_room_setting(
        self, request: RoomCenterRoomSettingRequest
    ) -> RoomCenterRoomSettingResponse:
        return await self._require_room_services().inquiry_room_setting(request)

    async def inquiry_active_runs(
        self, request: RoomCenterRoomSettingRequest
    ) -> RoomCenterActiveRunsResponse:
        return await self._require_room_services().inquiry_active_runs(request)

    async def delete_room_by_room_id(
        self, request: RoomCenterRoomSettingRequest
    ) -> RoomCenterRoomSettingResponse:
        return await self._require_room_services().delete_room_by_room_id(request)

    async def inquiry_rooms_by_room_owner_id(
        self, request: RoomCenterRoomSettingRequest
    ) -> RoomCenterRoomSettingResponse:
        return await self._require_room_services().inquiry_rooms_by_room_owner_id(
            request
        )

    async def inquiry_room_history_by_owner_id(
        self, request: RoomCenterRoomSettingRequest
    ) -> RoomCenterRoomSettingResponse:
        return await self._require_room_services().inquiry_room_history_by_owner_id(
            request
        )

    async def update_room_agent_set(
        self, request: RoomCenterRoomSettingRequest
    ) -> RoomCenterRoomSettingResponse:
        return await self._require_room_services().update_room_agent_set(request)

    async def update_room_name(
        self, request: RoomCenterRoomSettingRequest
    ) -> RoomCenterRoomSettingResponse:
        return await self._require_room_services().update_room_name(request)

    async def update_room_history_fields(
        self, request: RoomCenterRoomSettingRequest
    ) -> RoomCenterRoomSettingResponse:
        return await self._require_room_services().update_room_history_fields(request)

    async def update_room_default_mode(
        self,
        room_id: str,
        *,
        use_supervisor: bool,
    ) -> bool:
        return await self._require_room_services().update_room_default_mode(
            room_id,
            use_supervisor=use_supervisor,
        )

    async def inquiry_room_messages_by_room_id(
        self, request: RoomCenterRoomMessageRequest
    ) -> RoomCenterRoomMessageResponse:
        return await self._require_room_services().inquiry_room_messages_by_room_id(
            request
        )

    async def inquiry_agent_messages_by_related_message_id(
        self, request: RoomCenterAgentMessageRequest
    ) -> RoomCenterAgentMessageResponse:
        return await self._require_room_services().inquiry_agent_messages_by_related_message_id(
            request
        )

    async def get_idempotent_user_message(
        self,
        *,
        room_id: str,
        client_request_id: str,
        idempotency_fingerprint: str,
        idempotency_fingerprint_version: int,
    ) -> RoomCenterUserMessageResponse | None:
        return await self._require_room_services().get_idempotent_user_message(
            room_id=room_id,
            client_request_id=client_request_id,
            idempotency_fingerprint=idempotency_fingerprint,
            idempotency_fingerprint_version=idempotency_fingerprint_version,
        )

    async def send_message_to_room(
        self,
        request: RoomCenterUserMessageRequest,
        target_group: str = "room_team",
        mentioned_agent_ids: list[str] | None = None,
        *,
        idempotency_fingerprint: str | None = None,
        idempotency_fingerprint_version: int | None = None,
    ) -> RoomCenterUserMessageResponse:
        return await self._require_room_services().send_message_to_room(
            request,
            target_group,
            mentioned_agent_ids,
            idempotency_fingerprint=idempotency_fingerprint,
            idempotency_fingerprint_version=idempotency_fingerprint_version,
        )

    async def persist_message_to_room(
        self,
        request: RoomCenterUserMessageRequest,
        target_group: str = "room_team",
        mentioned_agent_ids: list[str] | None = None,
        *,
        idempotency_fingerprint: str | None = None,
        idempotency_fingerprint_version: int | None = None,
    ):
        return await self._require_room_services().persist_message_to_room(
            request,
            target_group,
            mentioned_agent_ids,
            idempotency_fingerprint=idempotency_fingerprint,
            idempotency_fingerprint_version=idempotency_fingerprint_version,
        )

    async def run_message_preflight_to_room(self, context):
        return await self._require_room_services().run_message_preflight_to_room(
            context
        )

    def discard_message_preflight(self, context) -> None:
        self._require_room_services().discard_message_preflight(context)

    async def update_user_message_orchestration_status(
        self,
        message_id: str,
        status: str,
    ) -> bool:
        return bool(
            await self._require_room_services().update_user_message_orchestration_status(
                message_id,
                status,
            )
        )


__all__ = ["RoomRouteAdapter"]

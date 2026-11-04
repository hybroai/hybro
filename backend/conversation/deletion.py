from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from common.observability import events as E
from common.utils.logger import get_logger
from common.utils.time import utcnow
from models.response import RoomCenterRoomSettingResponse


@runtime_checkable
class RoomMemoryCleanupPort(Protocol):
    async def delete_room_memory(self, conversation_id: str) -> bool: ...


logger = get_logger(__name__)


class RoomDeletionLifecycle(Protocol):
    async def get_room_owner(self, conversation_id: str) -> str | None: ...

    async def cleanup_room_owned_data(self, conversation_id: str) -> Any: ...

    async def delete_room(self, conversation_id: str, owner_id: str) -> bool: ...


class RoomFileDeletionLifecycle(Protocol):
    async def begin_room_deletion(
        self, conversation_id: str, owner_id: str
    ) -> str | None: ...

    async def wait_for_room_writes(self, conversation_id: str) -> bool: ...

    async def set_deletion_phase(
        self, conversation_id: str, deletion_id: str, phase: str
    ) -> bool: ...

    async def delete_for_room(
        self, conversation_id: str, *, deletion_id: str | None = None
    ) -> int: ...

    async def delete_room_state(self, conversation_id: str) -> bool: ...


class RoomEpochDeactivationPort(Protocol):
    async def read(self, conversation_id: str) -> Any: ...

    async def deactivate(
        self,
        conversation_id: str,
        epoch: int,
        deletion_id: str,
        *,
        deactivated_at: datetime,
    ) -> tuple[str, Any | None]: ...


class OrchestratorEpochCleanupPort(Protocol):
    async def delete_by_epoch(self, conversation_id: str, room_epoch: int) -> int: ...


class RoomDeletionService:
    """Coordinates the existing room deletion lifecycle through narrow ports."""

    def __init__(
        self,
        *,
        room_lifecycle: RoomDeletionLifecycle,
        memory_cleanup: RoomMemoryCleanupPort | None,
        file_lifecycle: RoomFileDeletionLifecycle,
        epoch_store: RoomEpochDeactivationPort | None = None,
        orchestrator_epoch_cleanup: OrchestratorEpochCleanupPort | None = None,
    ) -> None:
        self._room_lifecycle = room_lifecycle
        self._memory_cleanup = memory_cleanup
        self._file_lifecycle = file_lifecycle
        self._epoch_store = epoch_store
        self._orchestrator_epoch_cleanup = orchestrator_epoch_cleanup

    async def delete_room_by_room_id(
        self, conversation_id: str, *, requesting_user_id: str
    ) -> RoomCenterRoomSettingResponse:
        actual_owner_id = await self._room_lifecycle.get_room_owner(conversation_id)
        if actual_owner_id is None:
            return RoomCenterRoomSettingResponse(
                conversation_id=conversation_id,
                room=None,
                success=False,
                error="Room not found",
                status_code=404,
            )
        if requesting_user_id != actual_owner_id:
            return RoomCenterRoomSettingResponse(
                conversation_id=conversation_id,
                room=None,
                success=False,
                error="Forbidden",
                status_code=403,
            )

        return await self._delete_room_with_file_lifecycle(
            conversation_id, actual_owner_id
        )

    async def _delete_room_with_file_lifecycle(
        self, conversation_id: str, actual_owner_id: str
    ) -> RoomCenterRoomSettingResponse:
        deletion_id = await self._file_lifecycle.begin_room_deletion(
            conversation_id, actual_owner_id
        )
        if deletion_id is None:
            return RoomCenterRoomSettingResponse(
                conversation_id=conversation_id,
                room=None,
                success=False,
                error="Room deletion could not be started",
                status_code=409,
            )
        if not await self._file_lifecycle.wait_for_room_writes(conversation_id):
            return RoomCenterRoomSettingResponse(
                conversation_id=conversation_id,
                room=None,
                success=False,
                error="Room still has active writes",
                status_code=409,
            )

        # Epoch deactivation is the primary fence for the exact incarnation.
        # Non-terminal calls are not cancelled here; the A2A recovery cycle
        # expires them via `room_epoch_gone`.
        deactivation_ok, epoch = await self._deactivate_room_epoch(
            conversation_id, deletion_id
        )
        if not deactivation_ok:
            return RoomCenterRoomSettingResponse(
                conversation_id=conversation_id,
                room=None,
                success=False,
                error="Room epoch deactivation failed",
                status_code=409,
            )

        await self._file_lifecycle.set_deletion_phase(
            conversation_id, deletion_id, "cleaning"
        )
        cleanup_ok = await self._cleanup_context_memory_for_room(conversation_id)
        owned_cleanup_ok = await self._delete_room_owned_data(
            conversation_id, deletion_id=deletion_id, epoch=epoch
        )
        if not cleanup_ok or not owned_cleanup_ok:
            return RoomCenterRoomSettingResponse(
                conversation_id=conversation_id,
                room=None,
                success=False,
                error="Room cleanup is incomplete and will be retried",
                status_code=500,
            )

        await self._file_lifecycle.set_deletion_phase(
            conversation_id, deletion_id, "finalizing"
        )
        success = await self._room_lifecycle.delete_room(
            conversation_id, actual_owner_id
        )
        if not success:
            return RoomCenterRoomSettingResponse(
                conversation_id=conversation_id,
                room=None,
                success=False,
                error="Failed to finalize room deletion",
                status_code=500,
            )
        return RoomCenterRoomSettingResponse(
            conversation_id=conversation_id,
            room=None,
            success=True,
            error=None,
            status_code=200,
        )

    async def _deactivate_room_epoch(
        self, conversation_id: str, deletion_id: str
    ) -> tuple[bool, int | None]:
        if self._epoch_store is None:
            return True, None
        try:
            record = await self._epoch_store.read(conversation_id)
        except Exception as exc:
            logger.emit(
                E.ROOM_DELETION_STEP_FAILED,
                exc=exc,
                conversation_id=conversation_id,
                deletion_id=deletion_id,
                deletion_step="epoch_read",
            )
            return False, None
        if record is None:
            return True, None
        epoch = int(record.epoch)
        try:
            outcome, _ = await self._epoch_store.deactivate(
                conversation_id, epoch, deletion_id, deactivated_at=utcnow()
            )
        except Exception as exc:
            logger.emit(
                E.ROOM_DELETION_STEP_FAILED,
                exc=exc,
                conversation_id=conversation_id,
                deletion_id=deletion_id,
                deletion_step="epoch_deactivation",
                room_epoch=epoch,
            )
            return False, epoch
        if outcome == "conflict":
            logger.emit(
                E.ROOM_EPOCH_DEACTIVATION_CONFLICT_DETECTED,
                conversation_id=conversation_id,
                deletion_id=deletion_id,
                room_epoch=epoch,
            )
            return False, epoch
        return True, epoch

    async def _cleanup_context_memory_for_room(self, conversation_id: str) -> bool:
        if self._memory_cleanup is None:
            logger.emit(
                E.ROOM_DELETION_STEP_FAILED,
                conversation_id=conversation_id,
                deletion_step="context_memory_cleanup",
                reason="cleanup_unbound",
            )
            return False
        try:
            ok = await self._memory_cleanup.delete_room_memory(conversation_id)
            if not ok:
                logger.emit(
                    E.ROOM_DELETION_STEP_FAILED,
                    conversation_id=conversation_id,
                    deletion_step="context_memory_cleanup",
                    reason="cleanup_reported_failure",
                )
            return bool(ok)
        except Exception as exc:
            logger.emit(
                E.ROOM_DELETION_STEP_FAILED,
                exc=exc,
                conversation_id=conversation_id,
                deletion_step="context_memory_cleanup",
            )
            return False

    async def _delete_room_owned_data(
        self, conversation_id: str, *, deletion_id: str, epoch: int | None
    ) -> bool:
        file_lifecycle = self._file_lifecycle
        ok = True
        try:
            await file_lifecycle.delete_for_room(
                conversation_id, deletion_id=deletion_id
            )
            if not await file_lifecycle.delete_room_state(conversation_id):
                ok = False
        except Exception as exc:
            ok = False
            logger.emit(
                E.ROOM_DELETION_STEP_FAILED,
                exc=exc,
                conversation_id=conversation_id,
                deletion_id=deletion_id,
                deletion_step="file_cleanup",
            )

        try:
            if epoch is not None and self._orchestrator_epoch_cleanup is not None:
                await self._orchestrator_epoch_cleanup.delete_by_epoch(
                    conversation_id, epoch
                )
        except Exception as exc:
            ok = False
            logger.emit(
                E.ROOM_DELETION_STEP_FAILED,
                exc=exc,
                conversation_id=conversation_id,
                deletion_id=deletion_id,
                deletion_step="orchestrator_epoch_cleanup",
                room_epoch=epoch,
            )

        try:
            await self._room_lifecycle.cleanup_room_owned_data(conversation_id)
        except Exception as exc:
            ok = False
            logger.emit(
                E.ROOM_DELETION_STEP_FAILED,
                exc=exc,
                conversation_id=conversation_id,
                deletion_id=deletion_id,
                deletion_step="owned_data_cleanup",
            )
        return ok

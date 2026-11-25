from typing import Protocol, runtime_checkable

from common.dto import (
    CancellationAck,
    ExecutionAck,
    ExecutionRequest,
    HITLRequest,
    HITLResponse,
    RunInfo,
)
from common.dto.execution import OwnerCancelCause


@runtime_checkable
class ExecutionEngine(Protocol):
    async def execute(self, request: ExecutionRequest) -> ExecutionAck: ...
    async def start_orchestration(
        self,
        request: ExecutionRequest,
        ack: ExecutionAck,
    ) -> None: ...
    def schedule_orchestration(
        self,
        request: ExecutionRequest,
        ack: ExecutionAck,
    ) -> None: ...
    def is_orchestration_pending(self, message_id: str) -> bool: ...
    async def cancel(
        self,
        conversation_id: str,
        message_id: str,
        *,
        requested_by_user_id: str,
        cause: OwnerCancelCause = "user_requested",
    ) -> bool | CancellationAck: ...
    async def get_runs_for_room(self, conversation_id: str) -> list[RunInfo]: ...
    async def get_latest_runs_for_rooms(
        self, room_ids: list[str]
    ) -> dict[str, RunInfo]: ...
    async def cancel_inflight_tasks(self) -> int: ...


@runtime_checkable
class HITLManager(Protocol):
    async def resolve_hitl_batch(
        self,
        conversation_id: str,
        interaction_id: str,
        answers: list[dict[str, str]],
        responder_id: str,
        client_request_id: str | None = None,
        answered_by_client_id: str | None = None,
    ) -> HITLResponse: ...

    async def get_pending_hitl(self, conversation_id: str) -> list[HITLRequest]: ...
    async def cancel_hitl_interaction(
        self,
        conversation_id: str,
        interaction_id: str,
        expected_version: int,
        *,
        cause: OwnerCancelCause = "user_requested",
    ) -> int: ...


@runtime_checkable
class RoomDistributedLock(Protocol):
    async def acquire(
        self, conversation_id: str, owner: str, ttl: int
    ) -> bool | None: ...
    async def renew(
        self, conversation_id: str, owner: str, ttl: int
    ) -> bool | None: ...
    async def release(self, conversation_id: str, owner: str) -> None: ...


__all__ = [
    "ExecutionEngine",
    "HITLManager",
    "RoomDistributedLock",
]

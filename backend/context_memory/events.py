from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from common.dto import CompactionResult, MessageCommitted
from common.observability import events as E
from common.utils.logger import get_logger

logger = get_logger(__name__)


@runtime_checkable
class ProjectionPort(Protocol):
    async def project_message_for_event(
        self,
        conversation_id: str,
        message_id: str,
        *,
        room_agent_set: dict[str, str] | None = None,
        agent_name: str | None = None,
        was_successful: bool | None = None,
    ) -> dict[str, Any]: ...

    async def run_compaction(self, conversation_id: str) -> CompactionResult: ...


class ContextMemoryEventHandler:
    def __init__(self, projection: ProjectionPort) -> None:
        self._projection = projection

    async def handle_message_committed(self, event: MessageCommitted) -> None:
        try:
            status = await self._projection.project_message_for_event(
                event.conversation_id,
                event.message_id,
                room_agent_set=event.room_agent_set,
                agent_name=event.agent_name,
                was_successful=event.was_successful,
            )
            if status.get("projected"):
                await self._projection.run_compaction(event.conversation_id)
        except Exception as exc:
            logger.emit(
                E.CONTEXT_MEMORY_PROJECTION_FAILED,
                exc=exc,
                conversation_id=event.conversation_id,
                message_id=event.message_id,
            )
            raise

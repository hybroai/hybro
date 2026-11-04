from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

from common.dto import CompactionResult
from common.observability import NoopTracingProvider
from common.observability import events as E
from common.protocols import RoomHistoryReader
from common.utils.logger import get_logger
from context_memory import compaction, projection
from context_memory.config import CompactionConfig
from context_memory.ports import ContentStorageRepository, MemoryRepository

logger = get_logger(__name__)


class ContextMemoryFacade:
    def __init__(
        self,
        *,
        memory_repository: MemoryRepository,
        content_repository: ContentStorageRepository,
        room_history_reader: RoomHistoryReader,
        id_factory: Callable[[], str],
        now: Callable[[], datetime],
        compaction_config: CompactionConfig | None = None,
        tracer: Any | None = None,
    ) -> None:
        self.memory_repository = memory_repository
        self.content_repository = content_repository
        self.room_history_reader = room_history_reader
        self.id_factory = id_factory
        self.now = now
        self.compaction_config = compaction_config or CompactionConfig()
        self.tracer = tracer or NoopTracingProvider()

    async def delete_room_memory(self, conversation_id: str) -> bool:
        existed = await self.memory_repository.get_room_memory(conversation_id)
        if existed:
            memory_deleted = await self.memory_repository.delete_room_memory(
                conversation_id
            )
            if not memory_deleted:
                return False
        cleanup_ok = True
        try:
            await self.content_repository.delete_content_by_room_id(conversation_id)
        except Exception as exc:
            logger.emit(
                E.CONTEXT_MEMORY_CONTENT_CLEANUP_FAILED,
                exc=exc,
                conversation_id=conversation_id,
            )
            cleanup_ok = False
        return cleanup_ok

    async def project_message_for_event(
        self,
        conversation_id: str,
        message_id: str,
        *,
        room_agent_set: dict[str, str] | None = None,
        agent_name: str | None = None,
        was_successful: bool | None = None,
    ) -> dict:
        return await projection.project_message_from_history(
            conversation_id=conversation_id,
            message_id=message_id,
            repository=self.memory_repository,
            room_history_reader=self.room_history_reader,
            id_factory=self.id_factory,
            now=self.now,
            room_agent_set=room_agent_set,
            agent_name=agent_name,
            was_successful=was_successful,
        )

    async def run_compaction(self, conversation_id: str) -> CompactionResult:
        return await compaction.run_compaction(
            repository=self.memory_repository,
            content_repository=self.content_repository,
            conversation_id=conversation_id,
            config=self.compaction_config,
            now=self.now,
        )

    async def compact_if_needed(self, conversation_id: str):
        return await compaction.compact_if_needed(
            repository=self.memory_repository,
            content_repository=self.content_repository,
            conversation_id=conversation_id,
            config=self.compaction_config,
            now=self.now,
        )

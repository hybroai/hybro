"""Non-destructive context compaction helpers."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from hashlib import sha256

from execution.orchestrator.models import (
    CompactionResult,
    ModelMessage,
    ModelStreamEvent,
    ModelTurnRequest,
    ResolvedModelSnapshot,
    UsageRecord,
)
from execution.orchestrator.ports import CancellationSignal, ModelRuntime

from .streaming import ModelStreamAssembler


class CompactionError(RuntimeError):
    pass


class DeterministicFakeCompactor:
    async def compact(
        self,
        messages: list[object],
        *,
        turn_id: str,
        remaining_provider_retries: int,
        deadline_at: datetime,
        on_event: Callable[[ModelStreamEvent], Awaitable[None]],
        signal: CancellationSignal,
    ) -> CompactionResult:
        del turn_id, remaining_provider_retries, deadline_at, on_event
        if signal.cancelled:
            raise CompactionError("compaction canceled")
        return CompactionResult(
            summary=f"Compacted {len(messages)} prior model messages."
        )


class ModelBackedCompactor:
    def __init__(
        self,
        runtime: ModelRuntime,
        *,
        model: ResolvedModelSnapshot,
    ) -> None:
        self.runtime = runtime
        self.model = model

    async def compact(
        self,
        messages: list[object],
        *,
        turn_id: str,
        remaining_provider_retries: int,
        deadline_at: datetime,
        on_event: Callable[[ModelStreamEvent], Awaitable[None]],
        signal: CancellationSignal,
    ) -> CompactionResult:
        typed = [message for message in messages if isinstance(message, ModelMessage)]
        turn_material = "|".join(message.model_dump_json() for message in typed)
        content_digest = sha256(turn_material.encode()).hexdigest()
        request = ModelTurnRequest(
            turn_id=f"{turn_id}:{content_digest}",
            model=self.model,
            system_prompt=(
                "Summarize completed prior turns faithfully and compactly. "
                "Do not invent facts or instructions."
            ),
            messages=typed,
            tools=[],
            tool_choice="none",
            purpose="compaction",
            remaining_provider_retries=remaining_provider_retries,
            absolute_deadline_at=deadline_at,
        )
        assembler = ModelStreamAssembler()
        attempts = 0
        async for event in self.runtime.stream_turn(request, signal=signal):
            assembler.accept(event)
            await on_event(event)
            if event.kind == "attempt_started":
                attempts = max(attempts, event.attempt or 0)
        outcome = assembler.build_outcome(
            message_id="compaction",
            created_at=datetime.now(UTC),
        )
        if outcome.assistant is None:
            raise CompactionError(f"compaction ended with {outcome.kind}")
        text = "".join(
            part.text for part in outcome.assistant.content if part.kind == "text"
        ).strip()
        if not text:
            raise CompactionError("compaction returned empty summary")
        usages = list(assembler.usage_by_attempt.values())
        usage = UsageRecord(
            input_tokens=sum(item.input_tokens for item in usages),
            output_tokens=sum(item.output_tokens for item in usages),
            cache_read_tokens=sum(item.cache_read_tokens for item in usages),
            cache_write_tokens=sum(item.cache_write_tokens for item in usages),
        )
        return CompactionResult(
            summary=text,
            provider_attempts=attempts,
            usage=usage,
        )


__all__ = [
    "CompactionError",
    "DeterministicFakeCompactor",
    "ModelBackedCompactor",
]

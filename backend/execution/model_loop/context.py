"""Bounded non-destructive model-context compilation."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal

from execution.orchestrator.models import (
    AssistantMessage,
    ModelMessage,
    OrchestratorRunState,
    ToolDefinition,
    ToolResultMessage,
    UserMessage,
)
from execution.orchestrator.transcript import (
    agent_messages_to_model,
    unresolved_call_ids,
)


class UnresolvedToolBatchError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class CompiledContext:
    kind: Literal["ready", "needs_compaction", "context_unfit"]
    messages: list[ModelMessage]
    estimated_input_tokens: int
    reserved_output_tokens: int
    retained_transcript_indexes: tuple[int, ...]
    compacted: bool = False


class DeterministicTokenEstimator:
    """Conservative estimator for provider framing plus serialized content."""

    def estimate_text(self, text: str) -> int:
        return max(1, (len(text.encode("utf-8")) + 2) // 3)

    def estimate_messages(self, messages: list[ModelMessage]) -> int:
        payload = [message.model_dump(mode="json") for message in messages]
        return self.estimate_text(json.dumps(payload, separators=(",", ":"))) + 4 * len(
            messages
        )

    def estimate_tools(self, tools: list[ToolDefinition]) -> int:
        payload = [tool.model_dump(mode="json") for tool in tools]
        return self.estimate_text(json.dumps(payload, separators=(",", ":")))


class ContextCompiler:
    def __init__(self, estimator: DeterministicTokenEstimator | None = None) -> None:
        self.estimator = estimator or DeterministicTokenEstimator()

    def compile(
        self,
        run: OrchestratorRunState,
        *,
        tools: list[ToolDefinition],
        background: list[ModelMessage] | None = None,
        summary: str | None = None,
    ) -> CompiledContext:
        if unresolved_call_ids(run.transcript):
            raise UnresolvedToolBatchError(
                "cannot compile a model turn while tool calls are unresolved"
            )
        background = background or []
        reserve = run.profile.model.max_output_tokens
        mandatory = (
            self.estimator.estimate_text(run.profile.prompt.rendered_system_prompt)
            + self.estimator.estimate_tools(tools)
            + self.estimator.estimate_messages(background)
            + reserve
        )
        if mandatory > run.profile.model.context_window:
            return CompiledContext(
                kind="context_unfit",
                messages=[],
                estimated_input_tokens=mandatory - reserve,
                reserved_output_tokens=reserve,
                retained_transcript_indexes=(),
            )

        converted = agent_messages_to_model(
            run.transcript, prepare_orchestration_context=True
        )
        full = [*background, *converted]
        estimate = mandatory - reserve + self.estimator.estimate_messages(converted)
        if summary is None:
            if estimate + reserve <= run.profile.model.context_window:
                return CompiledContext(
                    kind="ready",
                    messages=full,
                    estimated_input_tokens=estimate,
                    reserved_output_tokens=reserve,
                    retained_transcript_indexes=tuple(range(len(run.transcript))),
                )
            return CompiledContext(
                kind="needs_compaction",
                messages=full,
                estimated_input_tokens=estimate,
                reserved_output_tokens=reserve,
                retained_transcript_indexes=tuple(range(len(run.transcript))),
            )

        summary_message = ModelMessage(
            role="user", content=[{"kind": "text", "text": f"[summary] {summary}"}]
        )
        summary_tokens = self.estimator.estimate_messages([summary_message])
        available = run.profile.model.context_window - mandatory - summary_tokens
        if available < 0:
            return CompiledContext(
                kind="context_unfit",
                messages=[],
                estimated_input_tokens=mandatory - reserve + summary_tokens,
                reserved_output_tokens=reserve,
                retained_transcript_indexes=(),
                compacted=True,
            )
        retained = _pair_safe_tail_indexes(run.transcript)
        first_user = next(
            (
                index
                for index, message in enumerate(run.transcript)
                if isinstance(message, UserMessage)
            ),
            None,
        )
        selected: list[int] = [] if first_user is None else [first_user]
        selected_messages = (
            []
            if first_user is None
            else agent_messages_to_model(
                [run.transcript[first_user]], prepare_orchestration_context=True
            )
        )
        if self.estimator.estimate_messages(selected_messages) > available:
            return CompiledContext(
                kind="context_unfit",
                messages=[],
                estimated_input_tokens=(
                    mandatory
                    - reserve
                    + summary_tokens
                    + self.estimator.estimate_messages(selected_messages)
                ),
                reserved_output_tokens=reserve,
                retained_transcript_indexes=(),
                compacted=True,
            )
        for group in reversed(retained):
            if first_user is not None and first_user in group:
                continue
            candidate_indexes = sorted({*group, *selected})
            candidate_transcript = [
                run.transcript[index] for index in candidate_indexes
            ]
            candidate = agent_messages_to_model(
                candidate_transcript, prepare_orchestration_context=True
            )
            if self.estimator.estimate_messages(candidate) > available:
                continue
            selected = candidate_indexes
            selected_messages = candidate
        selected_messages.insert(0, summary_message)
        estimate = (
            mandatory - reserve + self.estimator.estimate_messages(selected_messages)
        )
        return CompiledContext(
            kind="ready",
            messages=[*background, *selected_messages],
            estimated_input_tokens=estimate,
            reserved_output_tokens=reserve,
            retained_transcript_indexes=tuple(selected),
            compacted=True,
        )


def _pair_safe_tail_indexes(messages: list[object]) -> list[list[int]]:
    groups: list[list[int]] = []
    index = 0
    while index < len(messages):
        message = messages[index]
        if isinstance(message, AssistantMessage) and message.tool_calls:
            call_ids = {call.call_id for call in message.tool_calls}
            group = [index]
            index += 1
            while index < len(messages) and call_ids:
                current = messages[index]
                group.append(index)
                if isinstance(current, AssistantMessage) and current.tool_calls:
                    call_ids.update(call.call_id for call in current.tool_calls)
                elif isinstance(current, ToolResultMessage):
                    call_ids.discard(current.call_id)
                index += 1
            groups.append(group)
            continue
        groups.append([index])
        index += 1
    return groups


__all__ = [
    "CompiledContext",
    "ContextCompiler",
    "DeterministicTokenEstimator",
    "UnresolvedToolBatchError",
]

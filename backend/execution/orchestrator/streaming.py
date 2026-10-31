"""Provider-neutral model stream assembly."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from typing import Literal, cast

from .models import (
    AssistantMessage,
    FinishReason,
    ModelStreamEvent,
    TextPart,
    ToolCall,
    UsageRecord,
)

_ALLOWED_FINISH_REASONS = frozenset(
    {"stop", "tool_calls", "length", "content_filter", "error", "aborted"}
)

ModelStreamAssemblyErrorCode = Literal[
    "stream_contract_violation",
    "malformed_tool_arguments",
    "truncated_tool_call",
]
ModelAssemblyOutcomeKind = Literal[
    "assistant", "context_overflow", "provider_error", "aborted"
]


class ModelStreamAssemblyError(ValueError):
    """Raised when a provider stream violates the normalized event contract."""

    def __init__(
        self,
        message: str,
        *,
        code: ModelStreamAssemblyErrorCode = "stream_contract_violation",
        provider_call_id: str | None = None,
        tool_name: str | None = None,
        tool_index: int | None = None,
        raw_arguments_digest: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.provider_call_id = provider_call_id
        self.tool_name = tool_name
        self.tool_index = tool_index
        self.raw_arguments_digest = raw_arguments_digest


class MalformedToolArgumentsError(ModelStreamAssemblyError):
    """Raised only when a completed tool call has invalid arguments."""

    def __init__(
        self,
        message: str,
        *,
        provider_call_id: str | None = None,
        tool_name: str | None = None,
        tool_index: int | None = None,
        raw_arguments: str | None = None,
    ) -> None:
        super().__init__(
            message,
            code="malformed_tool_arguments",
            provider_call_id=provider_call_id,
            tool_name=tool_name,
            tool_index=tool_index,
            raw_arguments_digest=(
                sha256(raw_arguments.encode()).hexdigest()
                if raw_arguments is not None
                else None
            ),
        )


class TruncatedToolCallError(ModelStreamAssemblyError):
    """Raised only when tool-call assembly finishes before every call closes."""

    def __init__(
        self,
        message: str,
        *,
        provider_call_id: str | None = None,
        tool_name: str | None = None,
        tool_index: int | None = None,
        raw_arguments: str | None = None,
    ) -> None:
        super().__init__(
            message,
            code="truncated_tool_call",
            provider_call_id=provider_call_id,
            tool_name=tool_name,
            tool_index=tool_index,
            raw_arguments_digest=(
                sha256(raw_arguments.encode()).hexdigest()
                if raw_arguments is not None
                else None
            ),
        )


@dataclass(frozen=True, slots=True)
class ModelAssemblyOutcome:
    kind: ModelAssemblyOutcomeKind
    assistant: AssistantMessage | None = None
    error_class: str | None = None
    provider_request_id: str | None = None
    # Gateway code of a terminal error, e.g. ``provider_not_configured``.
    error_code: str | None = None


class ModelStreamAssembler:
    """Assemble one normalized assistant outcome from ordered stream events."""

    def __init__(self) -> None:
        self._text: list[str] = []
        self._tool_names: dict[str, str] = {}
        self._tool_arguments: dict[str, list[str]] = {}
        self._closed_calls: list[str] = []
        self._usage: UsageRecord | None = None
        self._usage_by_attempt: dict[int, UsageRecord] = {}
        self._finish_reason: FinishReason | None = None
        self._terminal_error_class: str | None = None
        self._terminal_error_code: str | None = None
        self._attempt: int | None = None
        self._failed_attempt: int | None = None
        self._scheduled_attempt: int | None = None
        self.retry_events: list[ModelStreamEvent] = []
        self.provider_request_id: str | None = None

    @property
    def usage_by_attempt(self) -> dict[int, UsageRecord]:
        return dict(self._usage_by_attempt)

    def accept(self, event: ModelStreamEvent) -> None:
        if self._finish_reason is not None or self._terminal_error_class is not None:
            raise ModelStreamAssemblyError("stream event received after terminal event")
        if event.provider_request_id is not None:
            self.provider_request_id = event.provider_request_id
        handler = getattr(self, f"_accept_{event.kind}", None)
        if handler is not None:
            handler(event)

    def _clear_attempt_output(self) -> None:
        self._text.clear()
        self._tool_names.clear()
        self._tool_arguments.clear()
        self._closed_calls.clear()
        self._usage = None

    def _accept_attempt_started(self, event: ModelStreamEvent) -> None:
        if event.attempt is None:
            raise ModelStreamAssemblyError("attempt_started requires an attempt number")
        if self._attempt is not None:
            if event.attempt != self._scheduled_attempt:
                raise ModelStreamAssemblyError(
                    "next model attempt must match the scheduled retry"
                )
        elif event.attempt != 1:
            raise ModelStreamAssemblyError("first model attempt must be 1")
        self._clear_attempt_output()
        self._attempt = event.attempt
        self._failed_attempt = None
        self._scheduled_attempt = None

    def _accept_attempt_failed(self, event: ModelStreamEvent) -> None:
        if self._attempt is None or event.attempt != self._attempt:
            raise ModelStreamAssemblyError(
                "attempt_failed must identify the active model attempt"
            )
        if self._failed_attempt is not None:
            raise ModelStreamAssemblyError("model attempt failed more than once")
        self._failed_attempt = event.attempt
        self._clear_attempt_output()
        self.retry_events.append(event)

    def _accept_retry_scheduled(self, event: ModelStreamEvent) -> None:
        if self._failed_attempt != self._attempt:
            raise ModelStreamAssemblyError(
                "retry_scheduled must follow a failed model attempt"
            )
        if event.attempt != self._attempt + 1:
            raise ModelStreamAssemblyError(
                "retry_scheduled must identify the next model attempt"
            )
        if self._scheduled_attempt is not None:
            raise ModelStreamAssemblyError("model retry scheduled more than once")
        failed_event = self.retry_events[-1]
        if (
            event.error_class != failed_event.error_class
            or event.retryable != failed_event.retryable
        ):
            raise ModelStreamAssemblyError(
                "scheduled retry must preserve failed-attempt classification"
            )
        self._scheduled_attempt = event.attempt
        self.retry_events.append(event)

    def _accept_text_delta(self, event: ModelStreamEvent) -> None:
        if self._failed_attempt is not None and self._failed_attempt == self._attempt:
            raise ModelStreamAssemblyError("output received after attempt failure")
        self._text.append(event.delta or "")

    def _accept_reasoning_delta(self, event: ModelStreamEvent) -> None:
        del event  # reasoning remains private lifecycle data

    def _accept_tool_call_start(self, event: ModelStreamEvent) -> None:
        if not event.call_id or not event.tool_name:
            raise ModelStreamAssemblyError(
                "tool_call_start requires call_id and tool_name"
            )
        if event.call_id in self._tool_names:
            raise ModelStreamAssemblyError("duplicate tool_call_start")
        self._tool_names[event.call_id] = event.tool_name
        self._tool_arguments[event.call_id] = []

    def _accept_tool_call_arguments_delta(self, event: ModelStreamEvent) -> None:
        if not event.call_id or event.call_id not in self._tool_arguments:
            raise ModelStreamAssemblyError("arguments delta precedes tool call start")
        self._tool_arguments[event.call_id].append(event.delta or "")

    def _accept_tool_call_end(self, event: ModelStreamEvent) -> None:
        if not event.call_id or event.call_id not in self._tool_arguments:
            raise ModelStreamAssemblyError("tool_call_end precedes tool call start")
        if event.call_id in self._closed_calls:
            raise ModelStreamAssemblyError("duplicate tool_call_end")
        self._closed_calls.append(event.call_id)

    def _accept_usage(self, event: ModelStreamEvent) -> None:
        if event.usage is None:
            raise ModelStreamAssemblyError("usage event has no usage")
        attempt = self._attempt or 1
        self._usage_by_attempt[attempt] = event.usage
        if self._failed_attempt != attempt:
            self._usage = event.usage

    def _accept_finish(self, event: ModelStreamEvent) -> None:
        if event.finish_reason not in _ALLOWED_FINISH_REASONS:
            raise ModelStreamAssemblyError("unsupported finish reason")
        self._finish_reason = cast(FinishReason, event.finish_reason)

    def _accept_error(self, event: ModelStreamEvent) -> None:
        if event.error_class is None:
            raise ModelStreamAssemblyError("terminal error requires error_class")
        if self._attempt is not None and self._failed_attempt != self._attempt:
            raise ModelStreamAssemblyError("terminal error must follow attempt_failed")
        self._terminal_error_class = event.error_class
        self._terminal_error_code = event.error_code

    def build_outcome(
        self, *, message_id: str, created_at: datetime
    ) -> ModelAssemblyOutcome:
        if self._terminal_error_class is not None:
            kind: ModelAssemblyOutcomeKind = (
                "context_overflow"
                if self._terminal_error_class == "context_overflow"
                else (
                    "aborted"
                    if self._terminal_error_class == "aborted"
                    else "provider_error"
                )
            )
            return ModelAssemblyOutcome(
                kind=kind,
                error_class=self._terminal_error_class,
                provider_request_id=self.provider_request_id,
                error_code=self._terminal_error_code,
            )
        if self._finish_reason is None:
            raise ModelStreamAssemblyError("stream did not finish")
        if self._finish_reason in {"error", "aborted", "content_filter"}:
            return ModelAssemblyOutcome(
                kind=(
                    "aborted" if self._finish_reason == "aborted" else "provider_error"
                ),
                error_class=(
                    "aborted"
                    if self._finish_reason == "aborted"
                    else (
                        "content_filter"
                        if self._finish_reason == "content_filter"
                        else "unknown"
                    )
                ),
                provider_request_id=self.provider_request_id,
            )
        if set(self._closed_calls) != set(self._tool_names):
            open_call = next(
                (
                    call_id
                    for call_id in self._tool_names
                    if call_id not in self._closed_calls
                ),
                None,
            )
            raise TruncatedToolCallError(
                "stream finished with a truncated tool call",
                provider_call_id=open_call,
                tool_name=self._tool_names.get(open_call or ""),
                tool_index=(
                    list(self._tool_names).index(open_call)
                    if open_call is not None
                    else None
                ),
                raw_arguments=(
                    "".join(self._tool_arguments.get(open_call, []))
                    if open_call is not None
                    else None
                ),
            )

        tool_calls: list[ToolCall] = []
        if self._finish_reason == "tool_calls":
            tool_calls = [
                self._build_tool_call(call_id) for call_id in self._closed_calls
            ]

        content = [TextPart(text="".join(self._text))] if self._text else []
        assistant = AssistantMessage(
            message_id=message_id,
            content=content,
            tool_calls=tool_calls,
            finish_reason=self._finish_reason,
            usage=self._usage,
            created_at=created_at,
        )
        return ModelAssemblyOutcome(
            kind="assistant",
            assistant=assistant,
            provider_request_id=self.provider_request_id,
        )

    def build(self, *, message_id: str, created_at: datetime) -> AssistantMessage:
        """Return the assistant message; raise if the stream ended otherwise."""

        outcome = self.build_outcome(message_id=message_id, created_at=created_at)
        if outcome.assistant is None:
            raise ModelStreamAssemblyError(
                f"model stream ended with {outcome.kind}",
            )
        return outcome.assistant

    def _build_tool_call(self, call_id: str) -> ToolCall:
        raw_arguments = "".join(self._tool_arguments[call_id]) or "{}"
        try:
            arguments = json.loads(raw_arguments)
        except json.JSONDecodeError as exc:
            raise MalformedToolArgumentsError(
                f"malformed arguments for tool call {call_id!r}",
                provider_call_id=call_id,
                tool_name=self._tool_names.get(call_id),
                tool_index=list(self._tool_names).index(call_id),
                raw_arguments=raw_arguments,
            ) from exc
        if not isinstance(arguments, dict):
            raise MalformedToolArgumentsError(
                "tool arguments must be an object",
                provider_call_id=call_id,
                tool_name=self._tool_names.get(call_id),
                tool_index=list(self._tool_names).index(call_id),
                raw_arguments=raw_arguments,
            )
        return ToolCall(
            call_id=call_id,
            tool_name=self._tool_names[call_id],
            arguments=arguments,
        )

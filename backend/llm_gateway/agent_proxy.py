"""The OpenAI wire subset used by bundled agents; no tools or tasks execute here."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from contextlib import aclosing
from typing import TYPE_CHECKING, Literal, Protocol
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from common.protocols import JsonValue
from llm_gateway.image_types import GatewayImageRequest, GatewayImageResult
from llm_gateway.provider_runtime import TextSelection
from llm_gateway.turn_types import (
    GatewayTextPart,
    GatewayToolCallPart,
    GatewayToolDefinition,
    GatewayToolResultPart,
    GatewayTurnEvent,
    GatewayTurnMessage,
    GatewayTurnRequest,
)

if TYPE_CHECKING:
    from llm_gateway.gateway import LLMGatewayImpl


class ProxyInput(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class FunctionCall(ProxyInput):
    name: str = Field(min_length=1, max_length=128)
    arguments: str = Field(max_length=262144)


class ToolCall(ProxyInput):
    id: str = Field(min_length=1, max_length=128)
    type: Literal["function"] = "function"
    function: FunctionCall


class TextContent(ProxyInput):
    type: Literal["text"]
    text: str


class ChatMessage(ProxyInput):
    role: Literal["system", "developer", "user", "assistant", "tool"]
    content: str | list[TextContent] | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list, max_length=64)
    tool_call_id: str | None = None
    name: str | None = None

    def text(self) -> str:
        if isinstance(self.content, list):
            return "\n".join(part.text for part in self.content)
        return self.content or ""


class FunctionDefinition(ProxyInput):
    name: str = Field(min_length=1, max_length=128)
    description: str = ""
    parameters: dict[str, object] = Field(default_factory=dict)
    strict: Literal[False] | None = None


class ToolDefinition(ProxyInput):
    type: Literal["function"] = "function"
    function: FunctionDefinition


class StreamOptions(ProxyInput):
    include_usage: bool = False


class ChatRequest(ProxyInput):
    model: str = Field(min_length=1, max_length=128)
    messages: list[ChatMessage] = Field(min_length=1, max_length=256)
    tools: list[ToolDefinition] = Field(default_factory=list, max_length=64)
    tool_choice: Literal["auto", "none", "required"] = "auto"
    stream: bool = False
    stream_options: StreamOptions | None = None
    temperature: float | None = Field(default=None, ge=0, le=1)
    max_tokens: int | None = Field(default=None, gt=0, le=32768)
    max_completion_tokens: int | None = Field(default=None, gt=0, le=32768)
    n: Literal[1] = 1
    client_request_id: str | None = Field(
        default=None, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$"
    )


def turn_request(
    body: ChatRequest, correlation: str, *, max_output_tokens: int = 32768
) -> GatewayTurnRequest:
    system: list[str] = []
    messages: list[GatewayTurnMessage] = []
    call_names: dict[str, str] = {}
    for message in body.messages:
        if message.role in {"system", "developer"}:
            if message.tool_calls or message.tool_call_id:
                raise ValueError("Invalid system message")
            system.append(message.text())
            continue
        parts = (
            [GatewayTextPart(text=message.text())]
            if message.content is not None
            else []
        )
        if message.role == "tool":
            name = call_names.get(message.tool_call_id or "")
            if name is None or message.tool_calls:
                raise ValueError("Unmatched tool result")
            parts = [
                GatewayToolResultPart(
                    call_id=message.tool_call_id, tool_name=name, content=message.text()
                )
            ]
        elif message.tool_call_id or (
            message.tool_calls and message.role != "assistant"
        ):
            raise ValueError("Invalid tool message")
        for call in message.tool_calls:
            arguments = json.loads(call.function.arguments)
            if not isinstance(arguments, dict) or call.id in call_names:
                raise ValueError("Invalid tool arguments or duplicate call ID")
            call_names[call.id] = call.function.name
            parts.append(
                GatewayToolCallPart(
                    call_id=call.id, tool_name=call.function.name, arguments=arguments
                )
            )
        messages.append(GatewayTurnMessage(role=message.role, parts=parts))
    return GatewayTurnRequest(
        provider="openai",
        model_id=body.model,
        api="chat_completions",
        system_prompt="\n\n".join(system),
        messages=messages,
        tools=[
            GatewayToolDefinition(
                name=t.function.name,
                description=t.function.description,
                input_schema=t.function.parameters,
            )
            for t in body.tools
        ],
        tool_choice=body.tool_choice,
        tool_strategy="native",
        temperature=body.temperature,
        max_output_tokens=min(
            body.max_completion_tokens or body.max_tokens or max_output_tokens,
            max_output_tokens,
        ),
        timeout_seconds=120,
        # Agent tool loops can make several model calls for one client request.
        turn_id=f"{correlation}:{uuid4().hex}",
    )


def _delta(event: GatewayTurnEvent) -> dict[str, object] | None:
    if event.kind == "text_delta":
        return {"content": event.delta or ""}
    if event.kind == "tool_call_start":
        return {
            "tool_calls": [
                {
                    "index": event.tool_index,
                    "id": event.call_id,
                    "type": "function",
                    "function": {"name": event.tool_name, "arguments": ""},
                }
            ]
        }
    if event.kind == "tool_call_arguments_delta":
        return {
            "tool_calls": [
                {
                    "index": event.tool_index,
                    "function": {"arguments": event.delta or ""},
                }
            ]
        }
    return None


class _AgentLLMProxyPort(Protocol):
    """Private route seam; no execution or persistence operations."""

    token: SecretStr
    slots: asyncio.Semaphore

    async def select_text(self) -> TextSelection: ...

    def chat_chunks(
        self, turn: GatewayTurnRequest, selection: TextSelection
    ) -> AsyncIterator[dict[str, JsonValue]]: ...

    async def image(self, body: GatewayImageRequest) -> GatewayImageResult: ...


class AgentLLMProxy:
    def __init__(self, gateway: LLMGatewayImpl, token: str) -> None:
        self.gateway = gateway
        self.token = SecretStr(token.strip())
        # Bound billable work per backend worker; no queue/retry service.
        self.slots = asyncio.Semaphore(4)

    async def select_text(self) -> TextSelection:
        """One Provider pair and text model for a whole request.

        The output ceiling, the chunk model label, and the turn all come from
        this selection, so a concurrent setup save cannot mix two models.
        Raises ``ProviderNotConfigured`` without a working pair.
        """
        return await self.gateway.select_text()

    async def chat_chunks(
        self, turn: GatewayTurnRequest, selection: TextSelection
    ) -> AsyncIterator[dict[str, JsonValue]]:
        base = {
            "id": "chatcmpl-" + uuid4().hex,
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": selection.model.model_id,
        }
        finished, emitted_bytes = False, 0
        async with asyncio.timeout(turn.timeout_seconds), self.slots:
            stream = self.gateway.stream_turn_selected(turn, selection)
            async with aclosing(stream):
                yield {
                    **base,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"role": "assistant"},
                            "finish_reason": None,
                        }
                    ],
                }
                async for event in stream:
                    emitted_bytes += len((event.delta or "").encode("utf-8"))
                    if emitted_bytes > 4 * 1024 * 1024:
                        raise ValueError("Provider output exceeds proxy limit")
                    if event.kind == "finish":
                        if event.finish_reason == "error":
                            raise ValueError("Provider turn failed")
                        finished = True
                        yield {
                            **base,
                            "choices": [
                                {
                                    "index": 0,
                                    "delta": {},
                                    "finish_reason": event.finish_reason,
                                }
                            ],
                        }
                    elif event.kind == "usage":
                        usage = event.usage
                        yield {
                            **base,
                            "choices": [],
                            "usage": {
                                "prompt_tokens": usage.input_tokens,
                                "completion_tokens": usage.output_tokens,
                                "total_tokens": usage.input_tokens
                                + usage.output_tokens,
                            },
                        }
                    else:
                        delta = _delta(event)
                        if delta is not None:
                            yield {
                                **base,
                                "choices": [
                                    {"index": 0, "delta": delta, "finish_reason": None}
                                ],
                            }
        if not finished:
            raise ValueError("Provider stream ended without a finish event")

    async def image(self, body: GatewayImageRequest) -> GatewayImageResult:
        async with asyncio.timeout(body.timeout_seconds), self.slots:
            return await self.gateway.generate_image(body)


async def completion(
    chunks: AsyncIterator[dict[str, JsonValue]],
) -> dict[str, JsonValue]:
    """Fold the same single-attempt stream for the weather agent's ainvoke."""
    text: list[str] = []
    calls: dict[int, dict] = {}
    usage = None
    finish = None
    base = {}
    async with aclosing(chunks):
        async for chunk in chunks:
            base = {key: chunk[key] for key in ("id", "model", "created")}
            if "usage" in chunk:
                usage = chunk["usage"]
            for choice in chunk["choices"]:
                delta = choice["delta"]
                if "content" in delta:
                    text.append(delta["content"])
                for call in delta.get("tool_calls", []):
                    index = call["index"]
                    if "id" in call:
                        calls[index] = {
                            key: value for key, value in call.items() if key != "index"
                        }
                    else:
                        calls[index]["function"]["arguments"] += call["function"][
                            "arguments"
                        ]
                finish = choice.get("finish_reason") or finish
    message: dict[str, object] = {"role": "assistant", "content": "".join(text) or None}
    if calls:
        message["tool_calls"] = [calls[i] for i in sorted(calls)]
    result = {
        **base,
        "object": "chat.completion",
        "choices": [{"index": 0, "message": message, "finish_reason": finish}],
    }
    if usage is not None:
        result["usage"] = usage
    return result

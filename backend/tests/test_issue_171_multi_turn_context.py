from __future__ import annotations

from dataclasses import replace

from execution.orchestrator.context import ContextCompiler
from execution.orchestrator.models import ModelMessage, ModelTextPart
from execution.orchestrator.session import DefaultRunFactory
from execution.orchestrator_routing import DualRuntimeRouter

from ._orchestrator_helpers import FixedClock, FixedIDs, session_config, user_message


async def test_room_history_is_typed_and_excludes_current_and_subagent_turns():
    async def read_memory(room_id: str):
        assert room_id == "room-1"
        return {
            "room_id": room_id,
            "memory_id": "memory-1",
            "conversation_history": [
                {
                    "turn_id": "message:user-previous",
                    "role": "user",
                    "content": "Remember ORCHID-171.",
                    "timestamp": "2031-01-01T00:00:00Z",
                },
                {
                    "turn_id": "message:agent-detail",
                    "role": "agent",
                    "agent_id": "weather-agent",
                    "content": "internal agent detail",
                    "timestamp": "2031-01-01T00:00:01Z",
                },
                {
                    "turn_id": "message:assistant-previous",
                    "role": "agent",
                    "agent_id": "system:hybro",
                    "content": "OK",
                    "timestamp": "2031-01-01T00:00:02Z",
                },
                {
                    "turn_id": "message:user-current",
                    "role": "user",
                    "content": "What was the code?",
                    "timestamp": "2031-01-01T00:00:03Z",
                },
            ],
        }

    router = DualRuntimeRouter(room_memory_reader=read_memory)
    history = await router._load_conversation_history(  # noqa: SLF001
        "room-1", current_message_id="user-current"
    )

    assert [message.role for message in history] == ["user", "assistant"]
    assert [message.content[0].text for message in history] == [
        "Remember ORCHID-171.",
        "OK",
    ]


def test_new_run_freezes_room_history_before_current_message():
    prior_user = ModelMessage(
        role="user", content=[ModelTextPart(text="Remember ORCHID-171.")]
    )
    prior_assistant = ModelMessage(role="assistant", content=[ModelTextPart(text="OK")])
    config = replace(
        session_config(), conversation_history=(prior_user, prior_assistant)
    )
    current = user_message("What was the code?").model_copy(
        update={"message_id": "user-current"}
    )

    run = DefaultRunFactory(clock=FixedClock(), id_factory=FixedIDs()).create_run(
        config=config,
        message=current,
        client_request_id="request-1",
    )
    compiled = ContextCompiler().compile(
        run, tools=[], background=run.background_context
    )

    assert run.transcript == [current]
    assert run.background_context == [prior_user, prior_assistant]
    assert compiled.kind == "ready"
    assert [message.role for message in compiled.messages] == [
        "user",
        "assistant",
        "user",
    ]
    assert [message.content[0].text for message in compiled.messages] == [
        "Remember ORCHID-171.",
        "OK",
        "What was the code?",
    ]

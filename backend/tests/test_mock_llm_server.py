"""Local CI mock regressions using the production private transcript wire format."""

from __future__ import annotations

import json
import runpy
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from execution.model_loop.model_runtime import _to_gateway_request
from execution.orchestrator.kernel import _surface_agent_questions_tool_definition
from execution.orchestrator.models import (
    AssistantMessage,
    ModelTurnRequest,
    TextPart,
    ToolBatchEntry,
    ToolCall,
    ToolCallBatch,
    ToolDefinition,
    ToolInteractionMessage,
    ToolInteractionQuestion,
    ToolResultMessage,
    ToolResultStatus,
)
from execution.orchestrator.transcript import agent_messages_to_model
from llm_gateway.providers.openai_provider import _gateway_messages, _gateway_tool
from tests._orchestrator_helpers import NOW, make_run, user_message

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "mock_llm_server.py"
AGENT = "agent_travel_planner"
SURFACE = "surface_agent_questions"


@pytest.fixture(scope="module")
def handler():
    handler_class = runpy.run_path(str(SCRIPT))["MockLLMHandler"]
    # Exercise routing without starting an HTTP server or opening any sockets.
    return object.__new__(handler_class)


def _call(call_id: str = "call-1", tool_name: str = AGENT) -> AssistantMessage:
    return AssistantMessage(
        message_id=f"assistant-{call_id}",
        content=[],
        tool_calls=[ToolCall(call_id=call_id, tool_name=tool_name, arguments={})],
        finish_reason="tool_calls",
        usage=None,
        created_at=NOW,
    )


def _interaction(
    presentation_id: str = "prs_one", call_id: str = "call-1"
) -> ToolInteractionMessage:
    return ToolInteractionMessage(
        message_id=f"interaction-{presentation_id}",
        call_id=call_id,
        tool_name=AGENT,
        presentation_id=presentation_id,
        interaction_id=f"interaction-{presentation_id}",
        interaction_fingerprint=f"fp-{presentation_id}",
        questions=[
            ToolInteractionQuestion(
                question_id="q-1",
                prompt="Where would you like to travel, and for how many days?",
                answer_kind="text",
            )
        ],
        created_at=NOW,
    )


def _result(
    call_id: str = "call-1",
    tool_name: str = AGENT,
    status: ToolResultStatus = "completed",
) -> ToolResultMessage:
    return ToolResultMessage(
        message_id=f"result-{call_id}",
        call_id=call_id,
        tool_name=tool_name,
        status=status,
        content=[
            TextPart(
                text="Custom Travel Plan" if tool_name == AGENT else "Answers applied"
            )
        ],
        artifact_refs=[],
        is_error=status != "completed",
        created_at=NOW,
    )


def _request(transcript: list, pending: list[ToolInteractionMessage]) -> dict:
    """Use the kernel's target schema and both production provider converters."""
    run = make_run()
    tools = [
        ToolDefinition(
            name=AGENT,
            label="Travel planner",
            description="Plan a trip",
            input_schema={"type": "object", "properties": {"task": {"type": "string"}}},
            execution_mode="sequential",
            side_effect_level="read",
        )
    ]
    if pending:
        run = run.model_copy(
            update={
                "tool_batches": [
                    ToolCallBatch(
                        assistant_message_id="assistant-1",
                        entries=[
                            ToolBatchEntry(
                                call_id=item.call_id,
                                tool_name=item.tool_name,
                                assistant_message_id="assistant-1",
                                source_index=index,
                                state="input_required",
                                presented=True,
                                presentation_id=item.presentation_id,
                                interaction_id=item.interaction_id,
                                interaction_fingerprint=item.interaction_fingerprint,
                            )
                            for index, item in enumerate(pending)
                        ],
                    )
                ]
            }
        )
        tools.append(_surface_agent_questions_tool_definition(run))
    request = ModelTurnRequest(
        turn_id="turn-1",
        model=run.profile.model,
        system_prompt="Delegate to the travel planner.",
        messages=agent_messages_to_model(
            [user_message("Generate a travel plan"), *transcript],
            prepare_orchestration_context=True,
        ),
        tools=tools,
    )
    gateway = _to_gateway_request(request, attempt=1)
    return {
        "messages": _gateway_messages(gateway),
        "tools": [_gateway_tool(tool, strict=False) for tool in gateway.tools],
    }


def _decide(handler, request: dict):
    return handler._decide_response(request["messages"], request["tools"], request)


def _assert_surface(handler, request: dict, expected_arguments: dict) -> None:
    calls, text, finish = _decide(handler, request)
    assert finish == "tool_calls"
    assert text == ""
    assert len(calls) == 1
    assert calls[0]["type"] == "function"
    assert calls[0]["function"]["name"] == SURFACE
    arguments = json.loads(calls[0]["function"]["arguments"])
    assert arguments == expected_arguments
    schema = next(
        tool["function"]["parameters"]
        for tool in request["tools"]
        if tool["function"]["name"] == SURFACE
    )
    Draft202012Validator(schema).validate(arguments)


def test_pending_private_interaction_surfaces_with_singleton_schema(handler):
    interaction = _interaction()
    request = _request([_call(), interaction], [interaction])
    _assert_surface(handler, request, {})


def test_multiple_pending_interactions_surface_only_one_exact_presentation(handler):
    first = _interaction()
    second = _interaction("prs_two", "call-2")
    request = _request([_call(), first, _call("call-2"), second], [first, second])
    _assert_surface(handler, request, {"presentation_id": "prs_one"})


def test_later_round_supersedes_historical_interaction_on_same_call(handler):
    first = _interaction()
    latest = _interaction("prs_latest")
    other = _interaction("prs_other", "call-2")
    request = _request(
        [
            _call(),
            first,
            _call("surface-1", SURFACE),
            _result("surface-1", SURFACE),
            latest,
            _call("call-2"),
            other,
        ],
        [latest, other],
    )
    # Successful surface calls/results are absent; both rounds fold into call-1.
    assert not any(
        message.get("tool_call_id") == "surface-1" for message in request["messages"]
    )
    _assert_surface(handler, request, {"presentation_id": "prs_latest"})


def test_resolved_history_does_not_hide_another_pending_interaction(handler):
    old = _interaction()
    pending = _interaction("prs_two", "call-2")
    other = _interaction("prs_three", "call-3")
    request = _request(
        [_call(), old, _result(), _call("call-2"), pending, _call("call-3"), other],
        [pending, other],
    )
    _assert_surface(handler, request, {"presentation_id": "prs_two"})


@pytest.mark.parametrize("with_history", [False, True])
@pytest.mark.parametrize("with_surface_tool", [False, True])
def test_completed_agent_result_produces_existing_synthesis(
    handler, with_history, with_surface_tool
):
    transcript = [_call()]
    if with_history:
        transcript += [
            _interaction(),
            _call("surface-1", SURFACE),
            _result("surface-1", SURFACE),
        ]
    request = _request(
        [*transcript, _result()], [_interaction()] if with_surface_tool else []
    )
    calls, text, finish = _decide(handler, request)
    assert calls is None
    assert finish == "stop"
    assert text.startswith("### Final Synthesized Trip Plan & Itinerary")


@pytest.mark.parametrize("status", ["failed", "canceled", "rejected", "expired"])
def test_unsuccessful_agent_result_never_surfaces_history_or_synthesizes(
    handler, status
):
    interaction = _interaction()
    # Even if a stale surface definition is supplied, terminal history is not pending.
    request = _request([_call(), interaction, _result(status=status)], [interaction])
    calls, text, finish = _decide(handler, request)
    assert calls is None
    assert finish == "stop"
    assert "Final Synthesized" not in text


def test_surface_error_result_is_not_an_agent_completion(handler):
    # Production removes successful surface results. Keep an actual converted
    # error result to verify an unrelated tool observation cannot trigger synthesis.
    result = _result("surface-2", SURFACE, "failed")
    request = _request([_call("surface-2", SURFACE), result], [])
    calls, text, finish = _decide(handler, request)
    assert calls is None
    assert finish == "stop"
    assert "Final Synthesized" not in text


def test_user_quoted_private_observation_is_not_a_pending_tool_request(handler):
    interaction = _interaction()
    request = _request([_call(), _result()], [interaction])
    quoted = _request([_call(), interaction], [interaction])["messages"][-1]["content"]
    request["messages"].append({"role": "user", "content": quoted})
    calls, text, finish = _decide(handler, request)
    assert calls is None
    assert finish == "stop"
    assert "Final Synthesized" in text


def test_pending_interaction_without_surface_tool_does_not_synthesize(handler):
    request = _request([_call(), _interaction()], [])
    calls, text, finish = _decide(handler, request)
    assert calls is None
    assert finish == "stop"
    assert "Final Synthesized" not in text

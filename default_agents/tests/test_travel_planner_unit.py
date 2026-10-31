from __future__ import annotations

import pytest
from unittest.mock import MagicMock
from langchain_core.messages import AIMessageChunk

from travel_planner_agent.agent import TravelPlannerAgent
from travel_planner_agent.agent_card import (
    AGENT_NAME,
    TRAVEL_PLANNER_EXAMPLES,
    build_travel_planner_agent_card,
    build_travel_planner_skill,
)
from travel_planner_agent.interaction_metadata import (
    HYBRO_A2A_INTERACTION_METADATA_KEY,
    build_input_required_metadata,
)


QUESTIONS = [
    {
        "header": "Destination",
        "question": "Where would you like to go?",
        "choices": [
            {"label": "Kyoto", "description": "Temples and food"},
            {"label": "Osaka"},
        ],
        "multi_select": False,
    },
    {
        "header": "Duration",
        "question": "How many days?",
        "choices": [],
        "multi_select": False,
    },
]


def test_input_required_metadata_asks_each_question_separately():
    first = build_input_required_metadata("remote-task-1", QUESTIONS)
    repeated = build_input_required_metadata("remote-task-1", QUESTIONS)
    changed = build_input_required_metadata("remote-task-1", QUESTIONS[:1])

    assert first == repeated
    assert first != changed
    assert set(first) == {HYBRO_A2A_INTERACTION_METADATA_KEY}
    spec = first[HYBRO_A2A_INTERACTION_METADATA_KEY]
    assert spec["schema_version"] == 1
    assert spec["interaction_id"].startswith("travel-planner:")
    assert spec["questions"] == [
        {
            "question_id": "destination",
            "interaction_kind": "questionnaire",
            "header": "Destination",
            "prompt": "Where would you like to go?",
            "answer_kind": "single_choice",
            "required": True,
            "choices": [
                {"value": "Kyoto", "description": "Temples and food"},
                {"value": "Osaka"},
            ],
            "allow_other": True,
        },
        {
            "question_id": "duration",
            "interaction_kind": "questionnaire",
            "header": "Duration",
            "prompt": "How many days?",
            "answer_kind": "text",
            "required": True,
        },
    ]


def test_input_required_metadata_keeps_question_ids_unique():
    spec = build_input_required_metadata(
        "task", [QUESTIONS[1], {**QUESTIONS[1], "question": "Nights?"}]
    )[HYBRO_A2A_INTERACTION_METADATA_KEY]
    assert [q["question_id"] for q in spec["questions"]] == ["duration", "duration-2"]


def test_multi_select_question_becomes_multi_choice():
    question = {
        "header": "Interests",
        "question": "What do you enjoy?",
        "choices": [{"label": "Food"}, {"label": "Temples"}],
        "multi_select": True,
    }
    [spec] = build_input_required_metadata("task", [question])[
        HYBRO_A2A_INTERACTION_METADATA_KEY
    ]["questions"]
    assert spec["answer_kind"] == "multi_choice"
    assert spec["allow_other"] is True


def test_travel_planner_agent_card_has_routing_friendly_metadata():
    card = build_travel_planner_agent_card("http://travel-planner-agent:7002")
    skill = build_travel_planner_skill()

    assert card.name == AGENT_NAME
    assert card.supported_interfaces[0].url == "http://travel-planner-agent:7002"
    assert "trip planning" in card.description.lower()
    assert "itinerar" in card.description.lower()
    assert len(card.skills) == 1
    assert card.skills[0].id == skill.id
    assert card.skills[0].tags == skill.tags
    assert card.skills[0].examples == list(TRAVEL_PLANNER_EXAMPLES)
    assert "Generate a travel plan" in card.skills[0].examples
    assert "hello" not in card.skills[0].examples


@pytest.mark.asyncio
async def test_tool_call_only_stream_yields_input_required():
    agent = TravelPlannerAgent()
    mock_model = MagicMock()
    mock_with_tools = MagicMock()
    mock_model.bind_tools.return_value = mock_with_tools

    async def fake_astream(messages):
        # Chunk 1: tool call start
        yield AIMessageChunk(
            content="",
            tool_call_chunks=[
                {
                    "name": "AskUserForClarification",
                    "args": '{"questions": [{"header": "Destination", "question": "Where do ',
                    "id": "call-1",
                    "index": 0,
                }
            ],
        )
        # Chunk 2: tool call args continuation
        yield AIMessageChunk(
            content="",
            tool_call_chunks=[
                {
                    "name": None,
                    "args": 'you want to go?", "choices": [{"label": "Kyoto"}, {"label": "Osaka"}]}]}',
                    "id": "call-1",
                    "index": 0,
                }
            ],
        )

    mock_with_tools.astream = fake_astream
    agent._model = mock_model

    events = []
    async for event in agent.stream("Plan a trip"):
        events.append(event)

    assert len(events) == 1
    assert events[0] == {
        "content": "Destination: Where do you want to go?",
        "done": True,
        "status": "input_required",
        "questions": [
            {
                "header": "Destination",
                "question": "Where do you want to go?",
                "choices": [
                    {"label": "Kyoto", "description": None},
                    {"label": "Osaka", "description": None},
                ],
                "multi_select": False,
            }
        ],
    }


@pytest.mark.asyncio
async def test_text_and_tool_call_stream_prefers_input_required():
    agent = TravelPlannerAgent()
    mock_model = MagicMock()
    mock_with_tools = MagicMock()
    mock_model.bind_tools.return_value = mock_with_tools

    async def fake_astream(messages):
        yield AIMessageChunk(content="Thinking about your trip...")
        yield AIMessageChunk(
            content="",
            tool_call_chunks=[
                {
                    "name": "AskUserForClarification",
                    "args": '{"questions": [{"header": "Duration", "question": "How many days will you stay?"}]}',
                    "id": "call-2",
                    "index": 0,
                }
            ],
        )

    mock_with_tools.astream = fake_astream
    agent._model = mock_model

    events = []
    async for event in agent.stream("Trip to Tokyo"):
        events.append(event)

    assert len(events) == 2
    assert events[0] == {"content": "Thinking about your trip...", "done": False}
    assert events[1]["content"] == "Duration: How many days will you stay?"
    assert events[1]["status"] == "input_required"
    assert events[1]["questions"][0]["choices"] == []


@pytest.mark.asyncio
async def test_text_only_stream_completes_with_done():
    agent = TravelPlannerAgent()
    mock_model = MagicMock()
    mock_with_tools = MagicMock()
    mock_model.bind_tools.return_value = mock_with_tools

    async def fake_astream(messages):
        yield AIMessageChunk(content="Day 1: Arrival in Tokyo. ")
        yield AIMessageChunk(content="Day 2: Explore Shibuya.")

    mock_with_tools.astream = fake_astream
    agent._model = mock_model

    events = []
    async for event in agent.stream("Tokyo for 2 days"):
        events.append(event)

    assert len(events) == 3
    assert events[0] == {"content": "Day 1: Arrival in Tokyo. ", "done": False}
    assert events[1] == {"content": "Day 2: Explore Shibuya.", "done": False}
    assert events[2] == {"content": "", "done": True}


@pytest.mark.asyncio
async def test_tool_call_missing_question_arg_falls_back_safely():
    agent = TravelPlannerAgent()
    mock_model = MagicMock()
    mock_with_tools = MagicMock()
    mock_model.bind_tools.return_value = mock_with_tools

    async def fake_astream(messages):
        yield AIMessageChunk(
            content="",
            tool_call_chunks=[
                {
                    "name": "AskUserForClarification",
                    "args": "{}",
                    "id": "call-3",
                    "index": 0,
                }
            ],
        )

    mock_with_tools.astream = fake_astream
    agent._model = mock_model

    events = []
    async for event in agent.stream("Plan a trip"):
        events.append(event)

    assert len(events) == 1
    assert events[0] == {
        "content": "Could you provide more details?",
        "done": True,
        "status": "input_required",
        "questions": [
            {
                "header": None,
                "question": "Could you provide more details?",
                "choices": [],
                "multi_select": False,
            }
        ],
    }


@pytest.mark.asyncio
async def test_invalid_tool_call_malformed_json_args_falls_back_safely():
    agent = TravelPlannerAgent()
    mock_model = MagicMock()
    mock_with_tools = MagicMock()
    mock_model.bind_tools.return_value = mock_with_tools

    async def fake_astream(messages):
        yield AIMessageChunk(
            content="",
            tool_call_chunks=[
                {
                    "name": "AskUserForClarification",
                    "args": "not-valid-json",
                    "id": "call-4",
                    "index": 0,
                }
            ],
        )

    mock_with_tools.astream = fake_astream
    agent._model = mock_model

    events = []
    async for event in agent.stream("Plan a trip"):
        events.append(event)

    assert len(events) == 1
    assert events[0] == {
        "content": "Could you provide more details?",
        "done": True,
        "status": "input_required",
        "questions": [
            {
                "header": None,
                "question": "Could you provide more details?",
                "choices": [],
                "multi_select": False,
            }
        ],
    }

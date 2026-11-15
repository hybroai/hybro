import ast
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from google.protobuf.json_format import MessageToDict

from tests.fakes.a2a_v1 import make_agent_card


def test_inspection_runtime_has_no_a2a_sdk_imports():
    tree = ast.parse(Path("agent/inspection.py").read_text())
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.ImportFrom)
            and node.module
            and (node.module == "a2a" or node.module.startswith("a2a."))
        ):
            raise AssertionError(f"Line {node.lineno}: still imports a2a SDK")


def test_inspection_service_accepts_validator_dependency():
    from agent.inspection import AgentInspectionService

    validator = AsyncMock(return_value=[])

    center = AgentInspectionService(validator=validator)

    assert center._validate_agent_card is validator


def test_inspection_service_has_default_validator():
    from agent.inspection import AgentInspectionService

    center = AgentInspectionService()

    assert center._validate_agent_card is not None


@pytest.mark.asyncio
async def test_inspection_service_allows_a2a_check_without_validator_use(
    monkeypatch,
):
    from agent.inspection import AgentInspectionService
    from models.request import InspectionCenterRequest

    inspect_a2a_connection = AsyncMock(
        return_value={"result": ["connected"], "status_code": 200}
    )
    monkeypatch.setattr(
        "agent.inspection.a2a_inspection.inspect_a2a_connection",
        inspect_a2a_connection,
    )
    validator = AsyncMock()

    center = AgentInspectionService(validator=validator)

    response = await center.inspect_a2a_connection(
        InspectionCenterRequest(agent_url="https://agent.example")
    )

    inspect_a2a_connection.assert_awaited_once_with("https://agent.example")
    validator.assert_not_awaited()
    assert response.result == ["connected"]


@pytest.mark.asyncio
async def test_inspection_service_uses_injected_validator(
    monkeypatch, sample_agent_card
):
    from agent.inspection import AgentInspectionService
    from models.request import InspectionCenterRequest

    validator = AsyncMock(return_value=["from fake"])
    fetch_agent_card = AsyncMock(return_value=sample_agent_card)
    monkeypatch.setattr(
        "agent.inspection.a2a_inspection.fetch_agent_card_for_inspection",
        fetch_agent_card,
    )

    center = AgentInspectionService(validator=validator)

    response = await center.inspect_agent_card(
        InspectionCenterRequest(agent_url="https://agent.example")
    )

    validator.assert_awaited_once_with(sample_agent_card.model_dump(exclude_none=False))
    assert response.result == ["from fake"]


@pytest.mark.asyncio
async def test_inspection_probe_keeps_card_data_sdk_free(monkeypatch):
    """The inspection probe hands the adapter SDK-free data and gets it back.

    Docker-host retries for both card discovery and the probe send are the
    facade's business and are covered against the real facade in
    ``test_adapter_unit.py``; what this test pins is the inspection seam itself:
    no SDK object crosses it in either direction.
    """
    from a2a_adapter import inspection
    from common.types import AgentCard

    card_data = MessageToDict(
        make_agent_card(
            url="http://127.0.0.1:9060",
            capabilities={"streaming": False},
            skills=[{"id": "s", "name": "Skill", "description": "Does work"}],
        )
    )
    fetch_card = AsyncMock(return_value=card_data)
    probe = AsyncMock(
        return_value={
            "kind": "message",
            "result": {
                "kind": "message",
                "role": "agent",
                "messageId": "probe-reply",
                "parts": [{"text": "hello back"}],
            },
            "error": None,
        }
    )
    monkeypatch.setattr(inspection, "fetch_agent_card_with_fallback", fetch_card)
    monkeypatch.setattr(inspection, "send_message", probe)

    result = await inspection.inspect_a2a_connection(
        "http://127.0.0.1:9060",
        timeout=1,
    )

    fetch_card.assert_awaited_once()
    assert fetch_card.await_args.args == ("http://127.0.0.1:9060",)
    assert fetch_card.await_args.kwargs["timeout"] == 1
    sent_card_data, _message = probe.await_args.args
    assert sent_card_data == card_data
    assert result["status_code"] == 200
    assert result["result"] == []
    # The probe reports the internal, SDK-free card model.
    assert isinstance(result["agent_card"], AgentCard)
    assert result["agent_card"].name == "TestAgent"

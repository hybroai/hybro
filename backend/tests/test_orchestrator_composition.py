"""Composition-root tests: the dark-launch graph binds and degrades safely."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from llm_gateway.model_registry import ModelRouteInfo
from orchestrator_composition import (
    OrchestratorCompositionError,
    create_orchestrator_runtime,
    validate_orchestrator_runtime,
)
from tests.orchestrator_harness.a2a_in_memory import InMemoryRoomEpochStore


def _route_info() -> ModelRouteInfo:
    return ModelRouteInfo(
        logical_name="orchestrator_fast",
        provider="openai",
        model_id="gpt-test",
        api="chat_completions",
        supports_native_tools=True,
        supports_provider_strict_schema=True,
        supports_local_structured_action=False,
        context_window=8192,
        max_output_tokens=1024,
        default_temperature=0.0,
        timeout_seconds=60.0,
        max_provider_retries=2,
        supported_thinking_levels=[],
    )


def _deps(*, route=None):
    mongo = MagicMock()
    mongo.collection.return_value = MagicMock()
    settings = SimpleNamespace(
        orchestrator_fast_model_route="orchestrator_fast",
        orchestrator_ultimate_model_route="orchestrator_ultimate",
        orchestrator_fast_prompt_id="orchestrator_fast",
        orchestrator_ultimate_prompt_id="orchestrator_ultimate",
        a2a_push_callback_base_url="",
        api_prefix="/api/v1",
    )
    model_registry = MagicMock()
    if route is None:
        model_registry.get_route_configuration.return_value = _route_info()
    else:
        model_registry.get_route_configuration.side_effect = route
    return SimpleNamespace(
        mongo=mongo,
        settings=settings,
        model_registry=model_registry,
        llm_gateway=MagicMock(),
        agent_registry=MagicMock(),
        exclusion_reader=MagicMock(),
        room_ownership_reader=MagicMock(),
        epoch_store=InMemoryRoomEpochStore(),
        room_files=MagicMock(),
    )


def test_composition_binds_the_full_graph_without_io():
    deps = _deps()
    runtime = create_orchestrator_runtime(
        mongo=deps.mongo,
        settings_obj=deps.settings,
        llm_gateway=deps.llm_gateway,
        model_registry=deps.model_registry,
        agent_registry=deps.agent_registry,
        exclusion_reader=deps.exclusion_reader,
        room_ownership_reader=deps.room_ownership_reader,
        epoch_store=deps.epoch_store,
        room_files=deps.room_files,
        message_writer=MagicMock(),
    )

    assert validate_orchestrator_runtime(runtime) == []
    assert set(runtime.profiles) == {"fast", "ultimate", "passthrough"}
    assert runtime.profiles["passthrough"].model is None
    assert runtime.profiles["passthrough"].max_model_turns == 0
    assert runtime.profiles["fast"].initial_routing == "explicit_agent_first"
    assert runtime.profiles["fast"].finalization == "pass_through"
    assert runtime.session_host is not None
    assert runtime.observation_sink is not None
    assert runtime.dispatch is not None
    assert runtime.continuation is not None
    assert runtime.hitl_delivery is not None
    assert runtime.tool_runtime.hitl is not None


def test_profile_resolution_failure_degrades_to_composition_error():
    from llm_gateway.errors import LLMModelRoutingError

    deps = _deps(route=LLMModelRoutingError("missing route"))
    with pytest.raises(OrchestratorCompositionError, match="profile resolution"):
        create_orchestrator_runtime(
            mongo=deps.mongo,
            settings_obj=deps.settings,
            llm_gateway=deps.llm_gateway,
            model_registry=deps.model_registry,
            agent_registry=deps.agent_registry,
            exclusion_reader=deps.exclusion_reader,
            room_ownership_reader=deps.room_ownership_reader,
            epoch_store=deps.epoch_store,
            room_files=deps.room_files,
            message_writer=MagicMock(),
        )


def test_validate_lists_every_missing_binding():
    from dataclasses import fields

    from orchestrator_composition import OrchestratorRuntime

    assert validate_orchestrator_runtime(None) == ["runtime"]
    expected = {field.name for field in fields(OrchestratorRuntime)}
    missing = SimpleNamespace()
    assert set(validate_orchestrator_runtime(missing)) == expected

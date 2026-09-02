from __future__ import annotations

from hashlib import sha256

import pytest

from common.config.settings import Settings
from execution.adapters.profiles import (
    BASE_ORCHESTRATOR_SYSTEM_PROMPT,
    FAST_ORCHESTRATOR_SYSTEM_PROMPT,
    ULTIMATE_ORCHESTRATOR_SYSTEM_PROMPT,
    OrchestratorProfileResolutionError,
    OrchestratorProfileResolver,
    PromptAssetRegistry,
)
from execution.orchestrator.profiles import UnsupportedProviderCapabilities
from llm_gateway.errors import LLMModelRoutingError
from llm_gateway.model_registry import ModelRegistryImpl, ModelRouteInfo


def _route(**updates) -> ModelRouteInfo:
    values = {
        "logical_name": "supervisor_model",
        "provider": "openai",
        "model_id": "gpt-5-mini",
        "api": "chat_completions",
        "supports_native_tools": True,
        "supports_provider_strict_schema": True,
        "supports_local_structured_action": False,
        "context_window": 128000,
        "max_output_tokens": 8192,
        "default_temperature": None,
        "timeout_seconds": 60.0,
        "max_provider_retries": 1,
        "supported_thinking_levels": ("low", "medium", "high"),
    }
    values.update(updates)
    return ModelRouteInfo(**values)


class FakeModelRegistry:
    def __init__(self, routes: dict[str, ModelRouteInfo] | None = None) -> None:
        self._routes = dict(routes or {})

    def get_route_configuration(self, logical_name: str) -> ModelRouteInfo:
        try:
            return self._routes[logical_name]
        except KeyError as exc:
            raise LLMModelRoutingError(
                f"No orchestrator model route configured for {logical_name!r}"
            ) from exc


def test_parameter_table_defaults_are_pinned():
    fields = Settings.model_fields
    assert fields["orchestrator_fast_model_route"].default == "supervisor_model"
    assert fields["orchestrator_fast_prompt_id"].default == "orchestrator_fast"
    assert fields["orchestrator_fast_prompt_version"].default == "5"
    assert fields["orchestrator_fast_max_model_turns"].default == 12
    assert fields["orchestrator_fast_max_agent_calls"].default == 10
    assert fields["orchestrator_fast_max_parallel_calls"].default == 3
    assert fields["orchestrator_fast_initial_routing"].default == (
        "explicit_agent_first"
    )
    assert fields["orchestrator_fast_finalization"].default == "pass_through"

    assert fields["orchestrator_ultimate_model_route"].default == "supervisor_model"
    assert fields["orchestrator_ultimate_prompt_id"].default == "orchestrator_ultimate"
    assert fields["orchestrator_ultimate_prompt_version"].default == "5"
    assert fields["orchestrator_ultimate_max_model_turns"].default == 24
    assert fields["orchestrator_ultimate_max_agent_calls"].default == 20
    assert fields["orchestrator_ultimate_max_parallel_calls"].default == 4
    assert fields["orchestrator_ultimate_initial_routing"].default == (
        "explicit_agent_first"
    )
    assert fields["orchestrator_ultimate_finalization"].default == "pass_through"


def test_orchestrator_prompts_let_the_model_decide_when_tools_are_needed():
    for prompt in (
        FAST_ORCHESTRATOR_SYSTEM_PROMPT,
        ULTIMATE_ORCHESTRATOR_SYSTEM_PROMPT,
    ):
        assert "CONVERSATION CONTINUITY" in prompt
        assert "Preserve relevant facts, recommendations" in prompt
        assert "TOOL DECISION" in prompt
        assert "actually requires an Agent" in prompt
        assert "Agent availability alone is never a reason" in prompt
        assert "follow-ups answerable from existing context" in prompt
        assert "DELEGATION FIRST" not in prompt
        assert "MUST call" not in prompt


def test_orchestrator_prompts_treat_prior_answers_as_context_not_execution_proof():
    for prompt in (
        FAST_ORCHESTRATOR_SYSTEM_PROMPT,
        ULTIMATE_ORCHESTRATOR_SYSTEM_PROMPT,
    ):
        assert "relevant prior conversation are evidence" in prompt
        assert "authoritative records of what Hybro already told" in prompt
        assert "do not, by themselves, prove that a new external action" in prompt
        assert "Tool-call arguments are intentions, not results" in prompt


def test_openai_reasoning_routes_use_responses_api_for_native_tools():
    registry = ModelRegistryImpl(
        Settings(
            _env_file=None,
            lead_ai_model="gpt-5-mini",
            classifier_ai_model="gpt-4o-mini",
            supervisor_model="gpt-5.4-mini",
        ),
        generation_provider="openai",
    )

    assert registry.get_route_configuration("supervisor_model").api == "responses"
    assert registry.get_route_configuration("lead_ai_model").api == "responses"
    assert (
        registry.get_route_configuration("classifier_ai_model").api
        == "chat_completions"
    )


def test_fast_and_ultimate_profiles_resolve_from_defaults():
    registry = FakeModelRegistry({"supervisor_model": _route()})
    resolver = OrchestratorProfileResolver(
        model_registry=registry, settings_obj=Settings()
    )

    fast = resolver.resolve("fast")
    ultimate = resolver.resolve("ultimate")

    assert fast.profile_id == "fast"
    assert fast.model.route == "supervisor_model"
    assert fast.model.model_id == "gpt-5-mini"
    assert fast.prompt.prompt_id == "orchestrator_fast"
    assert fast.prompt.version == "5"
    assert fast.prompt.rendered_system_prompt == FAST_ORCHESTRATOR_SYSTEM_PROMPT
    assert fast.initial_routing == "explicit_agent_first"
    assert fast.finalization == "pass_through"
    assert fast.max_model_turns == 12
    assert fast.max_agent_calls == 10
    assert fast.max_parallel_calls == 3

    assert ultimate.profile_id == "ultimate"
    assert ultimate.prompt.prompt_id == "orchestrator_ultimate"
    assert ultimate.prompt.version == "5"
    assert ultimate.initial_routing == "explicit_agent_first"
    assert ultimate.finalization == "pass_through"
    assert ultimate.max_model_turns == 24
    assert ultimate.max_agent_calls == 20
    assert ultimate.max_parallel_calls == 4


def test_default_prompts_share_truth_and_resource_contracts():
    for prompt in (
        FAST_ORCHESTRATOR_SYSTEM_PROMPT,
        ULTIMATE_ORCHESTRATOR_SYSTEM_PROMPT,
    ):
        assert prompt.startswith(BASE_ORCHESTRATOR_SYSTEM_PROMPT)
        assert "A successful Tool call proves only that the call completed" in prompt
        assert "minimal verified scalar facts" in prompt
        assert "Never reproduce or reconstruct a bulk or structured Artifact" in prompt
        assert "Keep conflicting evidence unresolved" in prompt
        assert "A2A AND HYBRO RUNTIME REFERENCE" in prompt
        assert "interaction_id" in prompt
        assert "answer_kind" in prompt
        assert "request_user_input" in prompt
        assert "mutually exclusive answers" in prompt
        assert "free text belong in question instead" in prompt
        assert "highest revision as the authoritative current state" in prompt
        assert (
            "do not carry forward blockers, missing fields, or status values" in prompt
        )
        assert "same batch only when they are mutually independent" in prompt
        assert "must wait for and consume the latest successful result" in prompt
        assert "A numeric target being met is not evidence of acceptance" in prompt
        assert "obtain authoritative successful evidence" in prompt
        assert "If revision identity or ordering is ambiguous, do not guess" in prompt
        assert "evidence from the responsible authority" in prompt
        assert "proposed, reviewed, accepted, authorized, and executed" in prompt
        assert "verbatim copy" not in prompt

    assert "shortest sufficient path" in FAST_ORCHESTRATOR_SYSTEM_PROMPT
    assert "bounded review-and-revision cycle" in (ULTIMATE_ORCHESTRATOR_SYSTEM_PROMPT)


def test_missing_model_route_fails_with_clear_message():
    resolver = OrchestratorProfileResolver(
        model_registry=FakeModelRegistry(), settings_obj=Settings()
    )
    with pytest.raises(
        OrchestratorProfileResolutionError, match="No orchestrator model route"
    ):
        resolver.resolve("fast")


def test_missing_prompt_asset_fails():
    registry = FakeModelRegistry({"supervisor_model": _route()})
    settings = Settings(orchestrator_fast_prompt_id="missing_prompt")
    resolver = OrchestratorProfileResolver(
        model_registry=registry, settings_obj=settings
    )
    with pytest.raises(OrchestratorProfileResolutionError, match="prompt asset"):
        resolver.resolve("fast")


def test_route_without_any_tool_capability_is_rejected():
    registry = FakeModelRegistry(
        {
            "supervisor_model": _route(
                supports_native_tools=False,
                supports_provider_strict_schema=False,
                supports_local_structured_action=False,
            )
        }
    )
    resolver = OrchestratorProfileResolver(
        model_registry=registry, settings_obj=Settings()
    )
    with pytest.raises(UnsupportedProviderCapabilities):
        resolver.resolve("fast")


def test_prompt_digest_mismatch_is_rejected():
    registry = FakeModelRegistry({"supervisor_model": _route()})
    prompts = PromptAssetRegistry()
    prompts.register(
        "orchestrator_fast",
        FAST_ORCHESTRATOR_SYSTEM_PROMPT,
        content_digest="0" * 64,
    )
    resolver = OrchestratorProfileResolver(
        model_registry=registry, prompt_registry=prompts, settings_obj=Settings()
    )
    with pytest.raises(ValueError, match="digest"):
        resolver.resolve("fast")


def test_prompt_digest_match_resolves_and_freezes_digest():
    digest = sha256(FAST_ORCHESTRATOR_SYSTEM_PROMPT.encode()).hexdigest()
    prompts = PromptAssetRegistry()
    prompts.register(
        "orchestrator_fast",
        FAST_ORCHESTRATOR_SYSTEM_PROMPT,
        content_digest=digest,
    )
    resolver = OrchestratorProfileResolver(
        model_registry=FakeModelRegistry({"supervisor_model": _route()}),
        prompt_registry=prompts,
        settings_obj=Settings(),
    )

    fast = resolver.resolve("fast")
    assert fast.prompt.content_digest == digest

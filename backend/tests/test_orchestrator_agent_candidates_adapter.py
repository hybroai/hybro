from __future__ import annotations

import pytest

from common.dto.agent import AgentInfo
from execution.adapters.agent_candidates import AgentServiceCandidateSource
from execution.orchestrator.a2a_runtime.catalog import FrozenToolCatalog
from execution.orchestrator.a2a_runtime.catalog_assembler import (
    AgentToolCatalogAssembler,
    deterministic_tool_name,
)
from execution.orchestrator.models import (
    AuthorizationBasis,
    CandidateScopeSnapshot,
    RunResourceManifestSnapshot,
)
from tests.orchestrator_harness.a2a_in_memory import (
    InMemoryAgentToolBindingStore,
    InMemoryRoomEpochStore,
)

from ._orchestrator_helpers import NOW, make_run


class FakeRegistry:
    def __init__(self, infos):
        self.infos = {info.agent_id: info for info in infos}

    async def get_agents_by_ids(self, agent_ids):
        return [
            self.infos[agent_id] for agent_id in agent_ids if agent_id in self.infos
        ]


class FakeExclusion:
    def __init__(self, excluded=frozenset()):
        self.excluded = excluded

    async def get_excluded_agent_ids(self):
        return self.excluded


def _info(
    agent_id,
    *,
    status="active",
    public=True,
    provider_id=None,
    hub_id=None,
    url="https://agent.example/a2a",
    raw_card=None,
) -> AgentInfo:
    card = {
        "name": f"Agent {agent_id}",
        "description": "description",
        "url": url,
        "defaultInputModes": ["text"],
        "defaultOutputModes": ["text"],
        "capabilities": {"streaming": True},
    }
    if raw_card:
        card.update(raw_card)
    return AgentInfo(
        agent_id=agent_id,
        name=card["name"],
        description=card["description"],
        url=card["url"],
        provider_id=provider_id,
        status=status,
        capabilities=list(card.get("capabilities", {}).keys()),
        source="hub" if hub_id else "cloud",
        hub_id=hub_id,
        is_hub_online=True,
        is_public=public,
        public_url=None,
        raw_card=card,
    )


async def test_active_direct_agent_produces_candidate():
    source = AgentServiceCandidateSource(
        agents=FakeRegistry([_info("agent-1")]),
        exclusion_reader=FakeExclusion(),
    )
    candidates = await source.list_candidates(
        run_id="run-1",
        conversation_id="room-1",
        room_epoch=1,
        requesting_subject_id="user-1",
        candidate_agent_ids=["agent-1"],
    )
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.agent_id == "agent-1"
    assert candidate.active is True
    assert candidate.authorized is True
    assert candidate.excluded is False
    assert candidate.transport_kind == "direct"
    assert candidate.endpoint_scope == "https://agent.example/a2a"
    assert "stream" in candidate.direct_capabilities


async def test_inactive_or_private_or_excluded_flags_are_surface():
    source = AgentServiceCandidateSource(
        agents=FakeRegistry(
            [
                _info("inactive", status="inactive"),
                _info("private", public=False, provider_id="other"),
                _info("excluded"),
            ]
        ),
        exclusion_reader=FakeExclusion(excluded=frozenset({"excluded"})),
    )
    candidates = await source.list_candidates(
        run_id="run-1",
        conversation_id="room-1",
        room_epoch=1,
        requesting_subject_id="user-1",
        candidate_agent_ids=["inactive", "private", "excluded"],
    )
    by_id = {candidate.agent_id: candidate for candidate in candidates}
    assert by_id["inactive"].active is False
    assert by_id["private"].authorized is False
    assert by_id["excluded"].excluded is True


async def test_hub_agent_uses_relay_transport_and_hub_scope():
    source = AgentServiceCandidateSource(
        agents=FakeRegistry([_info("hub-agent", hub_id="hub-1")]),
        exclusion_reader=FakeExclusion(),
    )
    candidates = await source.list_candidates(
        run_id="run-1",
        conversation_id="room-1",
        room_epoch=1,
        requesting_subject_id="user-1",
        candidate_agent_ids=["hub-agent"],
    )
    assert candidates[0].transport_kind == "direct"


async def test_skills_produce_only_per_skill_candidates():
    info = _info(
        "agent-1",
        raw_card={
            "name": "Weather Agent",
            "skills": [
                {
                    "id": "skill-1",
                    "name": "Get Current Weather",
                    "description": "current weather",
                },
                {"id": "skill-2", "name": "Get Forecast", "description": "forecast"},
            ],
        },
    )
    source = AgentServiceCandidateSource(
        agents=FakeRegistry([info]),
        exclusion_reader=FakeExclusion(),
    )
    candidates = await source.list_candidates(
        run_id="run-1",
        conversation_id="room-1",
        room_epoch=1,
        requesting_subject_id="user-1",
        candidate_agent_ids=["agent-1"],
    )
    assert [candidate.skill_id for candidate in candidates] == ["skill-1", "skill-2"]
    current = candidates[0]
    assert current.agent_display_name == "Weather Agent"
    assert current.display_name == "Weather Agent - Get Current Weather"


async def test_malformed_and_duplicate_skills_keep_only_usable_unique_tools():
    info = _info(
        "agent-1",
        raw_card={
            "skills": [
                None,
                {},
                {"id": {"nested": "value"}, "name": "object-id-fallback"},
                {"id": ["value"], "name": "list-id-fallback"},
                {"id": True, "name": "bool-id-fallback"},
                {"id": 42, "name": "numeric-id-fallback"},
                {"id": "   ", "name": "blank-id-fallback"},
                {"id": "submission-intake", "name": "Prepare Submission"},
                {"id": "submission-intake", "name": "Duplicate"},
                {"id": "invalid-name", "name": {"nested": "value"}},
                {"id": "list-name", "name": ["value"]},
                {"id": "bool-name", "name": False},
                {"id": "numeric-name", "name": 7},
                {"id": "blank-name", "name": "   "},
                42,
            ]
        },
    )
    source = AgentServiceCandidateSource(agents=FakeRegistry([info]))

    candidates = await source.list_candidates(
        run_id="run-1",
        conversation_id="room-1",
        room_epoch=1,
        requesting_subject_id="user-1",
        candidate_agent_ids=["agent-1"],
    )

    assert [candidate.skill_id for candidate in candidates] == [
        "object-id-fallback",
        "list-id-fallback",
        "bool-id-fallback",
        "numeric-id-fallback",
        "blank-id-fallback",
        "submission-intake",
        "invalid-name",
        "list-name",
        "bool-name",
        "numeric-name",
        "blank-name",
    ]
    assert [candidate.display_name for candidate in candidates] == [
        "Agent agent-1 - object-id-fallback",
        "Agent agent-1 - list-id-fallback",
        "Agent agent-1 - bool-id-fallback",
        "Agent agent-1 - numeric-id-fallback",
        "Agent agent-1 - blank-id-fallback",
        "Agent agent-1 - Prepare Submission",
        "Agent agent-1 - invalid-name",
        "Agent agent-1 - list-name",
        "Agent agent-1 - bool-name",
        "Agent agent-1 - numeric-name",
        "Agent agent-1 - blank-name",
    ]


@pytest.mark.parametrize(
    "skills",
    [
        None,
        [],
        "legacy-skills",
        [
            None,
            {},
            {"id": "", "name": "   "},
            {"id": {"nested": "value"}, "name": ["value"]},
            {"id": ["value"], "name": {"nested": "value"}},
            {"id": True, "name": False},
            {"id": 42, "name": 7},
            42,
        ],
    ],
)
async def test_skill_less_and_legacy_cards_keep_whole_agent_fallback(skills):
    raw_card = {} if skills is None else {"skills": skills}
    info = _info("agent-1", raw_card=raw_card)
    source = AgentServiceCandidateSource(agents=FakeRegistry([info]))

    candidates = await source.list_candidates(
        run_id="run-1",
        conversation_id="room-1",
        room_epoch=1,
        requesting_subject_id="user-1",
        candidate_agent_ids=["agent-1"],
    )

    assert len(candidates) == 1
    assert candidates[0].skill_id is None
    assert candidates[0].display_name == "Agent agent-1"


async def test_multiskill_card_freezes_only_skill_bindings_and_model_tools():
    info = _info(
        "broker-agent",
        raw_card={
            "name": "Broker Agent",
            "skills": [
                {"id": "submission-intake", "name": "Prepare Submission"},
                {"id": "quote-negotiation", "name": "Negotiate Quote"},
                {"id": "quote-review", "name": "Review Quote"},
            ],
        },
    )
    source = AgentServiceCandidateSource(agents=FakeRegistry([info]))
    epochs = InMemoryRoomEpochStore()
    await epochs.activate("room-1", "create-1", activated_at=NOW)
    bindings = InMemoryAgentToolBindingStore()
    assembler = AgentToolCatalogAssembler(
        candidate_source=source,
        binding_store=bindings,
        room_epoch_store=epochs,
    )

    prepared = await assembler.prepare(
        run_id="run-1",
        conversation_id="room-1",
        room_epoch=1,
        requesting_subject_id="user-1",
        candidate_scope=CandidateScopeSnapshot(
            snapshot_id="scope-1",
            revision=1,
            source="test",
            conversation_id="room-1",
            agent_ids=["broker-agent"],
            authorization_basis=AuthorizationBasis(kind="room_member"),
        ),
        resource_manifest=RunResourceManifestSnapshot(
            manifest_id="resources", refs=[], content_digest="empty"
        ),
        authorization_basis_digest="auth-basis",
        created_at=NOW,
    )

    # Agent Card order, so the first tool is the card's first skill.
    expected_skill_ids = ["submission-intake", "quote-negotiation", "quote-review"]
    assert [binding.skill_id for binding in prepared.bindings] == expected_skill_ids
    assert len(prepared.snapshot.entries) == 3
    assert len(prepared.bindings) == 3
    assert await bindings.load(prepared.bindings[0].binding_id) == prepared.bindings[0]

    run = make_run().model_copy(update={"tool_catalog": prepared.snapshot})
    model_tools = FrozenToolCatalog(prepared.snapshot).list_tools(run)
    assert [tool.name for tool in model_tools] == [
        deterministic_tool_name("broker-agent", skill_id)
        for skill_id in expected_skill_ids
    ]
    assert deterministic_tool_name("broker-agent") not in {
        tool.name for tool in model_tools
    }

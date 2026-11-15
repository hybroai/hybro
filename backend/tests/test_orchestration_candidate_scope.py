from types import SimpleNamespace

import pytest

from conversation.candidate_scope import (
    AuthorizationBasis,
    CandidateAgentSnapshot,
    CandidateScopeSnapshot,
    normalize_candidate_scope,
)


def test_normalize_candidate_scope_from_mapping_preserves_order_and_names():
    scope = normalize_candidate_scope(
        conversation_id="room-1",
        source="saved_group",
        group_id="group-1",
        selected_by_user_id="user-1",
        selected_agent_set={"agent-1": "Broker", "agent-2": "Insurer"},
    )

    assert scope.source == "saved_group"
    assert scope.conversation_id == "room-1"
    assert scope.group_id == "group-1"
    assert scope.agent_ids == ["agent-1", "agent-2"]
    assert [agent.name for agent in scope.agents] == ["Broker", "Insurer"]
    assert scope.authorization_basis.kind == "saved_group_member"


def test_normalize_candidate_scope_falls_back_to_agent_ids_when_agents_empty():
    scope = normalize_candidate_scope(
        conversation_id="room-1",
        source="explicit_selection",
        selected_agent_set={"agents": [], "agent_ids": ["agent-1"]},
    )

    assert scope.agent_ids == ["agent-1"]
    assert [agent.agent_id for agent in scope.agents] == ["agent-1"]


def test_normalize_all_agents_scope_records_all_active_authorization():
    scope = normalize_candidate_scope(
        conversation_id="room-1",
        source="all_agents",
        selected_by_user_id="user-1",
        selected_agent_set={"agent-1": "Broker", "agent-2": "Insurer"},
    )

    assert scope.source == "all_agents"
    assert scope.agent_ids == ["agent-1", "agent-2"]
    assert scope.authorization_basis is not None
    assert scope.authorization_basis.kind == "all_active_agents"


def test_normalize_candidate_scope_treats_string_as_single_agent_id():
    scope = normalize_candidate_scope(
        conversation_id="room-1",
        source="explicit_selection",
        selected_agent_set="agent-1",
    )

    assert scope.agent_ids == ["agent-1"]
    assert [agent.agent_id for agent in scope.agents] == ["agent-1"]


def test_normalize_candidate_scope_treats_single_object_as_one_agent():
    scope = normalize_candidate_scope(
        conversation_id="room-1",
        source="explicit_selection",
        selected_agent_set=SimpleNamespace(agent_id="agent-1", name="Agent One"),
    )

    assert scope.agent_ids == ["agent-1"]
    assert [agent.name for agent in scope.agents] == ["Agent One"]


def test_normalize_candidate_scope_partial_mapping_preserves_all_agent_ids():
    scope = normalize_candidate_scope(
        conversation_id="room-1",
        source="explicit_selection",
        selected_agent_set={
            "agent_ids": ["agent-a", "agent-b"],
            "agents": [
                {
                    "agent_id": "agent-a",
                    "name": "Broker",
                    "capability_summary": "Collects broker requirements.",
                    "status": "active",
                    "input_modes": ["text", "application/pdf"],
                    "output_modes": ["application/json"],
                    "supports_file_upload": True,
                }
            ],
        },
    )

    assert scope.agent_ids == ["agent-a", "agent-b"]
    assert [agent.agent_id for agent in scope.agents] == ["agent-a", "agent-b"]
    assert scope.agents[0].name == "Broker"
    assert scope.agents[0].capability_summary == "Collects broker requirements."
    assert scope.agents[0].status == "active"
    assert scope.agents[0].input_modes == ["text", "application/pdf"]
    assert scope.agents[0].output_modes == ["application/json"]
    assert scope.agents[0].supports_file_upload is True
    assert scope.agents[1].name is None


def test_normalize_candidate_scope_object_snapshot_preserves_all_agent_ids():
    snapshot = CandidateScopeSnapshot(
        snapshot_id="scope-1",
        revision=1,
        source="explicit_selection",
        conversation_id="room-1",
        agent_ids=["agent-a", "agent-b"],
        authorization_basis=AuthorizationBasis(kind="explicit_selection"),
        agents=[
            CandidateAgentSnapshot(
                agent_id="agent-a",
                name="Broker",
                capability_summary="Collects broker requirements.",
                status="active",
            )
        ],
    )

    scope = normalize_candidate_scope(
        conversation_id="room-1",
        source="explicit_selection",
        selected_agent_set=snapshot,
    )

    assert scope.agent_ids == ["agent-a", "agent-b"]
    assert [agent.agent_id for agent in scope.agents] == ["agent-a", "agent-b"]
    assert scope.agents[0].name == "Broker"
    assert scope.agents[0].capability_summary == "Collects broker requirements."
    assert scope.agents[0].status == "active"
    assert scope.agents[1].name is None


def test_normalize_candidate_scope_agent_object_preserves_metadata():
    scope = normalize_candidate_scope(
        conversation_id="room-1",
        source="explicit_selection",
        selected_agent_set=SimpleNamespace(
            agent_id="agent-1",
            agent_name="Broker",
            description="Handles broker intake.",
            capabilities=["quotes", "risk"],
            input_modes=["text", "application/pdf"],
            output_modes=["application/json"],
            supports_file_upload=True,
            success_rate=0.8,
            is_healthy=False,
        ),
    )

    assert scope.agent_ids == ["agent-1"]
    assert scope.agents[0].name == "Broker"
    assert scope.agents[0].capability_summary == "Handles broker intake."
    assert scope.agents[0].status == "inactive"
    assert scope.agents[0].capabilities == ["quotes", "risk"]
    assert scope.agents[0].input_modes == ["text", "application/pdf"]
    assert scope.agents[0].output_modes == ["application/json"]
    assert scope.agents[0].supports_file_upload is True
    assert scope.agents[0].success_rate == 0.8


def test_normalize_candidate_scope_rejects_unknown_source():
    with pytest.raises(ValueError, match="unsupported candidate scope source"):
        normalize_candidate_scope(
            conversation_id="room-1",
            source="saved_groups",
            selected_agent_set=["agent-1"],
        )

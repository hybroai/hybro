"""Exact method inventories of module-local ``protocols`` modules.

``tests/test_common_foundation.py`` pins ``common.protocols``. This test pins
the protocols that modules publish beside their facades, with the same
completeness check: every exported Protocol must be listed here.
"""

from __future__ import annotations

import importlib
import inspect

import pytest

from tests.test_common_foundation import _assert_methods, _assert_params

EXPECTED_METHODS = {
    "agent.protocols": {
        "AgentCapabilityIssueStore": {
            "get_issue_by_id",
            "get_issues_for_agent",
            "resolve_all_for_agent",
            "resolve_issue",
        },
        "AgentCatalog": {
            "delete",
            "get_visible",
            "list_for_provider",
            "list_visible",
            "register",
            "resolve_card",
        },
        "AgentGroupStore": {
            "add_agent_group",
            "delete_agent_group",
            "get_agent_group_by_id",
            "get_agent_groups_by_owner",
            "update_agent_group",
        },
        "AgentInspection": {"inspect_a2a_connection", "inspect_agent_card"},
        "AgentNetworkAccess": {"discover", "get_profile", "list_groups", "search"},
        "AgentSuggestionService": {"suggest_agents"},
    },
    "approval.protocols": {
        "HITLDeliveryPort": {"emit"},
        "HITLLifecyclePersistencePort": {
            "attach_interaction_request",
            "claim_interaction_application",
            "claim_resume_command",
            "claim_run_answer_projection",
            "create_resume_command",
            "ensure_indexes",
            "get_interaction_for_request_strict",
            "get_interaction_strict",
            "get_resume_command_for_interaction_strict",
            "get_resume_command_strict",
            "iter_active_interactions",
            "iter_due_interactions",
            "iter_due_resume_commands",
            "iter_materializing_interactions",
            "iter_stale_applications",
            "iter_unreconciled_terminal_interactions",
            "iter_unreconciled_terminal_requests",
            "mark_interaction_application_state",
            "mark_interaction_terminal_reconciled",
            "mark_resume_command_aggregate_applied",
            "mark_resume_command_state",
            "mark_run_answer_projection",
            "materialize_interaction",
            "reclaim_stale_resume_command",
            "record_interaction_answer",
            "renew_interaction_application",
            "renew_resume_command",
            "renew_run_answer_projection",
            "resume_uncertain_interaction",
            "terminalize_interaction",
        },
        "HITLPersistencePort": {
            "cas_update_hitl_request",
            "cas_update_hitl_request_strict",
            "claim_hitl_open_projection",
            "claim_hitl_request",
            "complete_hitl_open_projection",
            "count_hitl_requests_for_message",
            "create_hitl_request",
            "create_or_reuse_pending_hitl_request",
            "fenced_update_hitl_request",
            "find_pending_hitl_request_for_agent_message",
            "get_hitl_request",
            "get_pending_hitl_requests",
            "get_pending_hitl_requests_for_message",
            "get_pending_hitl_requests_strict",
            "get_room_agent_message_by_message_id",
            "get_room_user_message_by_message_id",
            "persist_hitl_interaction_metadata",
            "persist_hitl_request_id_on_message",
            "persist_hitl_user_answer",
            "persist_pending_hitl_on_agent_message",
            "release_hitl_open_projection",
            "reset_last_notified_state",
            "resolve_client_request_id_for_message_id",
            "update_agent_message_task_state",
            "update_hitl_request",
        },
        "HITLTerminalLifecyclePort": {"terminalize_owning_run"},
    },
    "local_agents.protocols": {
        "LocalAgentDiscovery": {"request_discovery"},
        "LocalAgentWriter": {
            "list_local_agent_ids",
            "mark_local_agents_inactive",
            "prune_inactive_local_agents",
            "upsert_local_agent",
        },
    },
    "conversation.protocols": {
        "ClientRequestIdResolver": {
            "resolve_client_request_id_for_agent_message",
            "resolve_client_request_id_for_message_id",
        },
        "ConversationMessageWriter": {
            "persist_final_agent_message",
            "persist_hitl_interaction_metadata",
            "persist_hitl_request_id_on_message",
            "persist_hitl_user_answer",
            "persist_pending_hitl_on_agent_message",
            "update_agent_message_task_state",
        },
    },
    "diagnostics.protocols": {
        "DiagnosticsAccess": {
            "can_read",
            "token_matches",
            "snapshot",
            "events",
            "logs",
        },
    },
    "chat.protocols": {
        "ChatRooms": {
            "create_room",
            "delete_room",
            "get_room",
            "list_room_history",
            "list_rooms_for_owner",
            "read_messages",
            "rename_room",
            "update_history_fields",
            "update_room_default_mode",
        },
    },
    "session.protocols": {
        "ConversationEventsPort": {"latest_seq", "read_range"},
        "RoomManagement": {
            "create_room",
            "delete_room",
            "update_membership",
            "update_room",
        },
        "RoomMessageStore": {
            "get_message",
            "save_agent_message",
            "save_user_message",
            "update_agent_message_status",
        },
        "RoomRegistry": {"get_room", "get_room_agents", "get_room_owner"},
        "SessionAccess": {"check", "close", "get_record", "list_sessions", "send"},
        "SessionService": {
            "check",
            "close",
            "list_for_owner",
            "record_for_owner",
            "send",
        },
        "SessionStore": {
            "get",
            "list_for_owner",
            "list_open_conversations",
            "mark_closed",
            "record_accepted_agent",
            "record_send",
            "reserve",
            "set_conversation_if_unset",
        },
    },
}


@pytest.mark.parametrize("module_name", sorted(EXPECTED_METHODS))
def test_module_protocol_methods_match_inventory(module_name):
    module = importlib.import_module(module_name)
    expected = EXPECTED_METHODS[module_name]

    for protocol_name, methods in expected.items():
        _assert_methods(getattr(module, protocol_name), methods)

    exported_protocols = {
        name
        for name in module.__all__
        if inspect.isclass(getattr(module, name))
        and getattr(getattr(module, name), "_is_protocol", False)
    }
    assert exported_protocols == set(expected)


# Ports a module declares beside its single consumer rather than in its
# ``protocols`` module.
CONSUMER_PORT_METHODS = {
    ("context_memory.ports", "ContentStorageRepository"): {
        "delete_content_by_room_id",
        "delete_content_by_turn_id",
        "get_content_by_turn_id",
        "upsert_full_content",
    },
    ("context_memory.ports", "MemoryRepository"): {
        "compact_turns_bulk",
        "create_room_memory",
        "delete_room_memory",
        "ensure_room_memory",
        "get_room_memory",
        "list_room_ids_with_memory",
        "push_and_trim_conversation_turn_if_absent",
        "upsert_room_memory",
    },
    ("agent.facade", "AgentRepository"): {
        "activate_agents",
        "delete",
        "delete_inactive_ownerless",
        "find_by_normalized_url",
        "get_by_id",
        "get_by_ids",
        "get_by_provider",
        "get_by_source",
        "get_public",
        "increment_agent_call_count",
        "list_active_agents",
        "list_visible",
        "mark_agents_inactive",
        "public_url_exists",
        "text_search",
        "update",
        "update_health",
        "update_where",
        "upsert",
    },
    ("conversation.attachments", "AttachmentMetadataReader"): {"get_for_room_file"},
    ("conversation.deletion", "RoomMemoryCleanupPort"): {"delete_room_memory"},
    ("conversation.facade", "CanonicalAgentCallReader"): {"agent_call_exists"},
    ("conversation.facade", "MessageRepository"): {
        "count_agent_messages",
        "delete_for_room",
        "get_agent_message_by_id",
        "get_agent_messages_by_related_message_id",
        "get_agent_messages_for_room",
        "get_by_id",
        "get_by_ids",
        "get_for_room",
        "get_thread",
        "get_timeline_page",
        "get_user_message_by_id",
        "get_user_message_by_idempotency_key",
        "get_user_messages_for_room",
        "insert_user_message_idempotently",
        "save_agent_message",
        "save_user_message",
        "update_agent_message",
        "update_agent_message_if_not_terminal",
        "update_status",
        "update_user_message",
    },
    ("conversation.membership", "RoomMembershipSeedSource"): {
        "get_saved_group",
        "list_current_agents",
    },
    ("delivery.types", "SSETransport"): {
        "broadcast_frame_to_room",
        "connect",
        "disconnect",
        "set_draining",
    },
    ("execution.orchestrator.a2a_runtime.call_counting", "AgentCallCounter"): {
        "increment_agent_call_count"
    },
    ("llm_gateway.gateway", "LLMProviderAdapter"): {
        "embed",
        "embed_batch",
        "generate",
        "generate_structured",
    },
    ("room_files.ports", "RoomLifecycleStore"): {
        "acquire_write_lease",
        "begin_deletion",
        "delete_room_in_deletion",
        "get_active_room_owner",
        "has_live_write_leases",
        "has_valid_write_lease",
        "list_stale_deletions",
        "release_write_lease",
        "renew_write_lease",
        "set_deletion_phase",
    },
    ("room_files.service", "RoomStateOwner"): {"delete_room_state"},
}


@pytest.mark.parametrize(
    ("module_name", "protocol_name"), sorted(CONSUMER_PORT_METHODS)
)
def test_consumer_port_methods_match_inventory(module_name, protocol_name):
    module = importlib.import_module(module_name)
    _assert_methods(
        getattr(module, protocol_name),
        CONSUMER_PORT_METHODS[(module_name, protocol_name)],
    )


def test_moved_port_signatures_are_pinned():
    from agent.facade import AgentRepository
    from session.protocols import RoomManagement

    _assert_params(
        AgentRepository.list_visible,
        ["self", "user_id", "active_only", "agent_ids", "query", "limit", "exhaust"],
    )
    _assert_params(RoomManagement.create_room, ["self", "request"])

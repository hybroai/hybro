"""C8: compatibility and legacy tolerances are removed once nothing needs them."""

from __future__ import annotations

import inspect

import pytest


def test_health_body_drops_the_legacy_redis_key():
    from main import compute_health_status

    result = compute_health_status(
        delivery_pubsub_connected=True,
        delivery_kv_connected=True,
        redis_runtime_connected=True,
        redis_url="redis://localhost:6379/0",
        agent_search_index_ready=True,
    )

    assert "legacy_redis_service_connected" not in result["body"]
    assert result["body"]["redis_service_connected"] is True


@pytest.mark.parametrize(
    "name", ["store", "database" + "_service", "db" + "_service", "a2a_service"]
)
def test_hitl_factory_rejects_retired_arguments_without_an_alias_list(name):
    from approval import factory

    with pytest.raises(TypeError, match="unexpected argument"):
        factory.create_hitl_service(**{name: object()})
    assert "legacy" not in inspect.getsource(factory)


def test_turn_notes_json_has_no_retired_provider_fallback():
    from common.utils import context_utils

    assert "call_supervisor_llm_json" not in inspect.getsource(context_utils)


def test_dead_message_claim_methods_are_gone():
    from conversation.repository import ConversationMessageStore, messages

    for name in (
        "claim_or_reclaim_user_message",
        "get_stale_claimed_orchestration_messages",
        "claim_user_message_for_processing",
        "unclaim_user_message",
    ):
        assert not hasattr(ConversationMessageStore, name), name
    assert not hasattr(messages, "_legacy_claim_threshold_text")


@pytest.mark.asyncio
async def test_normalized_url_lookup_reads_only_the_indexed_field():
    from unittest.mock import AsyncMock, MagicMock

    from agent.repository import mongo as agent_mongo
    from agent.repository.mongo import AgentMongoRepository

    collection = MagicMock()
    collection.find_one = AsyncMock(return_value=None)
    collection.find = AsyncMock(return_value=[])
    database = MagicMock()
    database.collection.return_value = collection

    assert await AgentMongoRepository(database).find_by_normalized_url("u") is None
    collection.find_one.assert_awaited_once_with({"normalized_url": "u"})
    collection.find.assert_not_awaited()
    assert not hasattr(agent_mongo, "_LEGACY_NORMALIZED_URL_SCAN_LIMIT")


def test_event_envelope_requires_a_timestamp():
    from pydantic import ValidationError

    from common.eventing.models import EventEnvelope

    assert not hasattr(EventEnvelope, "hydrate_legacy_timestamp")
    with pytest.raises(ValidationError):
        EventEnvelope.model_validate(
            {"origin": "o", "event_type": "t", "event": {"timestamp": "x"}}
        )


def test_agent_group_store_is_a_plain_repository_port():
    import agent.protocols as protocols
    from agent.repository import AgentRuntimeStore

    assert not hasattr(protocols, "AgentGroup" + "StoreCompatibility")
    assert "AgentGroupStore" in protocols.__all__
    for method in (
        "add_agent_group",
        "delete_agent_group",
        "get_agent_group_by_id",
        "get_agent_groups_by_owner",
        "update_agent_group",
    ):
        assert hasattr(protocols.AgentGroupStore, method), method
        assert hasattr(AgentRuntimeStore, method), method


def test_conversation_runtime_is_built_per_application_not_a_singleton():
    import conversation
    import conversation.runtime as runtime_module
    from conversation.runtime import ConversationRuntime

    assert not hasattr(runtime_module, "conversation_runtime")
    assert "conversation_runtime" not in conversation.__all__
    assert not hasattr(ConversationRuntime, "reset_bindings")


def test_container_validates_the_lifespan_runtime_from_app_state():
    from types import SimpleNamespace

    import container

    app = SimpleNamespace(state=SimpleNamespace())
    with pytest.raises(RuntimeError, match="app.state.conversation_runtime"):
        container.validate_runtime_bindings(app)


def test_container_requires_the_room_event_store_and_snapshot_service():
    from types import SimpleNamespace

    import container

    app = SimpleNamespace(state=SimpleNamespace())
    with pytest.raises(RuntimeError) as exc_info:
        container.validate_runtime_bindings(app)

    assert "app.state.room_event_store" in str(exc_info.value)
    assert "app.state.room_snapshot_service" in str(exc_info.value)

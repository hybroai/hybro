"""C5 step 4: each owner declares its indexes; the container only calls them."""

from __future__ import annotations

import inspect

import pytest


@pytest.mark.parametrize(
    "name",
    [
        "_create_index",
        "_ensure_text_index",
        "_ensure_agent_indexes",
        "_ensure_agent_group_indexes",
        "_ensure_context_memory_indexes",
        "_ensure_capability_issue_indexes",
        "_ensure_orchestrator_indexes",
        "_ensure_room_quote_indexes",
        "_ensure_room_history_indexes",
        "_ensure_user_message_indexes",
        "_user_message_index_readiness_issues",
        "_ensure_room_timeline_indexes",
        "_ensure_room_agent_message_indexes",
        "_ensure_room_file_indexes",
    ],
)
def test_container_no_longer_defines_index_helpers(name):
    import container

    assert not hasattr(container, name)


def test_shared_index_helpers_live_in_common():
    from common.utils.mongo_indexes import create_index, ensure_text_index

    assert inspect.iscoroutinefunction(create_index)
    assert inspect.iscoroutinefunction(ensure_text_index)


def _owners():
    from agent.repository import AgentMongoRepository, AgentRuntimeStore
    from agent.repository.capability_issue_mongo import (
        AgentCapabilityIssueMongoRepository,
    )
    from approval.repository import HITLLifecycleStore, HITLRequestStore
    from context_memory.repository import (
        ContentStorageMongoRepository,
        MemoryMongoRepository,
    )
    from conversation.repository import (
        MessageMongoRepository,
        RoomMongoRepository,
        RoomQuoteMongoRepository,
    )
    from diagnostics.repository import DiagnosticsMongoRepository

    return (
        AgentMongoRepository,
        AgentRuntimeStore,
        AgentCapabilityIssueMongoRepository,
        ContentStorageMongoRepository,
        MemoryMongoRepository,
        RoomQuoteMongoRepository,
        RoomMongoRepository,
        MessageMongoRepository,
        HITLRequestStore,
        HITLLifecycleStore,
        DiagnosticsMongoRepository,
    )


def test_each_owning_repository_has_ensure_indexes():
    for owner in _owners():
        assert inspect.iscoroutinefunction(owner.ensure_indexes), owner


def test_hitl_stores_use_the_common_method_name():
    from approval.repository import HITLLifecycleStore, HITLRequestStore

    assert not hasattr(HITLRequestStore, "ensure_hitl_indexes")
    assert not hasattr(HITLLifecycleStore, "ensure_hitl_lifecycle_indexes")


def test_hitl_lifecycle_port_declares_the_common_method_name():
    from approval.protocols import HITLLifecyclePersistencePort

    assert hasattr(HITLLifecyclePersistencePort, "ensure_indexes")
    assert not hasattr(HITLLifecyclePersistencePort, "ensure_hitl_lifecycle_indexes")


def test_store_level_index_functions_live_with_their_stores():
    from execution.repository.orchestrator.indexes import (
        ensure_orchestrator_indexes,
    )
    from room_files import ensure_room_file_indexes

    assert inspect.iscoroutinefunction(ensure_orchestrator_indexes)
    assert inspect.iscoroutinefunction(ensure_room_file_indexes)


def test_container_only_calls_the_owners():
    import container

    source = inspect.getsource(container.ensure_runtime_indexes)

    assert "create_index(" not in source
    assert source.count("ensure_indexes()") == 9
    assert "ensure_orchestrator_indexes(" in source
    assert "ensure_room_file_indexes(" in source

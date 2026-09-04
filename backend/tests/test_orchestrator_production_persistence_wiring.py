"""Focused tests for step-3 orchestrator persistence wiring in container.py."""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from dal.orchestrator.epoch_bindings import (
    EpochScopedOrchestratorCleanup,
    bind_room_epoch_store,
    require_room_epoch_store,
    reset_room_epoch_store,
)
from dal.orchestrator.stores import MongoRoomEpochStore
from execution.orchestrator.a2a_runtime.persistence import A2A_RUNTIME_COLLECTIONS
from execution.orchestrator.persistence import ORCHESTRATOR_COLLECTIONS

ROOT = Path(__file__).resolve().parents[1]

ROOM_OWNED_ORCHESTRATOR_COLLECTIONS = {
    "orchestrator_runs",
    "orchestrator_run_events",
    "orchestrator_agent_tool_bindings",
    "orchestrator_agent_calls",
    "orchestrator_a2a_observations",
    "orchestrator_a2a_observation_conflicts",
    "orchestrator_room_epochs",
}


def _has_create_index(collection: MagicMock, keys, **kwargs) -> bool:
    return any(
        call.args == (keys,) and call.kwargs == kwargs
        for call in collection.create_index.call_args_list
    )


@pytest.mark.asyncio
async def test_ensure_orchestrator_indexes_registers_exact_metadata_inventory():
    from container import _ensure_orchestrator_indexes

    collections: dict[str, MagicMock] = {}

    def _collection(name: str):
        if name not in collections:
            collection = MagicMock()
            collection.create_index = AsyncMock(return_value=f"{name}_idx")
            collection.drop_index = AsyncMock()
            collection.index_information = AsyncMock(return_value={})
            collections[name] = collection
        return collections[name]

    mongo = MagicMock()
    mongo.collection.side_effect = _collection

    await _ensure_orchestrator_indexes(mongo)

    expected_collections = {
        collection_definition.name
        for collection_definition in (
            *ORCHESTRATOR_COLLECTIONS,
            *A2A_RUNTIME_COLLECTIONS,
        )
    }
    assert set(collections) == expected_collections

    for collection_definition in (*ORCHESTRATOR_COLLECTIONS, *A2A_RUNTIME_COLLECTIONS):
        collection = collections[collection_definition.name]
        for index in collection_definition.indexes:
            kwargs = {"unique": index.unique, "name": index.name}
            if index.partial_filter is not None:
                kwargs["partialFilterExpression"] = dict(index.partial_filter)
            assert _has_create_index(
                collection,
                list(index.keys),
                **kwargs,
            ), f"{collection_definition.name}.{index.name}"


@pytest.mark.asyncio
async def test_ensure_orchestrator_indexes_removes_obsolete_active_room_index():
    from container import _ensure_orchestrator_indexes

    collections: dict[str, MagicMock] = {}

    def _collection(name: str):
        if name not in collections:
            collection = MagicMock()
            collection.create_index = AsyncMock(return_value=f"{name}_idx")
            collection.drop_index = AsyncMock()
            collection.index_information = AsyncMock(
                return_value=(
                    {"orchestrator_active_room_unique": {}}
                    if name == "orchestrator_runs"
                    else {}
                )
            )
            collections[name] = collection
        return collections[name]

    mongo = MagicMock()
    mongo.collection.side_effect = _collection

    await _ensure_orchestrator_indexes(mongo)

    runs = collections["orchestrator_runs"]
    runs.drop_index.assert_awaited_once_with("orchestrator_active_room_unique")
    assert _has_create_index(
        runs,
        [("room_id", 1)],
        unique=True,
        name="orchestrator_active_room_unique_canceling",
        partialFilterExpression={
            "status": {
                "$in": [
                    "queued",
                    "running",
                    "waiting_external",
                    "awaiting_user",
                    "canceling",
                    "finalizing",
                ]
            }
        },
    )


@pytest.mark.asyncio
async def test_ensure_orchestrator_indexes_fails_if_obsolete_index_cannot_be_removed():
    from container import _ensure_orchestrator_indexes

    runs = MagicMock()
    runs.index_information = AsyncMock(
        return_value={"orchestrator_active_room_unique": {}}
    )
    runs.drop_index = AsyncMock(side_effect=RuntimeError("drop failed"))
    mongo = MagicMock()
    mongo.collection.return_value = runs

    with pytest.raises(
        RuntimeError, match="obsolete orchestrator Run index removal failed"
    ):
        await _ensure_orchestrator_indexes(mongo)

    runs.create_index.assert_not_called()


def _room_owned_collection_names() -> set[str]:
    tree = ast.parse((ROOT / "container.py").read_text(), filename="container.py")
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        if node.func.id != "create_file_storage":
            continue
        for keyword in node.keywords:
            if keyword.arg != "room_owned_collections":
                continue
            list_comp = keyword.value
            assert isinstance(list_comp, ast.ListComp)
            assert len(list_comp.generators) == 1
            names = list_comp.generators[0].iter
            assert isinstance(names, ast.Tuple)
            return {
                element.value
                for element in names.elts
                if isinstance(element, ast.Constant) and isinstance(element.value, str)
            }
    raise AssertionError(
        "create_file_storage call with room_owned_collections not found"
    )


def test_orchestrator_collections_are_room_owned():
    names = _room_owned_collection_names()

    assert ROOM_OWNED_ORCHESTRATOR_COLLECTIONS <= names


def test_epoch_store_binding_is_bind_reset_and_fail_fast():
    with pytest.raises(RuntimeError, match="has not been bound"):
        require_room_epoch_store()

    first = MongoRoomEpochStore(MagicMock())
    second = MongoRoomEpochStore(MagicMock())
    bind_room_epoch_store(first)
    assert require_room_epoch_store() is first
    # Idempotent re-binding for lifespan restarts keeps the latest store.
    bind_room_epoch_store(second)
    assert require_room_epoch_store() is second

    reset_room_epoch_store()
    with pytest.raises(RuntimeError, match="has not been bound"):
        require_room_epoch_store()


@pytest.mark.asyncio
async def test_epoch_scoped_cleanup_deletes_every_collection_at_exact_epoch():
    stores = [
        SimpleNamespace(delete_by_epoch=AsyncMock(return_value=1)) for _ in range(5)
    ]
    runs = SimpleNamespace(
        delete_many=AsyncMock(return_value=SimpleNamespace(deleted_count=2))
    )
    cleanup = EpochScopedOrchestratorCleanup(
        bindings=stores[0],
        calls=stores[1],
        observations=stores[2],
        conflicts=stores[3],
        runs=runs,
        run_events=stores[4],
    )

    assert await cleanup.delete_by_epoch("room-1", 3) == 7
    for store in stores:
        store.delete_by_epoch.assert_awaited_once_with("room-1", 3)
    runs.delete_many.assert_awaited_once_with(
        {"room_id": "room-1", "request.room_epoch": 3}
    )

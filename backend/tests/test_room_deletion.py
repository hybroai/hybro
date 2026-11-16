from __future__ import annotations

import ast
import inspect
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from chat.service import ChatService
from conversation.deletion import (
    RoomDeletionLifecycle,
    RoomDeletionService,
    RoomFileDeletionLifecycle,
)
from tests.orchestrator_harness.a2a_in_memory import InMemoryRoomEpochStore

from ._orchestrator_helpers import NOW


def _room_lifecycle(*, owner: str | None = "owner", delete: bool = True):
    return SimpleNamespace(
        get_room_owner=AsyncMock(return_value=owner),
        cleanup_room_owned_data=AsyncMock(return_value={}),
        delete_room=AsyncMock(return_value=delete),
    )


def _file_lifecycle(*, deletion_id: str | None = "deletion-1", drained=True):
    return SimpleNamespace(
        begin_room_deletion=AsyncMock(return_value=deletion_id),
        wait_for_room_writes=AsyncMock(return_value=drained),
        set_deletion_phase=AsyncMock(return_value=True),
        delete_for_room=AsyncMock(return_value=0),
        delete_room_state=AsyncMock(return_value=True),
    )


def _memory_cleanup(*, result=True):
    return SimpleNamespace(delete_room_memory=AsyncMock(return_value=result))


def _service(*, room=None, files=None, memory=None):
    return RoomDeletionService(
        room_lifecycle=room or _room_lifecycle(),
        file_lifecycle=files or _file_lifecycle(),
        memory_cleanup=memory if memory is not None else _memory_cleanup(),
    )


@pytest.mark.parametrize(
    ("requesting_user_id", "owner", "status", "error"),
    [
        ("owner", None, 404, "Room not found"),
        ("intruder", "owner", 403, "Forbidden"),
    ],
)
async def test_request_and_owner_validation(requesting_user_id, owner, status, error):
    room = _room_lifecycle(owner=owner)

    response = await _service(room=room).delete_room_by_room_id(
        "room-1", requesting_user_id=requesting_user_id
    )

    assert response.success is False
    assert response.status_code == status
    assert response.error == error
    room.delete_room.assert_not_awaited()
    room.cleanup_room_owned_data.assert_not_awaited()


@pytest.mark.parametrize(
    ("deletion_id", "drained", "error"),
    [
        (None, True, "Room deletion could not be started"),
        ("deletion-1", False, "Room still has active writes"),
    ],
)
async def test_begin_or_write_drain_conflict_returns_409(deletion_id, drained, error):
    room = _room_lifecycle()
    files = _file_lifecycle(deletion_id=deletion_id, drained=drained)

    response = await _service(room=room, files=files).delete_room_by_room_id(
        "room-1", requesting_user_id="owner"
    )

    assert response.success is False
    assert response.status_code == 409
    assert response.error == error
    room.delete_room.assert_not_awaited()
    room.cleanup_room_owned_data.assert_not_awaited()


async def test_production_deletion_preserves_strict_phase_and_cleanup_order():
    events: list[str] = []

    def record(name, result):
        async def effect(*args, **kwargs):
            events.append(name)
            return result

        return effect

    room = _room_lifecycle()
    files = _file_lifecycle()
    memory = _memory_cleanup()
    room.get_room_owner.side_effect = record("owner", "owner")
    files.begin_room_deletion.side_effect = record("begin", "deletion-1")
    files.wait_for_room_writes.side_effect = record("drain", True)

    async def record_phase(conversation_id, deletion_id, phase):
        events.append(f"phase:{phase}")
        return True

    files.set_deletion_phase.side_effect = record_phase
    memory.delete_room_memory.side_effect = record("memory", True)
    files.delete_for_room.side_effect = record("files", 0)
    files.delete_room_state.side_effect = record("room-state", True)
    room.cleanup_room_owned_data.side_effect = record("owned-data", {})
    room.delete_room.side_effect = record("delete-room", True)

    response = await _service(
        room=room, files=files, memory=memory
    ).delete_room_by_room_id("room-1", requesting_user_id="owner")

    assert response.success is True
    assert response.status_code == 200
    assert events == [
        "owner",
        "begin",
        "drain",
        "phase:cleaning",
        "memory",
        "files",
        "room-state",
        "owned-data",
        "phase:finalizing",
        "delete-room",
    ]


async def test_deletion_deactivates_epoch_tombstone_survives_and_recreation_increments():
    epoch_store = InMemoryRoomEpochStore()
    await epoch_store.activate("room-1", "create-1", activated_at=NOW)
    epoch_cleanup = SimpleNamespace(delete_by_epoch=AsyncMock(return_value=1))
    room = _room_lifecycle()
    files = _file_lifecycle()

    service = RoomDeletionService(
        room_lifecycle=room,
        file_lifecycle=files,
        memory_cleanup=_memory_cleanup(),
        epoch_store=epoch_store,
        orchestrator_epoch_cleanup=epoch_cleanup,
    )

    response = await service.delete_room_by_room_id(
        "room-1", requesting_user_id="owner"
    )

    assert response.success is True
    assert await epoch_store.read_active("room-1") is None
    tombstone = await epoch_store.read("room-1")
    assert tombstone is not None
    assert tombstone.active is False
    assert tombstone.epoch == 1
    assert tombstone.deletion_id == "deletion-1"
    epoch_cleanup.delete_by_epoch.assert_awaited_once_with("room-1", 1)

    outcome, recreated = await epoch_store.activate(
        "room-1", "create-2", activated_at=NOW
    )
    assert outcome == "accepted"
    assert recreated.epoch == 2
    assert await epoch_store.verify_active("room-1", 2)
    assert not await epoch_store.verify_active("room-1", 1)


async def test_deletion_epoch_conflict_returns_409_before_cleanup():
    class ConflictingEpochStore(InMemoryRoomEpochStore):
        async def deactivate(
            self, conversation_id, epoch, deletion_id, *, deactivated_at
        ):
            return "conflict", None

    epoch_store = ConflictingEpochStore()
    await epoch_store.activate("room-1", "create-1", activated_at=NOW)
    room = _room_lifecycle()
    files = _file_lifecycle()
    service = RoomDeletionService(
        room_lifecycle=room,
        file_lifecycle=files,
        memory_cleanup=_memory_cleanup(),
        epoch_store=epoch_store,
    )

    response = await service.delete_room_by_room_id(
        "room-1", requesting_user_id="owner"
    )

    assert response.success is False
    assert response.status_code == 409
    assert response.error == "Room epoch deactivation failed"
    files.delete_for_room.assert_not_awaited()
    room.delete_room.assert_not_awaited()


@pytest.mark.parametrize("failure", ["memory", "files", "owned"])
async def test_cleanup_failure_stops_before_finalizing(failure):
    room = _room_lifecycle()
    files = _file_lifecycle()
    memory = _memory_cleanup()
    if failure == "memory":
        memory.delete_room_memory.return_value = False
    elif failure == "files":
        files.delete_for_room.side_effect = RuntimeError("file cleanup failed")
    else:
        room.cleanup_room_owned_data.side_effect = RuntimeError("owned cleanup failed")

    response = await _service(
        room=room, files=files, memory=memory
    ).delete_room_by_room_id("room-1", requesting_user_id="owner")

    assert response.success is False
    assert response.status_code == 500
    assert response.error == "Room cleanup is incomplete and will be retried"
    assert [call.args[2] for call in files.set_deletion_phase.await_args_list] == [
        "cleaning"
    ]
    room.delete_room.assert_not_awaited()
    # Cleanup attempts are intentionally independent so recovery can resume safely.
    files.delete_for_room.assert_awaited_once()
    room.cleanup_room_owned_data.assert_awaited_once_with("room-1")


async def test_final_room_delete_failure_preserves_finalizing_error():
    room = _room_lifecycle(delete=False)
    files = _file_lifecycle()

    response = await _service(room=room, files=files).delete_room_by_room_id(
        "room-1", requesting_user_id="owner"
    )

    assert response.success is False
    assert response.status_code == 500
    assert response.error == "Failed to finalize room deletion"
    assert [call.args[2] for call in files.set_deletion_phase.await_args_list] == [
        "cleaning",
        "finalizing",
    ]


async def test_chat_delete_is_thin_delegate_and_missing_binding_fails_fast():
    source = inspect.getsource(ChatService.delete_room)
    runtime = ChatService()

    assert "_require_room_deletion" in source
    assert "begin_room_deletion" not in source
    assert "cleanup_room_owned_data" not in source
    with pytest.raises(RuntimeError, match="room deletion service"):
        await runtime.delete_room("room-1", requesting_user_id="owner")

    service = SimpleNamespace(delete_room_by_room_id=AsyncMock(return_value="done"))
    runtime.bind_room_deletion(service)
    assert await runtime.delete_room("room-1", requesting_user_id="owner") == "done"
    service.delete_room_by_room_id.assert_awaited_once_with(
        "room-1", requesting_user_id="owner"
    )


def test_deletion_service_uses_only_narrow_ports_and_container_wires_them():
    service_source = Path("conversation/deletion.py").read_text()
    container_source = Path("container.py").read_text()

    assert "ConversationRuntime" not in service_source
    assert "ConversationFacade" not in service_source
    assert "RoomFiles" not in service_source
    assert "runtime_store" not in service_source
    assert set(RoomDeletionLifecycle.__dict__) >= {
        "get_room_owner",
        "cleanup_room_owned_data",
        "delete_room",
    }
    assert set(RoomFileDeletionLifecycle.__dict__) >= {
        "begin_room_deletion",
        "wait_for_room_writes",
        "set_deletion_phase",
        "delete_for_room",
        "delete_room_state",
    }
    assert "RoomDeletionService(" in container_source
    assert "chat_service.bind_room_deletion(room_deletion)" in container_source
    for method in (
        "get_room_owner",
        "cleanup_room_owned_data",
        "delete_room",
        "begin_room_deletion",
        "wait_for_room_writes",
        "set_deletion_phase",
        "delete_for_room",
        "delete_room_state",
    ):
        assert f"{method}=" in container_source


def test_room_deletion_extraction_does_not_add_import_cycle():  # noqa: C901
    package_roots = {
        path.name
        for path in Path(".").iterdir()
        if path.is_dir() and (path / "__init__.py").exists()
    }
    graph = {root: set() for root in package_roots}
    for root in package_roots:
        for path in Path(root).rglob("*.py"):
            tree = ast.parse(path.read_text(), filename=str(path))
            for node in ast.walk(tree):
                modules: list[str] = []
                if isinstance(node, ast.Import):
                    modules = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    modules = [node.module]
                graph[root].update(
                    module.split(".", 1)[0]
                    for module in modules
                    if module.split(".", 1)[0] in package_roots
                    and module.split(".", 1)[0] != root
                )

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str, trail: tuple[str, ...]) -> None:
        if node in visiting:
            raise AssertionError("package import cycle: " + " -> ".join((*trail, node)))
        if node in visited:
            return
        visiting.add(node)
        for dependency in graph[node]:
            visit(dependency, (*trail, node))
        visiting.remove(node)
        visited.add(node)

    for root in graph:
        visit(root, ())

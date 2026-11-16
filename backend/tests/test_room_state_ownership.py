"""C5 step 5: each module deletes its own room state; room_files only calls them."""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]


class _Collection:
    def __init__(self, name: str, rows: list[dict], *, sticky: bool = False) -> None:
        self.name = name
        self.rows = rows
        self.sticky = sticky
        self.deleted: list[dict] = []

    async def delete_many(self, query):
        self.deleted.append(query)
        if not self.sticky:
            self.rows = [row for row in self.rows if row["room_id"] != query["room_id"]]
        return len(self.rows)

    async def count(self, query):
        return sum(1 for row in self.rows if row["room_id"] == query["room_id"])


class _Mongo:
    def __init__(self, *, sticky: str | None = None) -> None:
        self.collections: dict[str, _Collection] = {}
        self.sticky = sticky

    def collection(self, name: str) -> _Collection:
        if name not in self.collections:
            self.collections[name] = _Collection(
                name,
                [{"room_id": "r1"}, {"room_id": "r2"}],
                sticky=name == self.sticky,
            )
        return self.collections[name]


def _owners(mongo: _Mongo):
    from approval.repository import HITLLifecycleStore, HITLRequestStore
    from context_memory.repository import (
        ContentStorageMongoRepository,
        MemoryMongoRepository,
    )
    from conversation.repository import MessageMongoRepository, RoomQuoteMongoRepository

    return {
        "conversation.messages": MessageMongoRepository(mongo),
        "conversation.quotes": RoomQuoteMongoRepository(mongo),
        "context_memory.memories": MemoryMongoRepository(mongo=mongo),
        "context_memory.content": ContentStorageMongoRepository(mongo=mongo),
        "approval.requests": HITLRequestStore(
            hitl_requests=mongo.collection("hitl_requests"), message_writer=object()
        ),
        "approval.lifecycle": HITLLifecycleStore(
            interactions=mongo.collection("hitl_interactions"),
            resume_commands=mongo.collection("hitl_resume_commands"),
            hitl_requests=mongo.collection("hitl_requests"),
        ),
    }


OWNED = {
    "conversation.messages": ("room_user_messages", "room_agent_messages"),
    "conversation.quotes": ("room_quotes",),
    "context_memory.memories": ("room_memories",),
    "context_memory.content": ("conversation_content",),
    "approval.requests": ("hitl_requests",),
    "approval.lifecycle": ("hitl_interactions", "hitl_resume_commands"),
}


def test_room_state_owner_port_is_shared():
    from room_files.service import RoomStateOwner

    assert inspect.iscoroutinefunction(RoomStateOwner.delete_room_state)


@pytest.mark.asyncio
@pytest.mark.parametrize("owner_key", sorted(OWNED))
async def test_each_owner_deletes_only_its_own_rows(owner_key):
    from room_files.service import RoomStateOwner

    mongo = _Mongo()
    owner = _owners(mongo)[owner_key]

    assert isinstance(owner, RoomStateOwner)
    assert await owner.delete_room_state("r1") is True
    touched = {
        name for name, collection in mongo.collections.items() if collection.deleted
    }
    assert touched == set(OWNED[owner_key])
    for name in OWNED[owner_key]:
        assert mongo.collections[name].rows == [{"room_id": "r2"}]


@pytest.mark.asyncio
async def test_owner_reports_rows_that_survive_deletion():
    mongo = _Mongo(sticky="room_agent_messages")
    owner = _owners(mongo)["conversation.messages"]

    assert await owner.delete_room_state("r1") is False
    assert mongo.collections["room_user_messages"].rows == [{"room_id": "r2"}]


class _Owner:
    def __init__(self, result: bool) -> None:
        self.result = result
        self.calls: list[str] = []

    async def delete_room_state(self, conversation_id: str) -> bool:
        self.calls.append(conversation_id)
        return self.result


@pytest.mark.asyncio
async def test_room_files_calls_every_bound_owner_and_combines_results():
    from room_files import RoomFiles
    from tests.fakes.room_files import MemoryFileContentStore

    files = RoomFiles(metadata=object(), content=MemoryFileContentStore())
    first, failing, last = _Owner(True), _Owner(False), _Owner(True)
    files.bind_room_state_owners((first, failing, last))

    assert await files.delete_room_state("r1") is False
    assert first.calls == failing.calls == last.calls == ["r1"]

    failing.result = True
    assert await files.delete_room_state("r1") is True


def test_room_files_no_longer_takes_collection_allowlists():
    from room_files import RoomFiles

    parameters = inspect.signature(RoomFiles).parameters
    assert "room_owned_collections" not in parameters
    assert "excluded_from_room_state_delete" not in parameters


def test_container_binds_the_owners_instead_of_collection_names():
    source = (BACKEND / "container.py").read_text()

    assert "room_owned_collections" not in source
    assert "excluded_from_room_state_delete" not in source
    assert "bind_room_state_owners(" in source


def test_orchestrator_collections_are_left_to_epoch_cleanup():
    source = (BACKEND / "container.py").read_text()
    start = source.index("bind_room_state_owners(")
    bound = source[start : source.index("\n            )\n", start)]

    assert "orchestrator" not in bound.lower()
    assert "Epoch" not in bound


@pytest.mark.asyncio
async def test_room_files_refuses_room_state_deletion_without_owners():
    from room_files import RoomFiles
    from tests.fakes.room_files import MemoryFileContentStore

    files = RoomFiles(metadata=object(), content=MemoryFileContentStore())

    with pytest.raises(RuntimeError, match="owners have not been bound"):
        await files.delete_room_state("r1")


def _room_files_sources() -> dict[str, str]:
    return {
        str(path.relative_to(BACKEND)): path.read_text()
        for path in sorted((BACKEND / "room_files").rglob("*.py"))
    }


def test_room_files_has_no_rooms_collection_access():
    """Conversation is the single writer of ``rooms``; room_files uses its port."""
    offenders = []
    for name, source in _room_files_sources().items():
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Constant) and node.value == "rooms":
                offenders.append(f"{name}:{node.lineno} 'rooms' literal")
            elif isinstance(node, ast.Attribute) and node.attr in {"rooms", "_rooms"}:
                offenders.append(f"{name}:{node.lineno} .{node.attr}")
            elif isinstance(node, ast.arg) and node.arg == "rooms":
                offenders.append(f"{name}:{node.lineno} rooms parameter")

    assert offenders == []


def test_room_files_takes_the_conversation_lifecycle_port():
    from room_files import RoomFiles, RoomLifecycleStore
    from room_files.leases import RoomWriteLeases

    parameters = inspect.signature(RoomFiles).parameters
    assert "rooms" not in parameters
    assert parameters["room_lifecycle"].annotation == (
        f"{RoomLifecycleStore.__name__} | None"
    )
    assert list(inspect.signature(RoomWriteLeases).parameters)[0] == "store"


def test_container_wires_room_files_with_the_conversation_repository():
    source = (BACKEND / "container.py").read_text()
    start = source.index("file_storage = create_file_storage(")
    call = source[start : source.index("\n            )\n", start)]

    assert "room_lifecycle=RoomMongoRepository(mongo_dal)" in call
    assert '"rooms"' not in call

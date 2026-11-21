"""The container binds the clients and hands them to the session facade."""

from __future__ import annotations

import inspect
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import container
from client import ClientService
from client.models import TokenRequirement
from client.protocols import ClientRegistry
from common.config.loader import Settings
from common.observability import events as E
from tests.fakes.client import MemoryClientStore


@pytest.mark.parametrize("mode", ["mock", "clerk"])
async def test_create_client_registry_binds_the_clients_collection(mode):
    collection = MagicMock()
    collection.insert_one = AsyncMock(return_value="id")
    mongo = SimpleNamespace(collection=lambda name: collection)

    clients = container.create_client_registry(
        settings=Settings(auth_mode=mode), mongo=mongo
    )

    assert isinstance(clients, ClientRegistry)
    created = await clients.create("CI bot")
    assert collection.insert_one.await_args.args[0]["client_id"] == created.client_id


def _mongo_with(present: set[str]):
    asked: list[str] = []

    def collection(name):
        asked.append(name)
        found = {"_id": "x"} if name in present else None
        return SimpleNamespace(find_one=AsyncMock(return_value=found))

    return SimpleNamespace(collection=collection), asked


@pytest.mark.parametrize(
    ("present", "mode", "required"),
    [
        (set(), "mock", True),
        ({"users"}, "mock", False),
        ({"rooms"}, "mock", False),
        ({"sessions"}, "mock", False),
        ({"rooms", "sessions"}, "clerk", True),
    ],
)
async def test_the_switch_is_seeded_from_whether_the_install_has_data(
    present, mode, required
):
    mongo, asked = _mongo_with(present)
    clients = ClientService(MemoryClientStore(), auth_mode=mode)

    seeded = await container.seed_token_requirement(clients=clients, mongo=mongo)

    assert seeded.required is required
    assert set(asked) <= {"users", "rooms", "sessions"}
    assert await clients.token_required() is required


async def test_a_switch_set_earlier_is_kept_at_the_next_start():
    mongo, _ = _mongo_with(set())
    clients = ClientService(MemoryClientStore(), auth_mode="mock")
    await container.seed_token_requirement(clients=clients, mongo=mongo)
    await clients.set_token_required(False)

    # Data exists now, and the owner's choice stays.
    mongo, _ = _mongo_with({"sessions"})
    again = await container.seed_token_requirement(clients=clients, mongo=mongo)

    assert again.required is False
    assert again.started_off is False


async def test_the_seed_reports_clerk_mode_as_on_whatever_was_stored():
    # Seeded off in mock mode earlier; Clerk mode always requires a token, so
    # the startup warning must not fire.
    mongo, _ = _mongo_with({"sessions"})
    store = MemoryClientStore()
    await container.seed_token_requirement(
        clients=ClientService(store, auth_mode="mock"), mongo=mongo
    )

    seeded = await container.seed_token_requirement(
        clients=ClientService(store, auth_mode="clerk"), mongo=mongo
    )

    assert store.requirement.required is False
    assert seeded.required is True


def _requirement(required: bool) -> TokenRequirement:
    return TokenRequirement(
        required=required, started_off=not required, updated_at=datetime.now(UTC)
    )


@pytest.mark.parametrize(
    ("required", "listen", "warned"),
    [
        (False, "public", True),
        (True, "public", False),
        (False, "loopback", False),
        (False, None, False),
    ],
)
def test_the_backend_warns_once_when_a_public_listener_needs_no_token(
    required, listen, warned, monkeypatch
):
    emitted = []
    monkeypatch.setattr(
        container.logger, "emit", lambda event, **fields: emitted.append(event)
    )

    container.warn_if_tokens_off_on_public_listener(_requirement(required), listen)

    expected = [E.CLIENT_TOKEN_NOT_REQUIRED_ON_PUBLIC_LISTENER] if warned else []
    assert emitted == expected


def test_the_lifespan_seeds_the_switch_before_serving_and_then_warns():
    source = inspect.getsource(container._runtime_lifespan)

    seed = source.index("await seed_token_requirement(")
    assert source.index("app.state.client_registry = create_client_registry(") < seed
    assert seed < source.index("warn_if_tokens_off_on_public_listener(")
    assert "app.state.instance_settings.listen()" in source


def test_lifespan_binds_resets_and_shares_the_clients():
    source = inspect.getsource(container._runtime_lifespan)

    assert "app.state.client_registry = create_client_registry(" in source
    assert source.count("app.state.client_registry = None") == 2
    assert "clients=app.state.client_registry" in source
    assert source.index("app.state.client_registry = create_client_registry(") < (
        source.index("clients=app.state.client_registry")
    )


def test_the_client_repository_creates_its_indexes_with_the_other_owners():
    source = inspect.getsource(container.ensure_runtime_indexes)

    assert "await MongoClientRepository(mongo).ensure_indexes()" in source

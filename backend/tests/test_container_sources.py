"""The container binds the sources and hands them to the session facade."""

from __future__ import annotations

import inspect
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import container
from common.config.loader import Settings
from common.observability import events as E
from source import SourceService
from source.models import TokenRequirement
from source.protocols import SourceRegistry
from tests.fakes.source import MemorySourceStore


@pytest.mark.parametrize("mode", ["mock", "clerk"])
async def test_create_source_registry_binds_the_sources_collection(mode):
    collection = MagicMock()
    collection.insert_one = AsyncMock(return_value="id")
    mongo = SimpleNamespace(collection=lambda name: collection)

    sources = container.create_source_registry(
        settings=Settings(auth_mode=mode), mongo=mongo
    )

    assert isinstance(sources, SourceRegistry)
    created = await sources.create("CI bot")
    assert collection.insert_one.await_args.args[0]["source_id"] == created.source_id


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
    sources = SourceService(MemorySourceStore(), auth_mode=mode)

    seeded = await container.seed_token_requirement(sources=sources, mongo=mongo)

    assert seeded.required is required
    assert set(asked) <= {"users", "rooms", "sessions"}
    assert await sources.token_required() is required


async def test_a_switch_set_earlier_is_kept_at_the_next_start():
    mongo, _ = _mongo_with(set())
    sources = SourceService(MemorySourceStore(), auth_mode="mock")
    await container.seed_token_requirement(sources=sources, mongo=mongo)
    await sources.set_token_required(False)

    # Data exists now, and the owner's choice stays.
    mongo, _ = _mongo_with({"sessions"})
    again = await container.seed_token_requirement(sources=sources, mongo=mongo)

    assert again.required is False
    assert again.started_off is False


async def test_the_seed_reports_clerk_mode_as_on_whatever_was_stored():
    # Seeded off in mock mode earlier; Clerk mode always requires a token, so
    # the startup warning must not fire.
    mongo, _ = _mongo_with({"sessions"})
    store = MemorySourceStore()
    await container.seed_token_requirement(
        sources=SourceService(store, auth_mode="mock"), mongo=mongo
    )

    seeded = await container.seed_token_requirement(
        sources=SourceService(store, auth_mode="clerk"), mongo=mongo
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

    expected = [E.SOURCE_TOKEN_NOT_REQUIRED_ON_PUBLIC_LISTENER] if warned else []
    assert emitted == expected


def test_the_lifespan_seeds_the_switch_before_serving_and_then_warns():
    source = inspect.getsource(container._runtime_lifespan)

    seed = source.index("await seed_token_requirement(")
    assert source.index("app.state.source_registry = create_source_registry(") < seed
    assert seed < source.index("warn_if_tokens_off_on_public_listener(")
    assert "app.state.instance_settings.listen()" in source


def test_lifespan_binds_resets_and_shares_the_sources():
    source = inspect.getsource(container._runtime_lifespan)

    assert "app.state.source_registry = create_source_registry(" in source
    assert source.count("app.state.source_registry = None") == 2
    assert "sources=app.state.source_registry" in source
    assert source.index("app.state.source_registry = create_source_registry(") < (
        source.index("sources=app.state.source_registry")
    )


def test_the_source_repository_creates_its_indexes_with_the_other_owners():
    source = inspect.getsource(container.ensure_runtime_indexes)

    assert "await MongoSourceRepository(mongo).ensure_indexes()" in source

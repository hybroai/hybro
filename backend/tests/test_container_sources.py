"""The container binds the sources and hands them to the session facade."""

from __future__ import annotations

import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import container
from common.config.loader import Settings
from source.protocols import SourceRegistry


@pytest.mark.parametrize(("mode", "required"), [("mock", False), ("clerk", True)])
async def test_create_source_registry_follows_the_auth_mode(mode, required):
    collection = MagicMock()
    collection.insert_one = AsyncMock(return_value="id")
    mongo = SimpleNamespace(collection=lambda name: collection)

    sources = container.create_source_registry(
        settings=Settings(auth_mode=mode), mongo=mongo
    )

    assert isinstance(sources, SourceRegistry)
    assert await sources.token_required() is required
    created = await sources.create("CI bot")
    assert collection.insert_one.await_args.args[0]["source_id"] == created.source_id


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

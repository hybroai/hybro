"""The container binds the owner account and the machine token."""

from __future__ import annotations

import inspect
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import container
from account.protocols import OwnerAccount, OwnerCreate
from common.config.loader import Settings
from orchestrator_composition import configured_public_secret_values
from tests.fakes.account import FAST_SCRYPT


def _mongo() -> SimpleNamespace:
    collection = MagicMock()
    collection.find_one = AsyncMock(return_value=None)
    collection.insert_one = AsyncMock(return_value="id")
    collection.create_index = AsyncMock(return_value="idx")
    return SimpleNamespace(collection=lambda name: collection, created=collection)


@pytest.mark.asyncio
async def test_create_owner_account_uses_the_configured_identity_rule():
    mongo = _mongo()
    settings = Settings(auth_mode="clerk", admin_user_ids=["user_admin"])

    account = container.create_owner_account(
        settings=settings, mongo=mongo, params=FAST_SCRYPT
    )
    profile = await account.create_owner(
        OwnerCreate(name="Ada", email="ada@example.com", password="correct horse")
    )

    assert isinstance(account, OwnerAccount)
    assert profile.user_id == "user_admin"
    assert mongo.created.insert_one.await_args.args[0]["user_id"] == "user_admin"


def test_the_account_repository_creates_its_indexes_with_the_other_owners():
    source = inspect.getsource(container.ensure_runtime_indexes)

    assert "await MongoAccountRepository(mongo).ensure_indexes()" in source


def test_lifespan_binds_and_resets_the_owner_account_and_the_machine_token():
    source = inspect.getsource(container._runtime_lifespan)

    assert "app.state.owner_account = create_owner_account(" in source
    assert (
        "app.state.machine_token = read_token(\n"
        "                runtime.settings.machine_handoff_file\n"
        "            )"
    ) in source or (
        "app.state.machine_token = read_token(runtime.settings.machine_handoff_file)"
        in source
    )
    assert source.count("app.state.owner_account = None") == 2
    assert source.count('app.state.machine_token = ""') == 2


def test_the_machine_token_is_a_public_secret_value(tmp_path):
    handoff = tmp_path / "machine.json"
    handoff.write_text(json.dumps({"token": "d" * 64}))
    settings = Settings(machine_handoff_file=str(handoff))

    assert "d" * 64 in configured_public_secret_values(settings)
    assert str(handoff) not in configured_public_secret_values(settings)


def test_an_unusable_machine_handoff_file_adds_no_secret_value(tmp_path):
    settings = Settings(machine_handoff_file=str(tmp_path / "missing.json"))

    assert configured_public_secret_values(settings) == (
        configured_public_secret_values(Settings())
    )

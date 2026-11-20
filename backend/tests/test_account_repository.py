"""MongoAccountRepository against mocked collection adapters."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from pymongo.errors import DuplicateKeyError

from account.models import PasswordHash, UserRecord, UserSessionRecord
from account.protocols import AccountExists, AccountStore
from account.repository import MongoAccountRepository
from account.repository.mongo import SESSION_IDLE_SECONDS

NOW = datetime(2030, 1, 1, tzinfo=UTC)
HASH = PasswordHash(n=16, r=1, p=1, salt="c2FsdA==", hash="aGFzaA==")


def _collection() -> MagicMock:
    collection = MagicMock()
    collection.insert_one = AsyncMock(return_value="id")
    collection.find_one = AsyncMock(return_value=None)
    collection.find_one_and_update = AsyncMock(return_value=None)
    collection.find = AsyncMock(return_value=[])
    collection.update_one = AsyncMock(return_value=True)
    collection.delete_one = AsyncMock(return_value=True)
    collection.delete_many = AsyncMock(return_value=0)
    collection.create_index = AsyncMock(return_value="idx")
    return collection


def _repository() -> tuple[MongoAccountRepository, dict[str, MagicMock]]:
    collections = {
        name: _collection() for name in ("users", "user_sessions", "setup_codes")
    }
    mongo = SimpleNamespace(collection=lambda name: collections[name])
    return MongoAccountRepository(mongo), collections


def _owner(**overrides) -> UserRecord:
    values = {
        "user_id": "user_local_developer",
        "name": "Ada",
        "email": "ada@example.com",
        "password": HASH,
        "password_id": "p1",
        "created_at": NOW,
        "updated_at": NOW,
    }
    values.update(overrides)
    return UserRecord(**values)


def _session(**overrides) -> UserSessionRecord:
    values = {
        "session_id": "s1",
        "user_id": "user_local_developer",
        "token_hash": "t" * 64,
        "created_at": NOW,
        "last_used_at": NOW,
        "user_agent": "Firefox",
    }
    values.update(overrides)
    return UserSessionRecord(**values)


def test_the_repository_satisfies_the_store_protocol():
    repository, _ = _repository()

    assert isinstance(repository, AccountStore)


@pytest.mark.asyncio
async def test_ensure_indexes_allows_one_owner_and_expires_idle_sessions():
    repository, collections = _repository()

    await repository.ensure_indexes()

    def created(name: str) -> dict:
        return {
            call.kwargs["name"]: (call.args[0], call.kwargs)
            for call in collections[name].create_index.await_args_list
        }

    users = created("users")
    assert users["user_id_unique"] == (
        [("user_id", 1)],
        {"name": "user_id_unique", "unique": True},
    )
    keys, options = users["single_owner"]
    assert keys == [("role", 1)]
    assert options["unique"] is True
    assert options["partialFilterExpression"] == {"role": "owner"}

    sessions = created("user_sessions")
    assert sessions["user_session_token_hash_unique"][1]["unique"] is True
    assert sessions["user_session_id_unique"][1]["unique"] is True
    keys, options = sessions["user_session_idle_expiry"]
    assert keys == [("last_used_at", 1)]
    assert options["expireAfterSeconds"] == SESSION_IDLE_SECONDS == 30 * 24 * 3600

    assert created("setup_codes")["setup_code_key_unique"][1]["unique"] is True


@pytest.mark.asyncio
async def test_create_owner_inserts_the_record_with_the_owner_role():
    repository, collections = _repository()

    stored = await repository.create_owner(_owner())

    inserted = collections["users"].insert_one.await_args.args[0]
    assert stored == _owner()
    assert inserted["role"] == "owner"
    assert inserted["user_id"] == "user_local_developer"
    assert inserted["password"]["algorithm"] == "scrypt"


@pytest.mark.asyncio
async def test_a_second_owner_is_refused_by_the_unique_index():
    repository, collections = _repository()
    collections["users"].insert_one.side_effect = DuplicateKeyError("dup")

    with pytest.raises(AccountExists):
        await repository.create_owner(_owner())


@pytest.mark.asyncio
async def test_get_owner_reads_the_owner_role():
    repository, collections = _repository()
    collections["users"].find_one.return_value = _owner().model_dump()

    assert await repository.get_owner() == _owner()
    assert collections["users"].find_one.await_args.args[0] == {"role": "owner"}


@pytest.mark.asyncio
async def test_update_owner_sets_fields_and_returns_the_new_record():
    repository, collections = _repository()
    collections["users"].find_one_and_update.return_value = _owner(
        name="Ada L."
    ).model_dump()

    updated = await repository.update_owner(
        "user_local_developer", {"name": "Ada L.", "updated_at": NOW}
    )

    query, update = collections["users"].find_one_and_update.await_args.args
    assert query == {"user_id": "user_local_developer", "role": "owner"}
    assert update == {"$set": {"name": "Ada L.", "updated_at": NOW}}
    assert updated.name == "Ada L."


@pytest.mark.asyncio
async def test_update_owner_can_require_the_password_read_earlier():
    repository, collections = _repository()

    updated = await repository.update_owner(
        "user_local_developer", {"name": "A"}, if_password_id="p1"
    )

    query = collections["users"].find_one_and_update.await_args.args[0]
    assert query == {
        "user_id": "user_local_developer",
        "role": "owner",
        "password_id": "p1",
    }
    assert updated is None


@pytest.mark.asyncio
async def test_sessions_are_found_by_token_hash_and_listed_newest_first():
    repository, collections = _repository()
    sessions = collections["user_sessions"]
    sessions.find_one.return_value = _session().model_dump()
    sessions.find.return_value = [_session().model_dump()]

    await repository.create_session(_session())
    found = await repository.get_session("t" * 64)
    listed = await repository.list_sessions("user_local_developer")

    assert sessions.insert_one.await_args.args[0]["token_hash"] == "t" * 64
    assert sessions.find_one.await_args.args[0] == {"token_hash": "t" * 64}
    assert found == _session() == listed[0]
    assert sessions.find.await_args.args[0] == {"user_id": "user_local_developer"}
    assert sessions.find.await_args.kwargs["sort"] == [("last_used_at", -1)]


@pytest.mark.asyncio
async def test_touch_and_delete_address_one_session():
    repository, collections = _repository()
    sessions = collections["user_sessions"]
    later = NOW + timedelta(minutes=10)

    await repository.touch_session("s1", later)
    deleted = await repository.delete_session("user_local_developer", "s1")

    assert sessions.update_one.await_args.args == (
        {"session_id": "s1"},
        {"$set": {"last_used_at": later}},
    )
    assert deleted is True
    assert sessions.delete_one.await_args.args[0] == {
        "user_id": "user_local_developer",
        "session_id": "s1",
    }


@pytest.mark.asyncio
async def test_delete_sessions_can_keep_the_current_one():
    repository, collections = _repository()
    sessions = collections["user_sessions"]
    sessions.delete_many.return_value = 2

    assert await repository.delete_sessions("u", keep="s1") == 2
    assert sessions.delete_many.await_args.args[0] == {
        "user_id": "u",
        "session_id": {"$ne": "s1"},
    }

    await repository.delete_sessions("u")
    assert sessions.delete_many.await_args.args[0] == {"user_id": "u"}


@pytest.mark.asyncio
async def test_a_new_setup_code_replaces_the_stored_one():
    repository, collections = _repository()
    codes = collections["setup_codes"]

    await repository.replace_setup_code("h" * 64, NOW)

    query, update = codes.update_one.await_args.args
    assert query == {"key": "owner_setup"}
    assert update == {"$set": {"code_hash": "h" * 64, "created_at": NOW}}
    assert codes.update_one.await_args.kwargs == {"upsert": True}


@pytest.mark.asyncio
async def test_a_setup_code_is_consumed_in_one_delete():
    repository, collections = _repository()
    codes = collections["setup_codes"]
    since = NOW - timedelta(hours=24)
    expected = {
        "key": "owner_setup",
        "code_hash": "h" * 64,
        "created_at": {"$gt": since},
    }
    codes.find_one.return_value = {"key": "owner_setup"}

    assert await repository.has_setup_code("h" * 64, since) is True
    assert codes.find_one.await_args.args[0] == expected

    assert await repository.consume_setup_code("h" * 64, since) is True
    assert codes.delete_one.await_args.args[0] == expected

    codes.delete_one.return_value = False
    assert await repository.consume_setup_code("h" * 64, since) is False

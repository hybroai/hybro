"""SourceService: create, rename, rotate, revoke, list, and resolve a token."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

import pytest

from source import SourceService
from source.models import NO_TOKEN, NO_TOKEN_SOURCE_ID, TOKEN_PREFIX
from source.protocols import SourceNotFound, SourceRegistry, SourceRevoked
from tests.fakes.source import MemorySourceStore

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


def make(auth_mode="mock"):
    store = MemorySourceStore()
    clock = Clock()
    return SourceService(store, auth_mode=auth_mode, now=clock), store, clock


def test_service_satisfies_the_registry_port():
    service, _, _ = make()
    assert isinstance(service, SourceRegistry)


async def test_create_returns_the_token_once_and_stores_only_its_hash():
    service, store, _ = make()

    created = await service.create("CI bot")

    assert created.name == "CI bot"
    assert created.token.startswith(TOKEN_PREFIX)
    assert created.token_last4 == created.token[-4:]
    stored = store.records[created.source_id]
    assert stored.token_hash == hashlib.sha256(created.token.encode()).hexdigest()
    assert created.token not in stored.model_dump_json()
    listed = await service.list_sources()
    assert [s.source_id for s in listed.sources] == [created.source_id]
    assert "token" not in listed.sources[0].model_dump()
    assert listed.sources[0].has_token is True


async def test_two_sources_get_different_ids_and_tokens():
    service, _, _ = make()
    first = await service.create("one")
    second = await service.create("two")
    assert first.source_id != second.source_id
    assert first.token != second.token
    assert NO_TOKEN_SOURCE_ID not in {first.source_id, second.source_id}


async def test_resolve_finds_the_source_and_records_its_use():
    service, store, clock = make()
    created = await service.create("CI bot")

    identity = await service.resolve(created.token)

    assert identity is not None
    assert (identity.source_id, identity.name, identity.has_token) == (
        created.source_id,
        "CI bot",
        True,
    )
    assert store.records[created.source_id].last_used_at == NOW
    # A second use within the minute is not written again.
    clock.now = NOW + timedelta(seconds=30)
    await service.resolve(created.token)
    assert len(store.touched) == 1
    clock.now = NOW + timedelta(minutes=2)
    await service.resolve(created.token)
    assert len(store.touched) == 2


@pytest.mark.parametrize("token", ["", "hybro_src_unknown", "mock-jwt-token"])
async def test_an_unknown_token_resolves_to_nothing(token):
    service, _, _ = make()
    await service.create("CI bot")
    assert await service.resolve(token) is None


async def test_rotation_replaces_the_token_and_keeps_the_source():
    service, _, _ = make()
    created = await service.create("CI bot")

    rotated = await service.rotate(created.source_id)

    assert rotated.source_id == created.source_id
    assert rotated.token != created.token
    assert await service.resolve(created.token) is None
    identity = await service.resolve(rotated.token)
    assert identity is not None and identity.source_id == created.source_id


async def test_revoke_refuses_the_token_and_keeps_the_record():
    service, store, clock = make()
    created = await service.create("CI bot")
    clock.now = NOW + timedelta(hours=1)

    revoked = await service.revoke(created.source_id)

    assert revoked.revoked_at == clock.now
    assert revoked.has_token is False
    assert await service.resolve(created.token) is None
    assert store.records[created.source_id].token_hash is None
    # Revoking again is harmless; rotating a revoked source is refused.
    assert (await service.revoke(created.source_id)).revoked_at == clock.now
    with pytest.raises(SourceRevoked):
        await service.rotate(created.source_id)
    # Its sessions still name it.
    names = await service.identities([created.source_id])
    assert names[created.source_id].name == "CI bot"
    assert names[created.source_id].has_token is False


async def test_rename_changes_the_name_only():
    service, _, _ = make()
    created = await service.create("CI bot")

    renamed = await service.rename(created.source_id, "Nightly CI")

    assert renamed.name == "Nightly CI"
    identity = await service.resolve(created.token)
    assert identity is not None and identity.name == "Nightly CI"


@pytest.mark.parametrize("action", ["rename", "rotate", "revoke"])
async def test_an_unknown_source_is_not_found(action):
    service, _, _ = make()
    call = getattr(service, action)
    args = ("missing", "name") if action == "rename" else ("missing",)
    with pytest.raises(SourceNotFound):
        await call(*args)


async def test_identities_include_no_token_and_skip_unknown_ids():
    service, _, _ = make()
    created = await service.create("CI bot")

    found = await service.identities([created.source_id, NO_TOKEN_SOURCE_ID, "gone"])

    assert found[NO_TOKEN_SOURCE_ID] == NO_TOKEN
    assert found[created.source_id].name == "CI bot"
    assert "gone" not in found


@pytest.mark.parametrize(("mode", "required"), [("mock", False), ("clerk", True)])
async def test_the_requirement_is_on_only_in_clerk_mode(mode, required):
    service, _, _ = make(auth_mode=mode)
    assert await service.token_required() is required


async def test_a_failed_last_use_write_still_resolves_and_is_logged(
    monkeypatch, caplog
):
    service, store, _ = make()
    created = await service.create("CI bot")

    async def broken_touch(source_id, at):
        raise RuntimeError("write failed")

    monkeypatch.setattr(store, "touch", broken_touch)
    caplog.set_level("WARNING")

    identity = await service.resolve(created.token)

    assert identity is not None and identity.source_id == created.source_id
    [record] = [r for r in caplog.records if r.msg == "source_last_use_write_failed"]
    assert record.source_id == created.source_id
    assert created.token not in caplog.text

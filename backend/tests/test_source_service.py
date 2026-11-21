"""SourceService: create, rename, rotate, revoke, list, and resolve a token."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

import pytest

from source import SourceService
from source.models import NO_TOKEN, NO_TOKEN_SOURCE_ID, TOKEN_PREFIX
from source.protocols import (
    SourceNotFound,
    SourceRegistry,
    SourceRevoked,
    TokenRequirementLocked,
    TokenRequirementUnset,
)
from tests.fakes.source import MemorySourceStore, store_with_requirement

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


@pytest.mark.parametrize(
    ("mode", "existing", "required"),
    [
        ("mock", False, True),
        ("mock", True, False),
        ("clerk", False, True),
        ("clerk", True, True),
    ],
)
async def test_the_seed_is_on_for_a_fresh_install_and_in_clerk_mode(
    mode, existing, required
):
    service, _, _ = make(auth_mode=mode)

    seeded = await service.seed_token_requirement(existing_install=existing)

    assert seeded.required is required
    assert seeded.started_off is (not required)
    assert seeded.updated_at == NOW
    assert await service.token_required() is required
    assert await service.token_requirement() == seeded


async def test_a_second_seed_keeps_the_stored_switch():
    service, _, clock = make()
    await service.seed_token_requirement(existing_install=False)
    changed = await service.set_token_required(False)
    clock.now = NOW + timedelta(days=1)

    again = await service.seed_token_requirement(existing_install=False)

    assert again == changed
    assert await service.token_required() is False


async def test_the_owner_changes_the_switch_and_keeps_how_it_started():
    service, _, clock = make()
    await service.seed_token_requirement(existing_install=True)
    clock.now = NOW + timedelta(minutes=5)

    on = await service.set_token_required(True)

    assert on.required is True
    assert on.started_off is True
    assert on.updated_at == NOW + timedelta(minutes=5)
    assert await service.token_required() is True


async def test_clerk_mode_always_requires_a_token_whatever_was_stored():
    # Seeded off in mock mode, then started with clerk: the stored off stays,
    # and Clerk mode must not become more open than it was.
    store = store_with_requirement(required=False)
    service = SourceService(store, auth_mode="clerk", now=Clock())

    assert await service.token_required() is True
    assert (await service.token_requirement()).required is True
    with pytest.raises(TokenRequirementLocked):
        await service.set_token_required(False)
    assert store.requirement.required is False
    assert (await service.set_token_required(True)).required is True


async def test_an_unseeded_switch_requires_a_token_and_cannot_be_changed():
    service, _, _ = make()

    # Fails closed: no document means a token is required.
    assert await service.token_required() is True
    with pytest.raises(TokenRequirementUnset):
        await service.token_requirement()
    with pytest.raises(TokenRequirementUnset):
        await service.set_token_required(False)


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


async def test_resolve_records_how_the_source_connected():
    service, store, clock = make()
    created = await service.create("CI bot")

    await service.resolve(created.token)
    assert store.records[created.source_id].last_used_via == "api"

    # A change of channel is written at once, within the minute too.
    clock.now = NOW + timedelta(seconds=10)
    await service.resolve(created.token, via="mcp")
    assert store.records[created.source_id].last_used_via == "mcp"
    assert store.touched[-1] == (created.source_id, clock.now, "mcp")
    # The same channel within the minute is not written again.
    clock.now = NOW + timedelta(seconds=20)
    await service.resolve(created.token, via="mcp")
    assert len(store.touched) == 2
    listed = await service.list_sources()
    assert listed.sources[0].last_used_via == "mcp"

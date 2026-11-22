"""ClientService: create, rename, rotate, revoke, list, and resolve a token."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

import pytest

from client import ClientService
from client.models import (
    NO_TOKEN,
    NO_TOKEN_CLIENT_ID,
    TOKEN_PREFIX,
    ClientIdentity,
    ClientRecord,
)
from client.protocols import (
    ClientBuiltIn,
    ClientNotFound,
    ClientRegistry,
    ClientRevoked,
    TokenRequirementLocked,
    TokenRequirementUnset,
)
from tests.fakes.client import MemoryClientStore, store_with_requirement

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


def make(auth_mode="mock"):
    store = MemoryClientStore()
    clock = Clock()
    return ClientService(store, auth_mode=auth_mode, now=clock), store, clock


def test_service_satisfies_the_registry_port():
    service, _, _ = make()
    assert isinstance(service, ClientRegistry)


async def test_create_returns_the_token_once_and_stores_only_its_hash():
    service, store, _ = make()

    created = await service.create("CI bot")

    assert created.name == "CI bot"
    assert created.token.startswith(TOKEN_PREFIX)
    assert created.token_last4 == created.token[-4:]
    stored = store.records[created.client_id]
    assert stored.token_hash == hashlib.sha256(created.token.encode()).hexdigest()
    assert created.token not in stored.model_dump_json()
    listed = await service.list_clients()
    assert [s.client_id for s in listed.clients] == [created.client_id]
    assert "token" not in listed.clients[0].model_dump()
    assert listed.clients[0].has_token is True


async def test_two_clients_get_different_ids_and_tokens():
    service, _, _ = make()
    first = await service.create("one")
    second = await service.create("two")
    assert first.client_id != second.client_id
    assert first.token != second.token
    assert NO_TOKEN_CLIENT_ID not in {first.client_id, second.client_id}


async def test_resolve_finds_the_client_and_records_its_use():
    service, store, clock = make()
    created = await service.create("CI bot")

    identity = await service.resolve(created.token)

    assert identity is not None
    assert (identity.client_id, identity.name, identity.has_token) == (
        created.client_id,
        "CI bot",
        True,
    )
    assert store.records[created.client_id].last_used_at == NOW
    # A second use within the minute is not written again.
    clock.now = NOW + timedelta(seconds=30)
    await service.resolve(created.token)
    assert len(store.touched) == 1
    clock.now = NOW + timedelta(minutes=2)
    await service.resolve(created.token)
    assert len(store.touched) == 2


@pytest.mark.parametrize("token", ["", "hybro_client_unknown", "mock-jwt-token"])
async def test_an_unknown_token_resolves_to_nothing(token):
    service, _, _ = make()
    await service.create("CI bot")
    assert await service.resolve(token) is None


async def test_rotation_replaces_the_token_and_keeps_the_client():
    service, _, _ = make()
    created = await service.create("CI bot")

    rotated = await service.rotate(created.client_id)

    assert rotated.client_id == created.client_id
    assert rotated.token != created.token
    assert await service.resolve(created.token) is None
    identity = await service.resolve(rotated.token)
    assert identity is not None and identity.client_id == created.client_id


async def test_revoke_refuses_the_token_and_keeps_the_record():
    service, store, clock = make()
    created = await service.create("CI bot")
    clock.now = NOW + timedelta(hours=1)

    revoked = await service.revoke(created.client_id)

    assert revoked.revoked_at == clock.now
    assert revoked.has_token is False
    assert await service.resolve(created.token) is None
    assert store.records[created.client_id].token_hash is None
    # Revoking again is harmless; rotating a revoked client is refused.
    assert (await service.revoke(created.client_id)).revoked_at == clock.now
    with pytest.raises(ClientRevoked):
        await service.rotate(created.client_id)
    # Its sessions still name it.
    names = await service.identities([created.client_id])
    assert names[created.client_id].name == "CI bot"
    assert names[created.client_id].has_token is False


async def test_rename_changes_the_name_only():
    service, _, _ = make()
    created = await service.create("CI bot")

    renamed = await service.rename(created.client_id, "Nightly CI")

    assert renamed.name == "Nightly CI"
    identity = await service.resolve(created.token)
    assert identity is not None and identity.name == "Nightly CI"


@pytest.mark.parametrize("action", ["rename", "rotate", "revoke"])
async def test_an_unknown_client_is_not_found(action):
    service, _, _ = make()
    call = getattr(service, action)
    args = ("missing", "name") if action == "rename" else ("missing",)
    with pytest.raises(ClientNotFound):
        await call(*args)


async def test_identities_include_no_token_and_skip_unknown_ids():
    service, _, _ = make()
    created = await service.create("CI bot")

    found = await service.identities([created.client_id, NO_TOKEN_CLIENT_ID, "gone"])

    assert found[NO_TOKEN_CLIENT_ID] == NO_TOKEN
    assert found[created.client_id].name == "CI bot"
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
    service = ClientService(store, auth_mode="clerk", now=Clock())

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

    async def broken_touch(client_id, at, via):
        raise RuntimeError("write failed")

    monkeypatch.setattr(store, "touch", broken_touch)
    caplog.set_level("WARNING")

    identity = await service.resolve(created.token)

    assert identity is not None and identity.client_id == created.client_id
    [record] = [r for r in caplog.records if r.msg == "client_last_use_write_failed"]
    assert record.client_id == created.client_id
    assert created.token not in caplog.text


async def test_resolve_records_how_the_client_connected():
    service, store, clock = make()
    created = await service.create("CI bot")

    await service.resolve(created.token)
    assert store.records[created.client_id].last_used_via == "api"

    # A change of channel is written at once, within the minute too.
    clock.now = NOW + timedelta(seconds=10)
    await service.resolve(created.token, via="mcp")
    assert store.records[created.client_id].last_used_via == "mcp"
    assert store.touched[-1] == (created.client_id, clock.now, "mcp")
    # The same channel within the minute is not written again.
    clock.now = NOW + timedelta(seconds=20)
    await service.resolve(created.token, via="mcp")
    assert len(store.touched) == 2
    listed = await service.list_clients()
    assert listed.clients[0].last_used_via == "mcp"


# -- The grant: a client may answer permission questions ----------------------


def ident(client_id: str) -> ClientIdentity:
    """The identity a request carries; the read must not trust its fields."""
    return ClientIdentity(client_id=client_id, name="any", has_token=True)


async def test_a_new_client_has_no_grant():
    service, _, _ = make()
    created = await service.create("CI bot")

    assert created.answer_grant is False
    assert (await service.list_clients()).clients[0].answer_grant is False
    assert (
        await service.may_answer_permission_questions(ident(created.client_id)) is False
    )


async def test_the_owner_sets_and_clears_the_grant_and_it_is_logged(
    caplog, monkeypatch
):
    service, store, clock = make()
    created = await service.create("CI bot")
    clock.now = NOW + timedelta(minutes=1)
    caplog.set_level("INFO")
    passed_through: list[object] = []

    def recording_log_id(value):
        passed_through.append(value)
        return value

    monkeypatch.setattr("client.service.log_id", recording_log_id)

    granted = await service.set_answer_grant(created.client_id, True)

    assert granted.answer_grant is True
    assert store.records[created.client_id].updated_at == clock.now
    assert (
        await service.may_answer_permission_questions(ident(created.client_id)) is True
    )
    cleared = await service.set_answer_grant(created.client_id, False)
    assert cleared.answer_grant is False
    assert (
        await service.may_answer_permission_questions(ident(created.client_id)) is False
    )
    events = [r for r in caplog.records if r.msg == "client_answer_grant_changed"]
    assert [(r.client_id, r.granted) for r in events] == [
        (created.client_id, True),
        (created.client_id, False),
    ]
    # The id reaches the log only through log_id.
    assert passed_through == [created.client_id, created.client_id]
    assert created.token not in caplog.text
    assert created.token[-4:] not in caplog.text


async def test_any_number_of_clients_hold_the_grant_at_once():
    service, _, _ = make()
    first = await service.create("one")
    second = await service.create("two")

    await service.set_answer_grant(first.client_id, True)
    await service.set_answer_grant(second.client_id, True)

    assert await service.may_answer_permission_questions(ident(first.client_id)) is True
    assert (
        await service.may_answer_permission_questions(ident(second.client_id)) is True
    )


async def test_rotate_and_rename_keep_the_grant():
    service, _, _ = make()
    created = await service.create("CI bot")
    await service.set_answer_grant(created.client_id, True)

    rotated = await service.rotate(created.client_id)
    renamed = await service.rename(created.client_id, "Nightly")

    assert rotated.answer_grant is True
    assert renamed.answer_grant is True
    assert (
        await service.may_answer_permission_questions(ident(created.client_id)) is True
    )


async def test_revoke_clears_the_grant_in_the_same_write():
    service, store, _ = make()
    created = await service.create("CI bot")
    await service.set_answer_grant(created.client_id, True)

    revoked = await service.revoke(created.client_id)

    assert revoked.answer_grant is False
    assert store.records[created.client_id].answer_grant is False
    # One write: the revoke itself carries the cleared flag.
    assert store.updates[-1][1]["answer_grant"] is False
    assert store.updates[-1][1]["revoked_at"] is not None
    assert (
        await service.may_answer_permission_questions(ident(created.client_id)) is False
    )


async def test_the_grant_cannot_be_set_on_a_revoked_client():
    service, store, _ = make()
    created = await service.create("CI bot")
    await service.revoke(created.client_id)

    with pytest.raises(ClientRevoked):
        await service.set_answer_grant(created.client_id, True)
    # Clearing it on a revoked client is harmless and changes nothing.
    cleared = await service.set_answer_grant(created.client_id, False)
    assert cleared.answer_grant is False
    assert store.records[created.client_id].answer_grant is False


async def test_a_grant_that_races_a_revoke_never_leaves_a_revoked_holder():
    # The revoke lands between the set's read and its write: the write is
    # conditional on the client being live, so it does not apply.
    service, store, _ = make()
    created = await service.create("CI bot")
    original_update = store.update

    async def revoke_first(client_id, fields, *, live_only=False):
        if "answer_grant" in fields and "revoked_at" not in fields:
            store.update = original_update
            await service.revoke(client_id)
        return await original_update(client_id, fields, live_only=live_only)

    store.update = revoke_first

    with pytest.raises(ClientRevoked):
        await service.set_answer_grant(created.client_id, True)
    assert store.records[created.client_id].answer_grant is False
    assert store.records[created.client_id].revoked_at is not None


@pytest.mark.parametrize("granted", [True, False])
async def test_the_grant_cannot_be_set_on_no_token(granted):
    service, store, _ = make()

    with pytest.raises(ClientBuiltIn):
        await service.set_answer_grant(NO_TOKEN_CLIENT_ID, granted)
    assert store.updates == []


async def test_an_unknown_client_has_no_grant_and_cannot_get_one():
    service, _, _ = make()

    assert (
        await service.may_answer_permission_questions(ident("client_missing")) is False
    )
    with pytest.raises(ClientNotFound):
        await service.set_answer_grant("client_missing", True)


async def test_the_read_answers_false_for_a_built_in_or_revoked_client_whatever_is_stored():
    service, store, clock = make()
    # A record that carries the flag under the built-in id, and a revoked one
    # that still carries it: neither may answer.
    store.records[NO_TOKEN_CLIENT_ID] = ClientRecord(
        client_id=NO_TOKEN_CLIENT_ID,
        name="No token",
        token_hash="a" * 64,
        token_last4="wxyz",
        created_at=NOW,
        updated_at=NOW,
        answer_grant=True,
    )
    created = await service.create("CI bot")
    store.records[created.client_id] = store.records[created.client_id].model_copy(
        update={
            "answer_grant": True,
            "revoked_at": NOW,
            "token_hash": None,
            "token_last4": None,
        }
    )

    # Neither the constant nor an identity that claims a token under its id.
    assert await service.may_answer_permission_questions(NO_TOKEN) is False
    assert (
        await service.may_answer_permission_questions(ident(NO_TOKEN_CLIENT_ID))
        is False
    )
    assert (
        await service.may_answer_permission_questions(ident(created.client_id)) is False
    )


async def test_a_stored_record_without_the_field_reads_as_no_grant():
    record = ClientRecord.model_validate(
        {
            "client_id": "client_old",
            "name": "Old",
            "token_hash": "a" * 64,
            "token_last4": "wxyz",
            "created_at": NOW,
            "updated_at": NOW,
        }
    )
    assert record.answer_grant is False


async def test_the_read_answers_false_for_an_identity_without_a_token():
    # The stored client holds the grant, but the identity passed in carries no
    # token: the read never trusts the stored flag alone.
    service, _, _ = make()
    created = await service.create("CI bot")
    await service.set_answer_grant(created.client_id, True)
    tokenless = ClientIdentity(
        client_id=created.client_id, name="CI bot", has_token=False
    )

    assert await service.may_answer_permission_questions(tokenless) is False

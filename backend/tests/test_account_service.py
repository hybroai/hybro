"""The owner account: setup, sign-in, browser sessions and the machine actions."""

from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta

import pytest

from account import AttemptLimiter, ScryptParams
from account.protocols import (
    OWNER_USER_ID,
    AccountExists,
    BrowserNotFound,
    InvalidCredentials,
    InvalidSetupCode,
    NoAccount,
    OwnerAccount,
    OwnerCreate,
    OwnerIdRequired,
    PasswordChange,
    PasswordReset,
    ProfileUpdate,
    SetupRequest,
    SignInRequest,
)
from tests.fakes.account import Clock, MemoryAccountStore, Sleeper, build_service

T0 = datetime(2030, 1, 1, tzinfo=UTC)
PASSWORD = "correct horse battery"
OWNER = {"name": "Ada", "email": "ada@example.com", "password": PASSWORD}


def _create(**overrides) -> OwnerCreate:
    return OwnerCreate(**{**OWNER, **overrides})


def _sign_in(password: str = PASSWORD, email: str = "ada@example.com") -> SignInRequest:
    return SignInRequest(email=email, password=password)


async def _signed_in(service, *, user_agent: str = "Firefox"):
    return await service.sign_in(_sign_in(), address="10.0.0.1", user_agent=user_agent)


def test_the_service_satisfies_the_route_protocol():
    assert isinstance(build_service(), OwnerAccount)


# ---- Creating the owner ----


@pytest.mark.asyncio
async def test_no_account_exists_at_first():
    assert await build_service().exists() is False


@pytest.mark.asyncio
async def test_create_owner_takes_the_fixed_id_and_stores_no_password():
    store = MemoryAccountStore()
    service = build_service(store=store)

    profile = await service.create_owner(_create())

    assert profile.user_id == OWNER_USER_ID == "user_local_developer"
    assert (profile.name, profile.email) == ("Ada", "ada@example.com")
    assert await service.exists() is True
    assert PASSWORD not in store.owner.model_dump_json()
    assert store.owner.password.algorithm == "scrypt"


@pytest.mark.asyncio
async def test_create_owner_fails_once_an_account_exists():
    service = build_service()
    await service.create_owner(_create())

    with pytest.raises(AccountExists):
        await service.create_owner(_create(email="other@example.com"))


@pytest.mark.asyncio
async def test_clerk_mode_takes_the_single_listed_admin_id():
    service = build_service(auth_mode="clerk", admin_user_ids=frozenset({"user_abc"}))

    assert (await service.create_owner(_create())).user_id == "user_abc"


@pytest.mark.asyncio
async def test_clerk_mode_without_a_listed_admin_takes_the_fixed_id():
    service = build_service(auth_mode="clerk")

    assert (await service.create_owner(_create())).user_id == OWNER_USER_ID


@pytest.mark.asyncio
async def test_clerk_mode_with_several_admins_asks_which_one():
    service = build_service(
        auth_mode="clerk", admin_user_ids=frozenset({"user_b", "user_a"})
    )

    with pytest.raises(OwnerIdRequired) as raised:
        await service.create_owner(_create())
    assert raised.value.choices == ("user_a", "user_b")
    with pytest.raises(OwnerIdRequired):
        await service.create_owner(_create(owner_id="user_c"))

    assert (await service.create_owner(_create(owner_id="user_b"))).user_id == "user_b"


@pytest.mark.asyncio
async def test_mock_mode_ignores_listed_admins_and_refuses_another_id():
    service = build_service(admin_user_ids=frozenset({"user_abc"}))

    with pytest.raises(OwnerIdRequired) as raised:
        await service.create_owner(_create(owner_id="user_abc"))
    assert raised.value.choices == (OWNER_USER_ID,)

    assert (await service.create_owner(_create())).user_id == OWNER_USER_ID


# ---- The setup code ----


@pytest.mark.asyncio
async def test_setup_with_the_issued_code_creates_the_owner_and_signs_in():
    store = MemoryAccountStore()
    service = build_service(store=store, clock=Clock(T0))

    issued = await service.issue_setup_code()
    new = await service.setup(
        SetupRequest(code=issued.code, **OWNER), address="10.0.0.1", user_agent="Fx"
    )

    assert issued.expires_at == T0 + timedelta(hours=24)
    assert new.owner.user_id == OWNER_USER_ID
    assert (await service.resolve(new.token)).owner == new.owner
    # Only a hash of the code and of the cookie value is stored.
    assert issued.code not in repr(store.__dict__)
    assert new.token not in repr(store.__dict__)
    assert store.setup_code is None


@pytest.mark.asyncio
async def test_a_setup_code_works_once():
    service = build_service()
    issued = await service.issue_setup_code()
    await service.setup(
        SetupRequest(code=issued.code, **OWNER), address="a", user_agent=""
    )

    with pytest.raises(AccountExists):
        await service.setup(
            SetupRequest(code=issued.code, **OWNER), address="a", user_agent=""
        )


@pytest.mark.asyncio
async def test_a_wrong_code_is_refused_and_creates_nothing():
    service = build_service()
    await service.issue_setup_code()

    with pytest.raises(InvalidSetupCode):
        await service.setup(
            SetupRequest(code="x" * 43, **OWNER), address="a", user_agent=""
        )
    assert await service.exists() is False


@pytest.mark.asyncio
async def test_a_setup_code_stops_after_24_hours():
    clock = Clock(T0)
    service = build_service(clock=clock)
    issued = await service.issue_setup_code()

    clock.now = T0 + timedelta(hours=24, seconds=1)

    with pytest.raises(InvalidSetupCode):
        await service.setup(
            SetupRequest(code=issued.code, **OWNER), address="a", user_agent=""
        )


@pytest.mark.asyncio
async def test_a_newer_setup_code_replaces_the_older_one():
    service = build_service()
    first = await service.issue_setup_code()
    second = await service.issue_setup_code()

    with pytest.raises(InvalidSetupCode):
        await service.setup(
            SetupRequest(code=first.code, **OWNER), address="a", user_agent=""
        )
    await service.setup(
        SetupRequest(code=second.code, **OWNER), address="a", user_agent=""
    )


@pytest.mark.asyncio
async def test_no_setup_code_is_issued_once_an_account_exists():
    service = build_service()
    await service.create_owner(_create())

    with pytest.raises(AccountExists):
        await service.issue_setup_code()


@pytest.mark.asyncio
async def test_asking_which_admin_does_not_use_up_the_setup_code():
    service = build_service(
        auth_mode="clerk", admin_user_ids=frozenset({"user_a", "user_b"})
    )
    issued = await service.issue_setup_code()

    with pytest.raises(OwnerIdRequired):
        await service.setup(
            SetupRequest(code=issued.code, **OWNER), address="a", user_agent=""
        )
    new = await service.setup(
        SetupRequest(code=issued.code, owner_id="user_a", **OWNER),
        address="a",
        user_agent="",
    )

    assert new.owner.user_id == "user_a"


@pytest.mark.asyncio
async def test_the_admin_choices_are_not_shown_for_a_wrong_code():
    service = build_service(
        auth_mode="clerk", admin_user_ids=frozenset({"user_a", "user_b"})
    )
    await service.issue_setup_code()

    with pytest.raises(InvalidSetupCode):
        await service.setup(
            SetupRequest(code="y" * 43, **OWNER), address="a", user_agent=""
        )


@pytest.mark.asyncio
async def test_repeated_wrong_setup_codes_slow_down_per_address():
    sleeper = Sleeper()
    service = build_service(sleeper=sleeper)
    issued = await service.issue_setup_code()

    for _ in range(3):
        with pytest.raises(InvalidSetupCode):
            await service.setup(
                SetupRequest(code="z" * 43, **OWNER), address="a", user_agent=""
            )
    await service.setup(
        SetupRequest(code=issued.code, **OWNER), address="a", user_agent=""
    )

    assert sleeper.delays[0] == 0
    assert sleeper.delays[1] < sleeper.delays[2] < sleeper.delays[3]


# ---- Sign-in ----


@pytest.mark.asyncio
async def test_sign_in_returns_a_session_whose_value_is_stored_only_as_a_hash():
    store = MemoryAccountStore()
    service = build_service(store=store, clock=Clock(T0))
    await service.create_owner(_create())

    new = await _signed_in(service, user_agent="Firefox")

    (record,) = store.sessions.values()
    assert record.token_hash == hashlib.sha256(new.token.encode()).hexdigest()
    assert new.token not in record.model_dump_json()
    assert (record.user_agent, record.created_at, record.last_used_at) == (
        "Firefox",
        T0,
        T0,
    )
    assert len(new.token) >= 43


@pytest.mark.asyncio
async def test_sign_in_ignores_the_case_of_the_email():
    service = build_service()
    await service.create_owner(_create())

    await service.sign_in(_sign_in(email="ADA@Example.com"), address="a", user_agent="")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "request_",
    [_sign_in(password="wrong password!"), _sign_in(email="eve@example.com")],
)
async def test_a_wrong_password_or_email_is_refused_the_same_way(request_):
    store = MemoryAccountStore()
    service = build_service(store=store)
    await service.create_owner(_create())

    with pytest.raises(InvalidCredentials):
        await service.sign_in(request_, address="a", user_agent="")
    assert store.sessions == {}


@pytest.mark.asyncio
async def test_sign_in_without_an_account_is_refused():
    with pytest.raises(InvalidCredentials):
        await build_service().sign_in(_sign_in(), address="a", user_agent="")


@pytest.mark.asyncio
async def test_repeated_wrong_passwords_slow_sign_in_and_never_block_the_right_one():
    sleeper = Sleeper()
    service = build_service(sleeper=sleeper)
    await service.create_owner(_create())

    for _ in range(6):
        with pytest.raises(InvalidCredentials):
            await service.sign_in(
                _sign_in(password="wrong password!"), address="a", user_agent=""
            )
    new = await _signed_in(service)

    assert new.owner.email == "ada@example.com"
    assert sleeper.delays[0] == 0
    assert sleeper.delays[1] > 0
    assert sleeper.delays == sorted(sleeper.delays)
    assert sleeper.delays[-1] > sleeper.delays[1]
    # The success cleared the count: the next sign-in is not delayed.
    await _signed_in(service)
    assert sleeper.delays[-1] == 0


@pytest.mark.asyncio
async def test_wrong_passwords_from_one_address_do_not_slow_a_wrong_free_address():
    sleeper = Sleeper()
    service = build_service(sleeper=sleeper)
    await service.create_owner(_create())
    for _ in range(4):
        with pytest.raises(InvalidCredentials):
            await service.sign_in(
                _sign_in(email="eve@example.com"), address="6.6.6.6", user_agent=""
            )

    await service.sign_in(_sign_in(), address="10.0.0.1", user_agent="")

    assert sleeper.delays[-1] == 0


@pytest.mark.asyncio
async def test_the_delay_has_an_upper_bound():
    sleeper = Sleeper()
    limiter = AttemptLimiter(base_seconds=1.0, max_seconds=4.0, clock=sleeper.clock)
    service = build_service(sleeper=sleeper, limiter=limiter)
    await service.create_owner(_create())

    for _ in range(10):
        with pytest.raises(InvalidCredentials):
            await service.sign_in(
                _sign_in(password="wrong password!"), address="a", user_agent=""
            )

    assert max(sleeper.delays) == 4.0


@pytest.mark.asyncio
async def test_an_old_hash_is_upgraded_at_the_next_sign_in():
    store = MemoryAccountStore()
    old = ScryptParams(n=2**3, r=1, p=1)
    await build_service(store=store, params=old).create_owner(_create())
    assert store.owner.password.n == 2**3

    service = build_service(store=store)
    await _signed_in(service)

    assert store.owner.password.n == 2**4
    await _signed_in(service)


# ---- Browser sessions ----


@pytest.mark.asyncio
async def test_an_unknown_or_missing_cookie_value_resolves_to_nothing():
    service = build_service()
    await service.create_owner(_create())

    assert await service.resolve("") is None
    assert await service.resolve("not-a-session") is None


@pytest.mark.asyncio
async def test_last_use_is_recorded_at_most_every_few_minutes():
    store = MemoryAccountStore()
    clock = Clock(T0)
    service = build_service(store=store, clock=clock)
    await service.create_owner(_create())
    new = await _signed_in(service)

    clock.now = T0 + timedelta(minutes=1)
    await service.resolve(new.token)
    (record,) = store.sessions.values()
    assert record.last_used_at == T0

    clock.now = T0 + timedelta(minutes=10)
    await service.resolve(new.token)
    (record,) = store.sessions.values()
    assert record.last_used_at == T0 + timedelta(minutes=10)


@pytest.mark.asyncio
async def test_a_session_unused_for_thirty_days_ends():
    store = MemoryAccountStore()
    clock = Clock(T0)
    service = build_service(store=store, clock=clock)
    await service.create_owner(_create())
    new = await _signed_in(service)

    clock.now = T0 + timedelta(days=30, seconds=1)

    assert await service.resolve(new.token) is None
    assert store.sessions == {}


@pytest.mark.asyncio
async def test_sign_out_ends_that_browser_only():
    service = build_service()
    await service.create_owner(_create())
    first = await _signed_in(service)
    second = await _signed_in(service)

    await service.sign_out(await service.resolve(first.token))

    assert await service.resolve(first.token) is None
    assert await service.resolve(second.token) is not None


@pytest.mark.asyncio
async def test_the_browser_list_marks_the_current_one():
    clock = Clock(T0)
    service = build_service(clock=clock)
    await service.create_owner(_create())
    first = await _signed_in(service, user_agent="Firefox")
    session = await service.resolve(first.token)
    clock.now = T0 + timedelta(hours=1)
    await _signed_in(service, user_agent="Safari")

    browsers = await service.list_browsers(session)

    assert [(b.user_agent, b.current) for b in browsers.items] == [
        ("Safari", False),
        ("Firefox", True),
    ]
    assert browsers.items[1].created_at == T0
    assert first.token not in browsers.model_dump_json()


@pytest.mark.asyncio
async def test_revoking_a_browser_signs_it_out():
    service = build_service()
    await service.create_owner(_create())
    first = await _signed_in(service)
    second = await _signed_in(service)
    session = await service.resolve(first.token)
    other = next(
        b for b in (await service.list_browsers(session)).items if not b.current
    )

    await service.revoke_browser(session, other.id)

    assert await service.resolve(second.token) is None
    assert await service.resolve(first.token) is not None
    with pytest.raises(BrowserNotFound):
        await service.revoke_browser(session, other.id)


@pytest.mark.asyncio
async def test_update_profile_changes_the_name_and_the_sign_in_email():
    service = build_service()
    await service.create_owner(_create())
    session = await service.resolve((await _signed_in(service)).token)

    profile = await service.update_profile(
        session, ProfileUpdate(name="Ada L.", email="ada@lovelace.dev")
    )

    assert (profile.name, profile.email) == ("Ada L.", "ada@lovelace.dev")
    await service.sign_in(
        _sign_in(email="ada@lovelace.dev"), address="a", user_agent=""
    )
    with pytest.raises(InvalidCredentials):
        await service.sign_in(_sign_in(), address="b", user_agent="")


@pytest.mark.asyncio
async def test_a_password_change_signs_out_the_other_browsers():
    service = build_service()
    await service.create_owner(_create())
    first = await _signed_in(service)
    second = await _signed_in(service)

    await service.change_password(
        await service.resolve(first.token),
        PasswordChange(current_password=PASSWORD, new_password="a brand new one"),
    )

    assert await service.resolve(first.token) is not None
    assert await service.resolve(second.token) is None
    await service.sign_in(
        _sign_in(password="a brand new one"), address="a", user_agent=""
    )
    with pytest.raises(InvalidCredentials):
        await service.sign_in(_sign_in(), address="b", user_agent="")


@pytest.mark.asyncio
async def test_a_password_change_needs_the_current_password():
    service = build_service()
    await service.create_owner(_create())
    session = await service.resolve((await _signed_in(service)).token)

    with pytest.raises(InvalidCredentials):
        await service.change_password(
            session,
            PasswordChange(
                current_password="wrong password!", new_password="a brand new one"
            ),
        )
    await _signed_in(service)


# ---- Reset ----


@pytest.mark.asyncio
async def test_reset_password_sets_the_password_and_signs_out_every_browser():
    service = build_service()
    await service.create_owner(_create())
    first = await _signed_in(service)

    await service.reset_password(PasswordReset(password="reset to this one"))

    assert await service.resolve(first.token) is None
    await service.sign_in(
        _sign_in(password="reset to this one"), address="a", user_agent=""
    )


@pytest.mark.asyncio
async def test_reset_password_without_an_account_is_refused():
    with pytest.raises(NoAccount):
        await build_service().reset_password(PasswordReset(password="reset to this"))


# ---- Request validation ----


@pytest.mark.parametrize(
    "overrides",
    [
        {"password": "short"},
        {"password": "x" * 1025},
        {"email": "not-an-email"},
        {"name": "   "},
        {"name": "x" * 201},
    ],
)
def test_owner_fields_are_validated(overrides):
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        OwnerCreate(**{**OWNER, **overrides})


def test_owner_fields_are_trimmed_and_a_password_is_never_in_a_repr():
    request = OwnerCreate(name="  Ada ", email=" ada@example.com ", password=PASSWORD)

    assert (request.name, request.email) == ("Ada", "ada@example.com")
    assert PASSWORD not in repr(request)


# ---- Requests at the same time ----


@pytest.mark.asyncio
async def test_wrong_passwords_sent_at_once_take_turns():
    sleeper = Sleeper()
    limiter = AttemptLimiter(base_seconds=1.0, max_seconds=100.0, clock=lambda: 0.0)
    service = build_service(sleeper=sleeper, limiter=limiter)
    await service.create_owner(_create())

    results = await asyncio.gather(
        *(
            service.sign_in(
                _sign_in(password="wrong password!"), address="a", user_agent=""
            )
            for _ in range(5)
        ),
        return_exceptions=True,
    )

    assert all(isinstance(result, InvalidCredentials) for result in results)
    # Each waits for the one before it and then its own delay.
    assert sorted(sleeper.delays) == [0.0, 1.0, 3.0, 7.0, 15.0]


@pytest.mark.asyncio
async def test_a_flood_of_wrong_passwords_never_refuses_the_owner():
    sleeper = Sleeper()
    service = build_service(sleeper=sleeper)
    await service.create_owner(_create())
    results = await asyncio.gather(
        *(
            service.sign_in(
                _sign_in(password="wrong password!"),
                address="6.6.6.6" if index % 2 else f"10.1.0.{index}",
                user_agent="",
            )
            for index in range(100)
        ),
        return_exceptions=True,
    )
    assert all(isinstance(result, InvalidCredentials) for result in results)

    # From the flooding address itself, and from another one.
    for address in ("6.6.6.6", "10.0.0.1"):
        new = await service.sign_in(_sign_in(), address=address, user_agent="")
        assert new.owner.email == "ada@example.com"

    # Slowed to the cap at most, and never refused.
    assert max(sleeper.delays) == 10.0


@pytest.mark.asyncio
async def test_the_machine_reset_does_not_wait_for_queued_sign_ins():
    release = asyncio.Event()

    async def wait(_seconds: float) -> None:
        await release.wait()

    service = build_service(sleeper=wait)
    await service.create_owner(_create())
    queued = [
        asyncio.ensure_future(
            service.sign_in(
                _sign_in(password="wrong password!"), address="a", user_agent=""
            )
        )
        for _ in range(20)
    ]
    await asyncio.sleep(0)

    await service.reset_password(PasswordReset(password="reset to this one"))

    release.set()
    results = await asyncio.gather(*queued, return_exceptions=True)
    assert all(isinstance(result, InvalidCredentials) for result in results)


@pytest.mark.asyncio
async def test_password_checks_beyond_the_pending_limit_are_refused():
    from account.protocols import TooManyAttempts
    from account.service import _PENDING_LIMIT

    release = asyncio.Event()

    async def wait(_seconds: float) -> None:
        await release.wait()

    service = build_service(sleeper=wait)
    await service.create_owner(_create())
    held = [
        asyncio.ensure_future(
            service.sign_in(
                _sign_in(password="wrong password!"),
                address=f"10.2.{index // 250}.{index % 250}",
                user_agent="",
            )
        )
        for index in range(_PENDING_LIMIT)
    ]
    await asyncio.sleep(0)

    with pytest.raises(TooManyAttempts):
        await _signed_in(service)

    release.set()
    await asyncio.gather(*held, return_exceptions=True)
    await _signed_in(service)


@pytest.mark.asyncio
async def test_a_password_change_does_not_undo_a_reset_made_while_it_waited():
    store = MemoryAccountStore()
    waiting = asyncio.Event()
    release = asyncio.Event()

    async def wait(_seconds: float) -> None:
        waiting.set()
        await release.wait()

    service = build_service(store=store, sleeper=wait)
    await service.create_owner(_create())
    release.set()
    session = await service.resolve((await _signed_in(service)).token)
    release.clear()
    waiting.clear()

    change = asyncio.ensure_future(
        service.change_password(
            session,
            PasswordChange(current_password=PASSWORD, new_password="a brand new one"),
        )
    )
    await waiting.wait()
    await service.reset_password(PasswordReset(password="reset to this one"))
    release.set()

    with pytest.raises(InvalidCredentials):
        await change
    await service.sign_in(
        _sign_in(password="reset to this one"), address="b", user_agent=""
    )


@pytest.mark.asyncio
async def test_a_sign_in_checked_before_a_reset_does_not_open_a_session_after_it():
    store = MemoryAccountStore()
    service = build_service(store=store)
    await service.create_owner(_create())
    original = store.get_owner
    calls = 0

    async def reset_before_second_read():
        nonlocal calls
        calls += 1
        if calls == 2:
            # After the session is stored, at the look that follows it.
            store.get_owner = original
            await service.reset_password(PasswordReset(password="reset to this one"))
        return await original()

    store.get_owner = reset_before_second_read

    with pytest.raises(InvalidCredentials):
        await _signed_in(service)
    assert store.sessions == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("replace_with", ["reset", "change"])
async def test_a_sign_in_overtaken_at_the_last_moment_leaves_no_session(replace_with):
    store = MemoryAccountStore()
    service = build_service(store=store)
    await service.create_owner(_create())
    other = await service.resolve((await _signed_in(service)).token)
    original = store.create_session

    async def replaced_first(record):
        # The password is replaced, and browsers signed out, after every
        # check of this sign-in and before its session is stored.
        store.create_session = original
        if replace_with == "reset":
            await service.reset_password(PasswordReset(password="a replacement 1"))
        else:
            await service.change_password(
                other,
                PasswordChange(
                    current_password=PASSWORD, new_password="a replacement 1"
                ),
            )
        await original(record)

    store.create_session = replaced_first

    with pytest.raises(InvalidCredentials):
        await _signed_in(service)

    # Only the browser that changed the password is still signed in.
    remaining = {record.session_id for record in store.sessions.values()}
    assert remaining == ({other.session_id} if replace_with == "change" else set())


@pytest.mark.asyncio
async def test_a_hash_upgrade_does_not_undo_a_password_reset_made_meanwhile():
    store = MemoryAccountStore()
    old = ScryptParams(n=2**3, r=1, p=1)
    await build_service(store=store, params=old).create_owner(_create())
    service = build_service(store=store)
    original = store.update_owner

    async def reset_first(user_id, fields, *, if_password_id=None):
        # The reset lands between the sign-in's check and its upgrade write.
        store.update_owner = original
        await service.reset_password(PasswordReset(password="reset to this one"))
        return await original(user_id, fields, if_password_id=if_password_id)

    store.update_owner = reset_first
    # The old password was checked before the reset, so it no longer signs in.
    with pytest.raises(InvalidCredentials):
        await _signed_in(service)
    assert store.sessions == {}

    await service.sign_in(
        _sign_in(password="reset to this one"), address="b", user_agent=""
    )
    with pytest.raises(InvalidCredentials):
        await service.sign_in(_sign_in(), address="c", user_agent="")


@pytest.mark.asyncio
async def test_a_setup_code_used_by_another_request_first_is_refused():
    store = MemoryAccountStore()
    service = build_service(store=store)
    issued = await service.issue_setup_code()

    async def already_used(code_hash, since):
        return False

    store.consume_setup_code = already_used

    with pytest.raises(InvalidSetupCode):
        await service.setup(
            SetupRequest(code=issued.code, **OWNER), address="a", user_agent=""
        )
    assert await service.exists() is False


# ---- A hash upgrade is not a password change ----


async def _owner_with_an_old_hash() -> tuple[MemoryAccountStore, object]:
    """A store whose owner's hash needs an upgrade, and a session for it."""
    store = MemoryAccountStore()
    old = build_service(store=store, params=ScryptParams(n=2**3, r=1, p=1))
    await old.create_owner(_create())
    session = await old.resolve((await _signed_in(old)).token)
    return store, session


@pytest.mark.asyncio
async def test_two_sign_ins_at_once_both_succeed_while_the_hash_is_upgraded():
    store, _ = await _owner_with_an_old_hash()
    sleeper = Sleeper()
    service = build_service(store=store, sleeper=sleeper)

    first, second = await asyncio.gather(_signed_in(service), _signed_in(service))

    assert first.token != second.token
    assert await service.resolve(first.token) is not None
    assert await service.resolve(second.token) is not None
    assert store.owner.password.n == 2**4
    # Neither was counted as a failure.
    await _signed_in(service)
    assert sleeper.delays[-1] == 0


@pytest.mark.asyncio
async def test_a_password_change_succeeds_when_a_sign_in_upgrades_the_hash_meanwhile():
    store, session = await _owner_with_an_old_hash()
    service = build_service(store=store)
    original = store.update_owner

    async def upgrade_first(*args, **kwargs):
        # A sign-in upgrades the hash between the change's check and write.
        store.update_owner = original
        await _signed_in(service)
        assert store.owner.password.n == 2**4
        return await original(*args, **kwargs)

    store.update_owner = upgrade_first

    await service.change_password(
        session,
        PasswordChange(current_password=PASSWORD, new_password="a brand new one"),
    )

    await service.sign_in(
        _sign_in(password="a brand new one"), address="b", user_agent=""
    )


# ---- Setup against a reset, and a failure after the session is stored ----


@pytest.mark.asyncio
async def test_a_setup_overtaken_by_a_reset_leaves_no_session():
    store = MemoryAccountStore()
    service = build_service(store=store)
    issued = await service.issue_setup_code()
    original = store.create_session

    async def reset_first(record):
        store.create_session = original
        await service.reset_password(PasswordReset(password="reset to this one"))
        await original(record)

    store.create_session = reset_first

    with pytest.raises(InvalidCredentials):
        await service.setup(
            SetupRequest(code=issued.code, **OWNER), address="a", user_agent=""
        )

    assert store.sessions == {}
    assert await service.exists() is True


@pytest.mark.asyncio
async def test_a_failure_after_the_session_is_stored_does_not_leave_it_behind():
    store = MemoryAccountStore()
    service = build_service(store=store)
    await service.create_owner(_create())
    original = store.get_owner
    calls = 0

    async def fail_on_the_second_read():
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("store down")
        return await original()

    store.get_owner = fail_on_the_second_read

    with pytest.raises(RuntimeError):
        await _signed_in(service)

    assert store.sessions == {}


# ---- Password change and the pending bound ----


@pytest.mark.asyncio
async def test_a_password_change_is_refused_beyond_the_pending_limit():
    from account.protocols import TooManyAttempts
    from account.service import _PENDING_LIMIT

    release = asyncio.Event()

    async def wait(_seconds: float) -> None:
        await release.wait()

    service = build_service(sleeper=wait)
    await service.create_owner(_create())
    release.set()
    session = await service.resolve((await _signed_in(service)).token)
    release.clear()
    wrong = PasswordChange(
        current_password="wrong password!", new_password="a brand new one"
    )
    held = [
        asyncio.ensure_future(service.change_password(session, wrong))
        for _ in range(_PENDING_LIMIT)
    ]
    await asyncio.sleep(0)

    # Held password changes count toward the bound, for every kind of check.
    # The time limit turns a missing bound into a failure, not a hang.
    with pytest.raises(TooManyAttempts):
        await asyncio.wait_for(service.change_password(session, wrong), 5)
    with pytest.raises(TooManyAttempts):
        await asyncio.wait_for(_signed_in(service), 5)

    # A cancelled one and the failed ones all give their place back.
    held[0].cancel()
    release.set()
    results = await asyncio.gather(*held, return_exceptions=True)
    assert isinstance(results[0], asyncio.CancelledError)
    assert all(isinstance(result, InvalidCredentials) for result in results[1:])

    # Every place is free again: the bound can be filled exactly once more.
    release.clear()
    again = [
        asyncio.ensure_future(service.change_password(session, wrong))
        for _ in range(_PENDING_LIMIT)
    ]
    await asyncio.sleep(0)
    assert not any(task.done() for task in again)
    with pytest.raises(TooManyAttempts):
        await service.change_password(session, wrong)
    release.set()
    await asyncio.gather(*again, return_exceptions=True)


@pytest.mark.asyncio
async def test_a_password_change_does_not_undo_a_reset_made_after_its_check():
    store = MemoryAccountStore()
    service = build_service(store=store)
    await service.create_owner(_create())
    session = await service.resolve((await _signed_in(service)).token)
    original = store.update_owner

    async def reset_first(*args, **kwargs):
        # The reset lands between the change's password check and its write.
        store.update_owner = original
        await service.reset_password(PasswordReset(password="reset to this one"))
        return await original(*args, **kwargs)

    store.update_owner = reset_first

    with pytest.raises(InvalidCredentials):
        await service.change_password(
            session,
            PasswordChange(current_password=PASSWORD, new_password="a brand new one"),
        )
    await service.sign_in(
        _sign_in(password="reset to this one"), address="b", user_agent=""
    )


@pytest.mark.asyncio
async def test_a_cancellation_while_the_session_is_stored_does_not_leave_it_behind():
    store = MemoryAccountStore()
    service = build_service(store=store)
    await service.create_owner(_create())
    original = store.create_session
    stored = asyncio.Event()

    async def store_then_hang(record):
        await original(record)
        stored.set()
        await asyncio.Event().wait()

    store.create_session = store_then_hang
    task = asyncio.ensure_future(_signed_in(service))
    await stored.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert store.sessions == {}


@pytest.mark.asyncio
async def test_a_replaced_password_is_refused_even_if_the_session_cannot_be_deleted():
    store = MemoryAccountStore()
    service = build_service(store=store)
    await service.create_owner(_create())
    original = store.create_session

    async def reset_first(record):
        store.create_session = original
        await service.reset_password(PasswordReset(password="reset to this one"))
        await original(record)

        async def fail(user_id, session_id):
            raise RuntimeError("store down")

        store.delete_session = fail

    store.create_session = reset_first

    with pytest.raises(InvalidCredentials):
        await _signed_in(service)


@pytest.mark.asyncio
async def test_a_failed_cleanup_keeps_the_first_error_and_other_browsers():
    store = MemoryAccountStore()
    service = build_service(store=store)
    await service.create_owner(_create())
    other = await service.resolve((await _signed_in(service)).token)
    read_owner = store.get_owner
    delete_session = store.delete_session
    calls = 0
    deleted: list[str] = []

    async def fail_on_the_second_read():
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("read failed")
        return await read_owner()

    async def delete_then_fail(user_id, session_id):
        deleted.append(session_id)
        await delete_session(user_id, session_id)
        raise ValueError("delete failed")

    store.get_owner = fail_on_the_second_read
    store.delete_session = delete_then_fail

    # The read's error is the outcome, not the cleanup's.
    with pytest.raises(RuntimeError, match="read failed"):
        await _signed_in(service)

    # Only the new session was removed; the other browser stays signed in.
    assert len(deleted) == 1
    assert set(store.sessions) == {other.session_id}


# ---- The owner a Source API request acts for ----


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "admins", "expected"),
    [
        ("mock", frozenset(), OWNER_USER_ID),
        # A mock install may have the field set and ignored.
        ("mock", frozenset({"user_abc", "user_def"}), OWNER_USER_ID),
        ("clerk", frozenset(), OWNER_USER_ID),
        ("clerk", frozenset({"user_abc"}), "user_abc"),
    ],
)
async def test_acting_owner_follows_the_id_rule_before_the_account(
    mode, admins, expected
):
    service = build_service(auth_mode=mode, admin_user_ids=admins)

    owner = await service.acting_owner()

    assert owner.user_id == expected


@pytest.mark.asyncio
async def test_acting_owner_is_unknown_with_several_admins_and_no_account():
    service = build_service(
        auth_mode="clerk", admin_user_ids=frozenset({"user_a", "user_b"})
    )
    with pytest.raises(OwnerIdRequired):
        await service.acting_owner()

    await service.create_owner(_create(owner_id="user_b"))
    owner = await service.acting_owner()
    assert (owner.user_id, owner.name) == ("user_b", "Ada")

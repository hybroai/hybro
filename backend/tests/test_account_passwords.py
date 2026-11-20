"""Password hashing with the standard library's scrypt."""

from __future__ import annotations

import hashlib

import pytest

from account.passwords import (
    DEFAULT_PARAMS,
    ScryptParams,
    hash_password,
    needs_upgrade,
    verify_password,
)

FAST = ScryptParams(n=2**4, r=1, p=1)


def test_a_hash_verifies_its_password_only():
    stored = hash_password("correct horse", FAST)

    assert verify_password("correct horse", stored)
    assert not verify_password("correct horsf", stored)
    assert not verify_password("", stored)


def test_the_parameters_and_a_fresh_salt_are_stored_beside_each_hash():
    first = hash_password("same password", FAST)
    second = hash_password("same password", FAST)

    assert (first.algorithm, first.n, first.r, first.p) == ("scrypt", 2**4, 1, 1)
    assert first.salt != second.salt
    assert first.hash != second.hash
    assert "same password" not in first.model_dump_json()


def test_a_hash_made_with_other_parameters_still_verifies_and_needs_an_upgrade():
    old = hash_password("pw-pw-pw-pw", ScryptParams(n=2**3, r=1, p=1))

    assert verify_password("pw-pw-pw-pw", old)
    assert needs_upgrade(old, FAST)
    assert not needs_upgrade(hash_password("pw-pw-pw-pw", FAST), FAST)


def test_default_parameters_exceed_the_library_memory_default_and_still_hash():
    # hashlib.scrypt's default maxmem is 32 MiB; the default parameters need
    # more, so hashing only works because maxmem is passed explicitly.
    needed = 128 * DEFAULT_PARAMS.r * (DEFAULT_PARAMS.n + DEFAULT_PARAMS.p + 2)
    assert needed > 32 * 1024 * 1024
    with pytest.raises(ValueError):
        hashlib.scrypt(
            b"x",
            salt=b"0" * 16,
            n=DEFAULT_PARAMS.n,
            r=DEFAULT_PARAMS.r,
            p=DEFAULT_PARAMS.p,
        )

    stored = hash_password("a long enough password")

    assert verify_password("a long enough password", stored)
    assert not needs_upgrade(stored, DEFAULT_PARAMS)

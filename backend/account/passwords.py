"""Password hashing with the standard library's scrypt.

The parameters are stored beside each hash, so they can be raised later: an
older hash still verifies and is replaced at the owner's next sign-in.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from dataclasses import dataclass

from account.models import PasswordHash

_SALT_BYTES = 16
_KEY_BYTES = 32


@dataclass(frozen=True, slots=True)
class ScryptParams:
    n: int
    r: int
    p: int


# About 32 MiB and three passes per hash. hashlib's default ``maxmem`` is
# 32 MiB, just under what these need, so every call passes its own bound.
DEFAULT_PARAMS = ScryptParams(n=2**15, r=8, p=3)


def _derive(password: str, salt: bytes, params: ScryptParams) -> bytes:
    return hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=params.n,
        r=params.r,
        p=params.p,
        # Twice the working memory of these parameters.
        maxmem=2 * 128 * params.r * (params.n + params.p + 2),
        dklen=_KEY_BYTES,
    )


def hash_password(password: str, params: ScryptParams = DEFAULT_PARAMS) -> PasswordHash:
    salt = secrets.token_bytes(_SALT_BYTES)
    return PasswordHash(
        n=params.n,
        r=params.r,
        p=params.p,
        salt=base64.b64encode(salt).decode("ascii"),
        hash=base64.b64encode(_derive(password, salt, params)).decode("ascii"),
    )


def verify_password(password: str, stored: PasswordHash) -> bool:
    derived = _derive(
        password,
        base64.b64decode(stored.salt),
        ScryptParams(n=stored.n, r=stored.r, p=stored.p),
    )
    return hmac.compare_digest(derived, base64.b64decode(stored.hash))


def needs_upgrade(stored: PasswordHash, params: ScryptParams = DEFAULT_PARAMS) -> bool:
    return (stored.n, stored.r, stored.p) != (params.n, params.r, params.p)


__all__ = [
    "DEFAULT_PARAMS",
    "ScryptParams",
    "hash_password",
    "needs_upgrade",
    "verify_password",
]

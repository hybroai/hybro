"""The owner's sign-in cookie, and the dependency that resolves it.

Nothing but the ``/auth`` routes uses the dependency yet: the other routes
keep ``common.auth.get_current_user`` until sign-in is enforced.
"""

from __future__ import annotations

from typing import Annotated, NoReturn

from fastapi import Depends, HTTPException, Request, Response

from account.protocols import (
    AccountExists,
    BrowserNotFound,
    InvalidCredentials,
    InvalidSetupCode,
    NoAccount,
    OwnerAccount,
    OwnerIdRequired,
    SignedIn,
    TooManyAttempts,
)

SESSION_COOKIE = "hybro_session"
# The browser keeps the cookie as long as an unused session stays valid.
SESSION_COOKIE_MAX_AGE = 30 * 24 * 3600


def get_owner_account(request: Request) -> OwnerAccount:
    account: OwnerAccount | None = getattr(request.app.state, "owner_account", None)
    if account is None:
        raise HTTPException(
            503,
            detail={
                "code": "account_unavailable",
                "message": "The owner account is unavailable.",
            },
        )
    return account


OwnerAccountDependency = Annotated[OwnerAccount, Depends(get_owner_account)]


async def get_optional_owner_session(
    request: Request, account: OwnerAccountDependency
) -> SignedIn | None:
    token = request.cookies.get(SESSION_COOKIE)
    return await account.resolve(token) if token else None


async def get_owner_session(
    session: Annotated[SignedIn | None, Depends(get_optional_owner_session)],
) -> SignedIn:
    """The signed-in owner. Only the cookie counts; a bearer header does not."""
    if session is None:
        raise HTTPException(
            401,
            detail={"code": "not_signed_in", "message": "Sign in to continue."},
        )
    return session


OptionalOwnerSessionDependency = Annotated[
    SignedIn | None, Depends(get_optional_owner_session)
]
OwnerSessionDependency = Annotated[SignedIn, Depends(get_owner_session)]


def set_session_cookie(request: Request, response: Response, token: str) -> None:
    """Host-only, so it is sent to every port of this host and nowhere else."""
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=SESSION_COOKIE_MAX_AGE,
        path="/",
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
    )


def clear_session_cookie(request: Request, response: Response) -> None:
    response.delete_cookie(
        SESSION_COOKIE,
        path="/",
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
    )


def _refuse(status: int, code: str, message: str, **extra: object) -> NoReturn:
    raise HTTPException(status, detail={"code": code, "message": message, **extra})


def refuse_account_error(exc: Exception) -> NoReturn:
    """The HTTP answer for a refusal of the account module."""
    if isinstance(exc, AccountExists):
        _refuse(409, "account_exists", "The owner account already exists.")
    if isinstance(exc, NoAccount):
        _refuse(409, "no_account", "No owner account exists yet.")
    if isinstance(exc, InvalidSetupCode):
        _refuse(
            403,
            "invalid_setup_code",
            "This setup link is not valid. Run `hybro` for a new one.",
        )
    if isinstance(exc, InvalidCredentials):
        _refuse(401, "invalid_credentials", "The email or the password is wrong.")
    if isinstance(exc, OwnerIdRequired):
        _refuse(
            409,
            "owner_id_required",
            "Choose which administrator the owner account takes over.",
            choices=list(exc.choices),
        )
    if isinstance(exc, TooManyAttempts):
        _refuse(
            429, "too_many_attempts", "Too many attempts at once. Try again shortly."
        )
    if isinstance(exc, BrowserNotFound):
        _refuse(404, "browser_not_found", "No signed-in browser has this id.")
    raise exc


ACCOUNT_ERRORS = (
    AccountExists,
    NoAccount,
    InvalidSetupCode,
    InvalidCredentials,
    OwnerIdRequired,
    TooManyAttempts,
    BrowserNotFound,
)

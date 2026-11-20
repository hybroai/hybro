"""The owner's setup, sign-in and account pages; behaviour lives in ``account``.

Status, setup and sign-in take no credential. The other routes take the
owner's sign-in cookie and nothing else.
"""

from __future__ import annotations

from fastapi import APIRouter, Request, Response

from account.protocols import (
    AccountStatus,
    BrowserList,
    OwnerProfile,
    PasswordChange,
    ProfileUpdate,
    SetupRequest,
    SignInRequest,
)
from api_gateway.errors import QuietValidationRoute
from api_gateway.owner_session import (
    ACCOUNT_ERRORS,
    OptionalOwnerSessionDependency,
    OwnerAccountDependency,
    OwnerSessionDependency,
    clear_session_cookie,
    refuse_account_error,
    set_session_cookie,
)
from api_gateway.registry import mark_declared_owner

router = APIRouter(prefix="/auth", route_class=QuietValidationRoute)


def _caller(request: Request) -> tuple[str, str]:
    address = request.client.host if request.client else "unknown"
    return address, request.headers.get("user-agent", "")


@router.get("/status")
async def auth_status(
    account: OwnerAccountDependency, session: OptionalOwnerSessionDependency
) -> AccountStatus:
    if session is not None:
        return AccountStatus(account_exists=True, owner=session.owner)
    return AccountStatus(account_exists=await account.exists())


@router.post("/setup")
async def auth_setup(
    body: SetupRequest,
    request: Request,
    response: Response,
    account: OwnerAccountDependency,
) -> OwnerProfile:
    address, user_agent = _caller(request)
    try:
        new = await account.setup(body, address=address, user_agent=user_agent)
    except ACCOUNT_ERRORS as exc:
        refuse_account_error(exc)
    set_session_cookie(request, response, new.token)
    return new.owner


@router.post("/sign-in")
async def auth_sign_in(
    body: SignInRequest,
    request: Request,
    response: Response,
    account: OwnerAccountDependency,
) -> OwnerProfile:
    address, user_agent = _caller(request)
    try:
        new = await account.sign_in(body, address=address, user_agent=user_agent)
    except ACCOUNT_ERRORS as exc:
        refuse_account_error(exc)
    set_session_cookie(request, response, new.token)
    return new.owner


@router.post("/sign-out", status_code=204, response_class=Response)
async def auth_sign_out(
    request: Request,
    account: OwnerAccountDependency,
    session: OptionalOwnerSessionDependency,
) -> Response:
    if session is not None:
        await account.sign_out(session)
    response = Response(status_code=204)
    clear_session_cookie(request, response)
    return response


@router.patch("/profile")
async def auth_update_profile(
    body: ProfileUpdate,
    account: OwnerAccountDependency,
    session: OwnerSessionDependency,
) -> OwnerProfile:
    try:
        return await account.update_profile(session, body)
    except ACCOUNT_ERRORS as exc:
        refuse_account_error(exc)


@router.post("/password", status_code=204, response_class=Response)
async def auth_change_password(
    body: PasswordChange,
    account: OwnerAccountDependency,
    session: OwnerSessionDependency,
) -> Response:
    try:
        await account.change_password(session, body)
    except ACCOUNT_ERRORS as exc:
        refuse_account_error(exc)
    return Response(status_code=204)


@router.get("/browsers")
async def auth_list_browsers(
    account: OwnerAccountDependency, session: OwnerSessionDependency
) -> BrowserList:
    return await account.list_browsers(session)


@router.delete("/browsers/{browser_id}", status_code=204, response_class=Response)
async def auth_revoke_browser(
    browser_id: str,
    account: OwnerAccountDependency,
    session: OwnerSessionDependency,
) -> Response:
    try:
        await account.revoke_browser(session, browser_id)
    except ACCOUNT_ERRORS as exc:
        refuse_account_error(exc)
    return Response(status_code=204)


mark_declared_owner(router, "api_gateway.routes.auth_routes")

"""Machine-only routes; behaviour lives in ``account``.

The ``hybro`` command reaches them from inside the container, over loopback,
with the per-start token of the handoff file the entrypoint writes. Caddy
refuses the path, so no request from outside the container arrives here. The
header is read from the request, not declared, so the schema does not
advertise it. The owner's cookie and a bearer token open nothing here.
"""

from __future__ import annotations

import hmac
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from account.protocols import (
    OwnerAccount,
    OwnerCreate,
    OwnerProfile,
    PasswordReset,
    SetupCode,
)
from api_gateway.errors import QuietValidationRoute
from api_gateway.owner_session import (
    ACCOUNT_ERRORS,
    get_owner_account,
    refuse_account_error,
)
from api_gateway.registry import mark_declared_owner

TOKEN_HEADER = "X-Hybro-Machine-Token"

router = APIRouter(prefix="/machine", route_class=QuietValidationRoute)


def require_machine_token(request: Request) -> None:
    expected: str = getattr(request.app.state, "machine_token", "")
    given = request.headers.get(TOKEN_HEADER, "")
    # No handoff file (a checkout) means no token, and then nothing matches.
    if not expected or not hmac.compare_digest(
        given.encode("utf-8"), expected.encode("utf-8")
    ):
        raise HTTPException(
            401,
            detail={
                "code": "machine_token_required",
                "message": "This route is for the hybro command on the machine.",
            },
        )


def get_machine_account(
    _token: Annotated[None, Depends(require_machine_token)],
    account: Annotated[OwnerAccount, Depends(get_owner_account)],
) -> OwnerAccount:
    return account


MachineAccountDependency = Annotated[OwnerAccount, Depends(get_machine_account)]


@router.post("/setup-code")
async def machine_issue_setup_code(account: MachineAccountDependency) -> SetupCode:
    try:
        return await account.issue_setup_code()
    except ACCOUNT_ERRORS as exc:
        refuse_account_error(exc)


@router.post("/owner", status_code=201)
async def machine_create_owner(
    body: OwnerCreate, account: MachineAccountDependency
) -> OwnerProfile:
    try:
        return await account.create_owner(body)
    except ACCOUNT_ERRORS as exc:
        refuse_account_error(exc)


@router.post("/owner/password", status_code=204, response_class=Response)
async def machine_reset_password(
    body: PasswordReset, account: MachineAccountDependency
) -> Response:
    try:
        await account.reset_password(body)
    except ACCOUNT_ERRORS as exc:
        refuse_account_error(exc)
    return Response(status_code=204)


mark_declared_owner(router, "api_gateway.routes.machine_routes")

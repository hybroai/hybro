"""The owner's check rules; behaviour lives in ``policy``.

Every route takes the owner's sign-in. The rules are stored and listed here;
nothing enforces them yet.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Annotated, Any, NoReturn

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute

from api_gateway.client_token import SignedInOwnerDependency
from api_gateway.registry import mark_declared_owner
from policy.protocols import (
    RuleCreate,
    RuleLimitReached,
    RuleList,
    RuleNotFound,
    RuleOrder,
    RuleOrderMismatch,
    RuleRegistry,
    RuleReplace,
    RuleView,
)

# The body fields a validation error may name. Any other key is reported as
# an unknown field, so nothing the caller wrote is echoed.
_FIELDS = (
    frozenset(RuleCreate.model_fields)
    | frozenset(RuleReplace.model_fields)
    | frozenset(RuleOrder.model_fields)
)


def _invalid_fields(exc: RequestValidationError) -> list[str]:
    names: set[str] = set()
    for error in exc.errors():
        loc = error.get("loc", ())
        field = loc[1] if len(loc) > 1 and loc[0] == "body" else None
        if error.get("type") == "extra_forbidden":
            names.add("unknown field")
        elif field in _FIELDS:
            names.add(str(field))
        elif isinstance(field, str) and error.get("type") != "json_invalid":
            names.add("unknown field")
        else:
            # Not JSON, or not an object: there is no field to name.
            names.add("body")
    return sorted(names)


class _RuleRoute(APIRoute):
    """Validation failures name the field and never echo the input."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        original = super().get_route_handler()

        async def handle(request: Request) -> Response:
            try:
                return await original(request)
            except RequestValidationError as exc:
                fields = _invalid_fields(exc)
                return JSONResponse(
                    {
                        "detail": {
                            "code": "invalid_request",
                            "message": "Invalid request; check: "
                            + ", ".join(fields)
                            + ".",
                            "fields": fields,
                        }
                    },
                    status_code=422,
                )

        return handle


router = APIRouter(prefix="/rules", route_class=_RuleRoute)

_ERRORS = {
    401: {"description": "Authentication required"},
    403: {"description": "Only the owner manages rules"},
    503: {"description": "The owner is not known yet, or rules are unavailable"},
}
_RULE_ERRORS = {**_ERRORS, 404: {"description": "No rule has this id"}}
_CREATE_ERRORS = {**_ERRORS, 409: {"description": "The list holds the most rules"}}
_ORDER_ERRORS = {
    **_ERRORS,
    409: {"description": "The order does not name every rule exactly once"},
}


def _refuse(status: int, code: str, message: str) -> NoReturn:
    raise HTTPException(status, detail={"code": code, "message": message})


async def get_owner_rules(
    request: Request, owner: SignedInOwnerDependency
) -> RuleRegistry:
    """Owner only; a client token cannot call these routes."""
    rules: RuleRegistry | None = getattr(request.app.state, "policy_rules", None)
    if rules is None:
        _refuse(503, "rules_unavailable", "Rules are unavailable.")
    return rules


OwnerRulesDependency = Annotated[RuleRegistry, Depends(get_owner_rules)]


def _not_found() -> NoReturn:
    _refuse(404, "rule_not_found", "No rule has this id.")


@router.get("", responses=_ERRORS)
async def list_rules(rules: OwnerRulesDependency) -> RuleList:
    """The check rules in order; the first that matches decides."""
    return await rules.list_rules()


@router.post("", status_code=201, responses=_CREATE_ERRORS)
async def create_rule(body: RuleCreate, rules: OwnerRulesDependency) -> RuleView:
    """Add a check rule at the end of the list."""
    try:
        return await rules.create(body)
    except RuleLimitReached:
        _refuse(409, "rule_limit_reached", "The list holds the most rules it can.")


@router.put("/order", responses=_ORDER_ERRORS)
async def reorder_rules(body: RuleOrder, rules: OwnerRulesDependency) -> RuleList:
    """Put the rules in a new order. It names every rule exactly once."""
    try:
        return await rules.reorder(body.rule_ids)
    except RuleOrderMismatch:
        _refuse(
            409,
            "rule_order_mismatch",
            "The order must name every rule exactly once; reload the rules.",
        )


@router.put("/{rule_id}", responses=_RULE_ERRORS)
async def update_rule(
    rule_id: str, body: RuleReplace, rules: OwnerRulesDependency
) -> RuleView:
    """Replace a rule's condition, answer and state; each is required. Its
    place stays."""
    try:
        return await rules.update(rule_id, body)
    except RuleNotFound:
        _not_found()


@router.delete("/{rule_id}", status_code=204, responses=_RULE_ERRORS)
async def delete_rule(rule_id: str, rules: OwnerRulesDependency) -> Response:
    try:
        await rules.delete(rule_id)
    except RuleNotFound:
        _not_found()
    return Response(status_code=204)


mark_declared_owner(router, "api_gateway.routes.rule_routes")

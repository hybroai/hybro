import json
from pathlib import Path

from fastapi.routing import APIRoute

FIXTURE_DIR = Path(__file__).parent / "fixtures"
AUTH_DEPENDENCY_NAMES = {
    "get_api_key",
    "get_api_key_no_track",
    "get_current_user",
    "get_current_user_or_service",
    "get_current_user_with_query_token",
    "get_optional_user",
}


def _load_fixture(name: str):
    return json.loads((FIXTURE_DIR / name).read_text())


def _route_inventory(app):
    from api_gateway.registry import resolve_declared_owner

    rows = []
    for route in sorted(
        app.routes,
        key=lambda r: (
            getattr(r, "path", ""),
            sorted(getattr(r, "methods", []) or []),
            getattr(r, "name", ""),
        ),
    ):
        path = getattr(route, "path", "")
        if not path.startswith("/api/") and path != "/health":
            continue
        endpoint = getattr(route, "endpoint", None)
        dependencies = []
        auth_dependencies = []
        if isinstance(route, APIRoute):
            dependencies = sorted(
                getattr(dependency.call, "__name__", repr(dependency.call))
                for dependency in route.dependant.dependencies
            )
            auth_dependencies = [
                dependency
                for dependency in dependencies
                if dependency in AUTH_DEPENDENCY_NAMES
            ]
        rows.append(
            {
                "path": path,
                "methods": sorted(getattr(route, "methods", []) or []),
                "name": getattr(route, "name", ""),
                "endpoint_module": getattr(endpoint, "__module__", ""),
                "declared_owner": resolve_declared_owner(route),
                "tags": list(getattr(route, "tags", []) or []),
                "status_code": getattr(route, "status_code", None),
                "dependencies": dependencies,
                "auth_dependencies": auth_dependencies,
                "include_in_schema": getattr(route, "include_in_schema", None),
            }
        )
    return rows


def _contract_key(row):
    return (row["path"], tuple(row["methods"]), row["name"], tuple(row["tags"]))


def test_public_route_inventory_matches_pre_gateway_contract():
    from main import app

    before = _load_fixture("api_gateway_route_inventory_before.json")
    current = _route_inventory(app)

    assert [_contract_key(row) for row in current] == [
        _contract_key(row) for row in before
    ]


def test_removed_routes_are_absent_from_checked_in_openapi():
    openapi = _load_fixture("../../openapi.json")

    assert {
        "/api/v1/agents",
        "/api/v1/agents/{item_id}",
        "/api/v1/roomCenter/inquiryRoomsByRoomOwnerId",
        "/api/v1/roomCenter/updateRoomExtendInfo",
    }.isdisjoint(openapi["paths"])
    schemas = openapi.get("components", {}).get("schemas", {})
    assert {
        "AgentCreate",
        "AgentPatch",
        "AgentResponse",
        "AgentUpdate",
        "PaginatedResponse_AgentResponse_",
        "PaginationMeta",
    }.isdisjoint(schemas)


def test_security_hardening_contract_changes_are_explicitly_documented():
    from main import app

    exceptions = _load_fixture("api_gateway_security_hardening_exceptions.json")
    current = {
        (row["path"], tuple(row["methods"]), row["name"]): row
        for row in _route_inventory(app)
    }

    assert exceptions
    bad = []
    for exception in exceptions:
        key = (
            exception["path"],
            tuple(exception["methods"]),
            exception["name"],
        )
        row = current.get(key)
        if row is None:
            bad.append((key, "missing route"))
            continue
        if not exception.get("reason"):
            bad.append((key, "missing reason"))
        for field, expected in exception["expected_contract"].items():
            if row[field] != expected:
                bad.append((key, field, row[field], expected))

    assert bad == []


def test_expected_gateway_inventory_records_full_route_contract():
    expected = _load_fixture("api_gateway_route_inventory_expected.json")

    required = {
        "path",
        "methods",
        "name",
        "endpoint_module",
        "declared_owner",
        "tags",
        "status_code",
        "dependencies",
        "auth_dependencies",
        "include_in_schema",
    }

    missing = [
        (row["path"], row["name"], sorted(required - set(row)))
        for row in expected
        if not required.issubset(row)
    ]

    assert missing == []


def test_all_api_routes_are_declared_by_api_gateway():
    from main import app

    expected = _load_fixture("api_gateway_route_inventory_expected.json")
    current = _route_inventory(app)

    expected_by_route = {
        (row["path"], tuple(row["methods"]), row["name"]): row for row in expected
    }
    current_by_route = {
        (row["path"], tuple(row["methods"]), row["name"]): row for row in current
    }

    assert current_by_route.keys() == expected_by_route.keys()

    bad = []
    for key, row in current_by_route.items():
        expected_row = expected_by_route[key]
        owner = row["declared_owner"]
        expected_owner = expected_row["declared_owner"]
        if row["path"] == "/health":
            if owner != "main":
                bad.append((row["path"], owner, "main"))
        elif owner != expected_owner or not owner.startswith("api_gateway.routes."):
            bad.append((row["path"], owner, expected_owner))
        for field in (
            "endpoint_module",
            "tags",
            "status_code",
            "dependencies",
            "auth_dependencies",
            "include_in_schema",
        ):
            if row[field] != expected_row[field]:
                bad.append((row["path"], field, row[field], expected_row[field]))

    assert bad == []


def test_public_api_routes_do_not_use_old_api_endpoint_modules():
    from main import app

    bad = []
    for row in _route_inventory(app):
        if not row["path"].startswith("/api/"):
            continue
        endpoint_module = row["endpoint_module"]
        if endpoint_module.startswith("api."):
            bad.append((row["path"], row["name"], endpoint_module))

    assert bad == []

import ast
import importlib
import inspect
import json
from dataclasses import fields, is_dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.routing import APIRoute

FORBIDDEN_API_IMPORT_PREFIXES = (
    "database",
    "motor",
    "modules",
    "pymongo",
    "services",
    "delivery",
    "execution",
    "hub_runtime_bridge",
    "agent.repository",
    "room.repository",
    "context_memory.repository",
)

ALLOWLIST_PATH = Path("tests/fixtures/phase9_import_allowlist.json")


def _imported_module(node: ast.AST) -> str | None:
    if isinstance(node, ast.Import):
        return None
    if isinstance(node, ast.ImportFrom):
        return node.module
    return None


def _import_names(node: ast.AST) -> list[tuple[str, str]]:
    if isinstance(node, ast.Import):
        return [(alias.name, alias.name) for alias in node.names]
    if isinstance(node, ast.ImportFrom) and node.module is not None:
        return [(f"{node.module}.{alias.name}", node.module) for alias in node.names]
    return []


def _is_forbidden(module: str) -> bool:
    return any(
        module == prefix or module.startswith(f"{prefix}.")
        for prefix in FORBIDDEN_API_IMPORT_PREFIXES
    )


def _annotation_has_broad_shape(annotation: ast.AST | None) -> bool:
    if annotation is None:
        return False
    for node in ast.walk(annotation):
        if isinstance(node, ast.Name) and node.id == "Any":
            return True
        if isinstance(node, ast.Name) and node.id == "object":
            return True
        if isinstance(node, ast.Attribute) and node.attr == "Any":
            return True
        if isinstance(node, ast.Constant) and node.value is Ellipsis:
            return True
    return False


def _annotation_contains_broad_object(annotation) -> bool:
    if annotation is inspect.Signature.empty:
        return False
    if annotation is object:
        return True
    text = str(annotation)
    return (
        " object" in text
        or "[str, object]" in text
        or "| object" in text
        or "typing.Any" in text
    )


def test_route_protocol_broad_shape_rules_cover_nested_any_and_bare_containers():
    from typing import Any, get_args, get_origin

    from common.dto.base import FrozenDTO
    from common.protocols import JsonValue

    class NestedBroadDTO(FrozenDTO):
        payload: dict[str, Any]

    def annotation_is_broad(annotation, seen: set[object] | None = None) -> bool:
        if seen is None:
            seen = set()
        if annotation in seen:
            return False
        seen.add(annotation)
        if annotation in {Any, object, inspect.Signature.empty}:
            return True
        if annotation in {dict, list, set, tuple}:
            return True
        origin = get_origin(annotation)
        if origin is None:
            if inspect.isclass(annotation) and issubclass(annotation, FrozenDTO):
                return any(
                    annotation_is_broad(field.annotation, seen)
                    for field in annotation.model_fields.values()
                )
            return False
        if origin in {dict, list, set, tuple} and not get_args(annotation):
            return True
        return any(annotation_is_broad(arg, seen) for arg in get_args(annotation))

    assert annotation_is_broad(dict)
    assert annotation_is_broad(list)
    assert annotation_is_broad(dict[str, Any])
    assert annotation_is_broad(list[dict[str, Any]])
    assert annotation_is_broad(NestedBroadDTO)
    assert not annotation_is_broad(dict[str, JsonValue])


def _load_allowlist() -> set[tuple[str, str]]:
    raw = json.loads(ALLOWLIST_PATH.read_text())
    allowed: set[tuple[str, str]] = set()
    for entry in raw:
        allowed.add((entry["path"], entry["import"]))
    return allowed


def _gateway_python_files() -> list[Path]:
    return sorted(Path("api_gateway/routes").glob("*.py"))


def _api_import_violations() -> list[str]:
    allowed = _load_allowlist()
    violations: list[str] = []
    for path in _gateway_python_files():
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Import | ast.ImportFrom):
                continue
            for imported_name, module in _import_names(node):
                if _is_forbidden(module) and (str(path), imported_name) not in allowed:
                    violations.append(f"{path}:{node.lineno}: {imported_name}")
    return violations


def test_legacy_api_package_is_not_reintroduced():
    assert not Path("api").exists()


def test_api_gateway_modules_are_thin_route_adapters():
    violations = _api_import_violations()

    assert not violations, "Forbidden API imports:\n" + "\n".join(violations)


def test_api_gateway_routes_do_not_import_other_routes_for_helpers():
    violations: list[str] = []
    for path in sorted(Path("api_gateway/routes").glob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or node.module is None:
                continue
            if node.module.startswith("api_gateway.routes."):
                violations.append(f"{path}:{node.lineno}: {node.module}")

    assert not violations, (
        "API Gateway routes import other route modules:\n" + "\n".join(violations)
    )


def test_api_gateway_bindings_do_not_expose_concrete_store_or_service_names():
    forbidden_names = {
        "mongodb",
        "mongo",
        "s3_service",
        "storage_service",
        "openai_service",
    }
    violations: list[str] = []

    for path in _gateway_python_files():
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in tree.body:
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                if node.target.id in forbidden_names:
                    violations.append(f"{path}:{node.lineno}: {node.target.id}")
            if isinstance(node, ast.FunctionDef) and node.name.startswith("bind_"):
                for arg in (*node.args.args, *node.args.kwonlyargs):
                    if arg.arg in forbidden_names:
                        violations.append(
                            f"{path}:{node.lineno}: {node.name}.{arg.arg}"
                        )

    assert not violations, (
        "API bindings expose concrete dependency names:\n" + "\n".join(violations)
    )


def test_api_bindings_do_not_use_any_typed_dependency_seams():
    violations: list[str] = []
    paths = _gateway_python_files()

    for path in sorted(paths):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in tree.body:
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                if _annotation_has_broad_shape(node.annotation):
                    violations.append(f"{path}:{node.lineno}: {node.target.id}")
            if isinstance(node, ast.FunctionDef) and node.name.startswith("bind_"):
                for arg in (*node.args.args, *node.args.kwonlyargs):
                    if _annotation_has_broad_shape(arg.annotation):
                        violations.append(
                            f"{path}:{node.lineno}: {node.name}.{arg.arg}"
                        )
            if isinstance(node, ast.FunctionDef) and node.name.startswith("get_"):
                if _annotation_has_broad_shape(node.returns):
                    violations.append(f"{path}:{node.lineno}: {node.name}.return")
            if isinstance(node, ast.AsyncFunctionDef):
                for arg in (*node.args.args, *node.args.kwonlyargs):
                    if _annotation_has_broad_shape(arg.annotation):
                        default = None
                        arg_names = [item.arg for item in node.args.args]
                        if arg.arg in arg_names:
                            index = arg_names.index(arg.arg)
                            default_index = index - (
                                len(arg_names) - len(node.args.defaults)
                            )
                            if default_index >= 0:
                                default = node.args.defaults[default_index]
                        if isinstance(default, ast.Call) and ast.unparse(
                            default.func
                        ).endswith("Depends"):
                            violations.append(
                                f"{path}:{node.lineno}: {node.name}.{arg.arg}"
                            )

    assert not violations, (
        "API bindings still use Any for dependency seams:\n" + "\n".join(violations)
    )


def test_phase9_route_inventory_is_recorded():
    routes = json.loads(Path("tests/fixtures/phase9_api_routes.json").read_text())

    assert routes
    for route in routes:
        assert {
            "path",
            "methods",
            "name",
            "auth_dependencies",
            "dependencies",
            "owning_protocol",
            "status_code",
            "openapi_response_codes",
            "response_class",
        }.issubset(route)


def test_phase9_route_inventory_matches_live_app_routes():
    from main import app

    docs_paths = {"/docs", "/docs/oauth2-redirect", "/openapi.json", "/redoc"}
    recorded_routes = json.loads(
        Path("tests/fixtures/phase9_api_routes.json").read_text()
    )
    openapi = app.openapi()
    recorded = {
        (
            route["path"],
            tuple(route["methods"]),
            route["name"],
        ): route
        for route in recorded_routes
        if route["path"] not in docs_paths
    }
    live = {}
    for route in app.routes:
        if not isinstance(route, APIRoute) or route.path in docs_paths:
            continue
        methods = tuple(
            sorted(
                method for method in route.methods if method not in {"HEAD", "OPTIONS"}
            )
        )
        response_model = (
            getattr(route.response_model, "__name__", str(route.response_model))
            if route.response_model is not None
            else None
        )
        response_class = getattr(route.response_class, "__name__", None)
        if response_class is None or response_class == "DefaultPlaceholder":
            response_class = None
        openapi_path = route.path_format
        method = next(iter(methods)).lower()
        openapi_responses = sorted(
            openapi["paths"][openapi_path][method].get("responses", {})
        )
        dependencies = sorted(
            getattr(dependency.call, "__name__", repr(dependency.call))
            for dependency in route.dependant.dependencies
        )
        auth_dependencies = [
            dependency
            for dependency in dependencies
            if dependency
            in {
                "get_api_key",
                "get_api_key_no_track",
                "get_current_user",
                "get_current_user_or_service",
                "get_current_user_with_query_token",
                "get_optional_user",
            }
        ]
        live[(route.path, methods, route.name)] = {
            "module": getattr(route.endpoint, "__module__", ""),
            "dependencies": dependencies,
            "auth_dependencies": auth_dependencies,
            "openapi_response_codes": openapi_responses,
            "response_model": response_model,
            "response_class": response_class,
            "status_code": route.status_code,
        }

    assert set(recorded) == set(live)
    for key, route in recorded.items():
        assert route["module"] == live[key]["module"]
        assert route["response_model"] == live[key]["response_model"]
        assert sorted(route["dependencies"]) == live[key]["dependencies"]
        assert sorted(route["auth_dependencies"]) == live[key]["auth_dependencies"]
        assert route["status_code"] == live[key]["status_code"]
        assert route["openapi_response_codes"] == live[key]["openapi_response_codes"]
        assert route["response_class"] == live[key]["response_class"]
        assert not route["owning_protocol"].startswith("blocked:")
        assert not any(
            protocol.startswith("blocked:")
            for protocol in route.get("supporting_protocols") or []
        )


def test_route_inventory_auth_dependencies_are_only_auth_dependencies():
    auth_dependency_names = {
        "get_api_key",
        "get_api_key_no_track",
        "get_current_user",
        "get_current_user_or_service",
        "get_current_user_with_query_token",
        "get_optional_user",
    }
    routes = json.loads(Path("tests/fixtures/phase9_api_routes.json").read_text())
    violations = [
        f"{route['path']} {route['name']}: {dependency}"
        for route in routes
        for dependency in route["auth_dependencies"]
        if dependency not in auth_dependency_names
    ]

    assert not violations, (
        "Route inventory auth_dependencies include non-auth dependencies:\n"
        + "\n".join(violations)
    )


def test_live_routes_do_not_duplicate_clerk_auth_dependency():
    from common.auth import get_current_user
    from main import app

    violations: list[str] = []
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        auth_dependencies = [
            dependency.call
            for dependency in route.dependant.dependencies
            if dependency.call is get_current_user
        ]
        if len(auth_dependencies) > 1:
            methods = ",".join(
                sorted(
                    method
                    for method in route.methods
                    if method not in {"HEAD", "OPTIONS"}
                )
            )
            violations.append(f"{methods} {route.path} {route.name}")

    assert not violations, "Routes duplicate Clerk auth dependency:\n" + "\n".join(
        violations
    )


def test_streaming_routes_record_sse_media_type_and_headers():
    from main import app

    recorded_routes = json.loads(
        Path("tests/fixtures/phase9_api_routes.json").read_text()
    )
    route_by_name = {route["name"]: route for route in recorded_routes}
    expected = {
        "stream_room_messages": {
            "media_type": "text/event-stream",
            "headers": [
                "Cache-Control",
                "Connection",
                "Content-Type",
                "X-Accel-Buffering",
            ],
        },
    }

    for name, streaming in expected.items():
        assert route_by_name[name]["streaming_response"] == streaming
        route = next(
            route
            for route in app.routes
            if isinstance(route, APIRoute) and route.name == name
        )
        source = inspect.getsource(route.endpoint)
        assert "StreamingResponse" in source
        assert f'media_type="{streaming["media_type"]}"' in source
        for header in streaming["headers"]:
            assert f'"{header}"' in source


def test_phase9_route_inventory_owners_resolve_to_real_symbols():
    routes = json.loads(Path("tests/fixtures/phase9_api_routes.json").read_text())
    symbolic_owners = {"fastapi.documentation"}
    missing: list[str] = []

    for route in routes:
        owner = route["owning_protocol"]
        if owner in symbolic_owners or owner.startswith("blocked:"):
            continue
        module_name, _, symbol_name = owner.rpartition(".")
        if not module_name or not symbol_name:
            missing.append(f"{route['path']}: {owner}")
            continue
        try:
            module = importlib.import_module(module_name)
        except ModuleNotFoundError as exc:
            missing.append(f"{route['path']}: {owner} ({exc})")
            continue
        if not hasattr(module, symbol_name):
            missing.append(f"{route['path']}: {owner}")

    assert not missing, "Unresolved route owners:\n" + "\n".join(missing)


def test_phase9_route_inventory_does_not_use_platform_implementation_owners():
    routes = json.loads(Path("tests/fixtures/phase9_api_routes.json").read_text())
    violations = [
        f"{route['path']}: {route['owning_protocol']}"
        for route in routes
        if route["owning_protocol"].startswith("platform_module.")
    ]

    assert not violations, (
        "Routes must use common protocols, not platform implementations:\n"
        + "\n".join(violations)
    )


def test_phase9_route_inventory_does_not_use_removed_runtime_bound_protocols():
    REMOVED_RUNTIME_PACKAGE = "app_" + "shell"
    removed_bound_module = f"{REMOVED_RUNTIME_PACKAGE}.bound"
    routes = json.loads(Path("tests/fixtures/phase9_api_routes.json").read_text())
    violations: list[str] = []

    for route in routes:
        protocol_paths = [
            route["owning_protocol"],
            *(route.get("supporting_protocols") or []),
        ]
        for protocol_path in protocol_paths:
            if protocol_path.startswith(f"{removed_bound_module}."):
                violations.append(f"{route['path']}: {protocol_path}")

    assert not violations, (
        "Routes must not use removed runtime protocol shims:\n" + "\n".join(violations)
    )


def test_phase9_route_inventory_has_no_blocked_owners_or_supporting_protocols():
    routes = json.loads(Path("tests/fixtures/phase9_api_routes.json").read_text())
    violations = []

    for route in routes:
        owner = route["owning_protocol"]
        if owner.startswith("blocked:"):
            violations.append(f"{route['path']} {route['name']}: {owner}")
        for protocol in route.get("supporting_protocols") or []:
            if protocol.startswith("blocked:"):
                violations.append(f"{route['path']} {route['name']}: {protocol}")

    assert not violations, "Blocked route protocols remain:\n" + "\n".join(violations)


def test_phase9_route_inventory_owners_are_protocol_symbols():
    routes = json.loads(Path("tests/fixtures/phase9_api_routes.json").read_text())
    symbolic_owners = {"fastapi.documentation"}
    violations: list[str] = []

    for route in routes:
        owner = route["owning_protocol"]
        if owner in symbolic_owners or owner.startswith("blocked:"):
            continue
        module_name, _, symbol_name = owner.rpartition(".")
        symbol = getattr(importlib.import_module(module_name), symbol_name)
        if not getattr(symbol, "_is_protocol", False):
            violations.append(f"{route['path']}: {owner}")

    assert not violations, "Route owners must resolve to Protocols:\n" + "\n".join(
        violations
    )


def test_phase9_route_inventory_supporting_protocols_are_protocol_symbols():
    routes = json.loads(Path("tests/fixtures/phase9_api_routes.json").read_text())
    violations: list[str] = []

    for route in routes:
        for protocol_path in route.get("supporting_protocols") or []:
            module_name, _, symbol_name = protocol_path.rpartition(".")
            try:
                symbol = getattr(importlib.import_module(module_name), symbol_name)
            except (AttributeError, ModuleNotFoundError) as exc:
                violations.append(f"{route['path']}: {protocol_path} ({exc})")
                continue
            if not getattr(symbol, "_is_protocol", False):
                violations.append(f"{route['path']}: {protocol_path}")

    assert not violations, (
        "Supporting route protocols must resolve to Protocols:\n"
        + "\n".join(violations)
    )


def test_api_key_management_routes_are_owned_by_store_protocol():
    routes = json.loads(Path("tests/fixtures/phase9_api_routes.json").read_text())
    violations = [
        f"{route['path']} {route['name']}: {route['owning_protocol']}"
        for route in routes
        if route["module"] == "api_gateway.routes.discovery_api_key_routes"
        and route["owning_protocol"] != "common.protocols.APIKeyStore"
    ]

    assert not violations, (
        "API-key management routes must use APIKeyStore owner:\n"
        + "\n".join(violations)
    )


def test_room_center_route_inventory_records_live_protocol_owners():
    routes = json.loads(Path("tests/fixtures/phase9_api_routes.json").read_text())
    by_name = {
        route["name"]: route
        for route in routes
        if route["module"] == "api_gateway.routes.room_routes"
    }
    expected = {
        "inquiry_active_runs": {
            "owner": "room.protocols.RoomCenterCompatibility",
            "supporting": {
                "common.protocols.ExecutionEngine",
                "common.protocols.RoomRouteReader",
            },
        },
        "inquiry_room_setting": {
            "owner": "room.protocols.RoomCenterCompatibility",
            "supporting": {
                "common.protocols.ExecutionEngine",
                "common.protocols.RoomRouteReader",
            },
        },
        "inquiry_room_messages": {
            "owner": "room.protocols.RoomCenterCompatibility",
            "supporting": {"common.protocols.RoomRouteReader"},
        },
        "update_room_agent_set": {
            "owner": "room.protocols.RoomCenterCompatibility",
            "supporting": {"common.protocols.RoomRouteReader"},
        },
        "update_room_name": {
            "owner": "room.protocols.RoomCenterCompatibility",
            "supporting": {"common.protocols.RoomRouteReader"},
        },
        "send_message": {
            "owner": "common.protocols.ExecutionEngine",
            "supporting": {
                "common.protocols.RoomRouteReader",
                "room.protocols.RoomCenterCompatibility",
            },
        },
        "suggest_agents": {
            "owner": "agent.protocols.AgentSuggestionService",
            "supporting": set(),
        },
    }
    violations: list[str] = []

    for name, expectation in expected.items():
        route = by_name[name]
        if route["owning_protocol"] != expectation["owner"]:
            violations.append(
                f"{name}: owner={route['owning_protocol']} expected={expectation['owner']}"
            )
        supporting = set(route.get("supporting_protocols") or [])
        if supporting != expectation["supporting"]:
            violations.append(
                f"{name}: supporting={sorted(supporting)} "
                f"expected={sorted(expectation['supporting'])}"
            )

    assert not violations, (
        "Room-center route inventory mismatches live protocols:\n"
        + "\n".join(violations)
    )


def test_room_center_protocol_inventory_matches_handler_calls():
    from api_gateway.routes import room_routes as room_center

    expectations = {
        "inquiry_active_runs": (
            "room.protocols.RoomCenterCompatibility",
            ["inquiry_active_runs"],
        ),
        "send_message": (
            "common.protocols.ExecutionEngine",
            ["execute(", "start_orchestration"],
        ),
        "suggest_agents": (
            "agent.protocols.AgentSuggestionService",
            ["suggest_agents"],
        ),
    }
    routes = json.loads(Path("tests/fixtures/phase9_api_routes.json").read_text())
    by_name = {
        route["name"]: route
        for route in routes
        if route["module"] == "api_gateway.routes.room_routes"
    }
    violations: list[str] = []

    for handler_name, (owner, method_names) in expectations.items():
        source = inspect.getsource(getattr(room_center, handler_name))
        if by_name[handler_name]["owning_protocol"] != owner:
            violations.append(
                f"{handler_name}: {by_name[handler_name]['owning_protocol']}"
            )
        for call_marker in method_names:
            if call_marker not in source:
                violations.append(f"{handler_name}: missing call {call_marker}")

    assert not violations, (
        "Room-center protocol inventory does not match handlers:\n"
        + "\n".join(violations)
    )


def test_room_active_runs_inventory_records_execution_support():
    from api_gateway.routes import room_routes as room_center

    routes = json.loads(Path("tests/fixtures/phase9_api_routes.json").read_text())
    room_setting_route = next(
        route
        for route in routes
        if route["module"] == "api_gateway.routes.room_routes"
        and route["name"] == "inquiry_room_setting"
    )
    active_runs_route = next(
        route
        for route in routes
        if route["module"] == "api_gateway.routes.room_routes"
        and route["name"] == "inquiry_active_runs"
    )
    route_source = inspect.getsource(room_center)
    room_runtime_source = Path("room/compat/runtime.py").read_text()

    assert "_require_execution_engine" not in route_source
    assert "runs = await engine.get_runs_for_room(room_id)" in route_source
    assert (
        "active_runs = await _active_run_refs_for_room(room_id, engine)" in route_source
    )
    assert "_read_active_runs_for_room" not in room_runtime_source
    assert "common.protocols.ExecutionEngine" in set(
        room_setting_route.get("supporting_protocols") or []
    )
    assert "common.protocols.ExecutionEngine" in set(
        active_runs_route.get("supporting_protocols") or []
    )


def test_legacy_410_routes_are_not_bound_to_legacy_execution_centers_at_startup():
    source = Path("main.py").read_text()
    forbidden = (
        "task.bind_task_dependencies(",
        "orchestration_center.bind_orchestration_dependencies(",
    )
    violations = [value for value in forbidden if value in source]

    assert not violations, (
        "Legacy 410 routes still bind execution centers:\n" + "\n".join(violations)
    )


def test_route_owner_protocols_match_handler_calls():
    from agent.protocols import (
        AgentCapabilityIssueStore,
        AgentCenterCompatibility,
        AgentGroupStoreCompatibility,
        AgentInspection,
        AgentLivenessChecker,
    )
    from common.protocols import (
        A2ATaskStatusReader,
        AgentRegistry,
        HealthCheck,
        RoomRouteReader,
        SSEStateReader,
        WebhookReceiver,
    )

    expected_by_protocol = {
        AgentCapabilityIssueStore: {
            "get_issue_by_id",
            "get_issues_for_agent",
            "resolve_all_for_agent",
            "resolve_issue",
        },
        AgentCenterCompatibility: {
            "delete_agent_from_route",
            "finalize_agent_response_for_route",
            "get_agent_card_from_url_for_route",
            "get_agents_by_provider_for_route",
            "get_visible_agent_for_route",
            "list_visible_agents_for_route",
            "register_agent_from_route",
        },
        AgentLivenessChecker: {
            "__call__",
        },
        AgentRegistry: {
            "get_agent",
        },
        AgentInspection: {
            "inspect_a2a_connection",
            "inspect_agent_card",
        },
        AgentGroupStoreCompatibility: {
            "add_agent_group",
            "delete_agent_group",
            "get_agent_group_by_id",
            "get_agent_groups_by_owner",
            "update_agent_group",
        },
        A2ATaskStatusReader: {
            "get_pending_task_messages_for_user",
            "get_room_agent_message_by_message_id",
            "get_task_messages_for_room",
        },
        RoomRouteReader: {"get_room_by_room_id"},
        SSEStateReader: {
            "get_room_by_room_id",
            "get_room_user_message_by_message_id",
        },
        WebhookReceiver: {"authenticate_webhook", "handle_webhook"},
        HealthCheck: {"check"},
    }

    missing: list[str] = []
    for protocol, expected_methods in expected_by_protocol.items():
        protocol_methods = {
            name
            for base in protocol.__mro__
            for name, value in base.__dict__.items()
            if callable(value)
            and (
                not name.startswith("_")
                or name in {"__call__", "_mask_sensitive_information"}
            )
        }
        absent = sorted(expected_methods - protocol_methods)
        if absent:
            missing.append(f"{protocol.__name__}: {absent}")

    assert not missing, "Route owner protocol methods missing:\n" + "\n".join(missing)


def test_agent_center_route_protocol_excludes_legacy_internal_methods():
    from agent.protocols import AgentCenterCompatibility

    forbidden = {
        "_mask_sensitive_information",
        "get_agent_card_from_url",
        "get_agents_by_provider_id",
        "get_agents_with_conditions",
        "get_all_active_agents",
        "get_all_agents",
        "query_agent_by_agent_id",
        "register_agent",
        "remove_agent",
        "update_agent",
    }

    assert forbidden.isdisjoint(AgentCenterCompatibility.__dict__)


def test_route_bound_compatibility_adapters_satisfy_protocols():
    from agent.protocols import AgentCenterCompatibility
    from agent.route_adapter import AgentRouteAdapter
    from room.protocols import RoomCenterCompatibility
    from room.route_adapter import RoomRouteAdapter

    assert isinstance(AgentRouteAdapter(service=object()), AgentCenterCompatibility)
    assert isinstance(RoomRouteAdapter(), RoomCenterCompatibility)


def test_agent_route_adapter_accepts_service():
    from agent.route_adapter import AgentRouteAdapter

    service = object()

    center = AgentRouteAdapter(service=service)

    assert center.agent_service is service


def test_agent_routes_expose_typed_dependency_providers():
    import inspect
    from typing import get_type_hints

    from agent.protocols import (
        AgentCapabilityIssueStore,
        AgentCenterCompatibility,
        AgentLivenessChecker,
    )
    from api_gateway.routes import agent_routes as agent
    from common.protocols import AgentRegistry

    provider_expectations = {
        agent.get_agent_center: AgentCenterCompatibility,
        agent.get_agent_service: AgentRegistry,
        agent.get_capability_issue_service: AgentCapabilityIssueStore,
        agent.get_agent_liveness_checker: AgentLivenessChecker,
    }
    for provider, expected_type in provider_expectations.items():
        assert get_type_hints(provider)["return"] is expected_type

    route_expectations = {
        agent.register_agent: {"center": AgentCenterCompatibility},
        agent.get_agent_by_provider: {"center": AgentCenterCompatibility},
        agent.delete_agent: {
            "center": AgentCenterCompatibility,
        },
        agent.get_capability_issues: {
            "agent_lookup": AgentRegistry,
            "issue_store": AgentCapabilityIssueStore,
        },
        agent.resolve_all_capability_issues: {
            "agent_lookup": AgentRegistry,
            "issue_store": AgentCapabilityIssueStore,
        },
        agent.resolve_capability_issue: {
            "agent_lookup": AgentRegistry,
            "issue_store": AgentCapabilityIssueStore,
        },
        agent.get_agent_card_from_url: {"center": AgentCenterCompatibility},
        agent.get_agent: {
            "center": AgentCenterCompatibility,
            "liveness_checker": AgentLivenessChecker,
        },
        agent.get_agent_list: {"center": AgentCenterCompatibility},
        agent.get_all_active_agents: {"center": AgentCenterCompatibility},
    }
    missing: list[str] = []
    for handler, expected_params in route_expectations.items():
        hints = get_type_hints(handler)
        signature = inspect.signature(handler)
        for param_name, expected_type in expected_params.items():
            if param_name not in signature.parameters:
                missing.append(f"{handler.__name__}.{param_name}")
            elif hints.get(param_name) is not expected_type:
                missing.append(
                    f"{handler.__name__}.{param_name}: {hints.get(param_name)}"
                )

    assert not missing, "Agent routes hide route owner dependencies:\n" + "\n".join(
        missing
    )


def test_agent_route_inventory_records_live_protocol_owners():
    routes = json.loads(Path("tests/fixtures/phase9_api_routes.json").read_text())
    by_name = {
        route["name"]: route
        for route in routes
        if route["module"] == "api_gateway.routes.agent_routes"
    }
    expectations = {
        "delete_agent": (
            "agent.protocols.AgentCenterCompatibility",
            set(),
        ),
        "get_agent_by_provider": ("agent.protocols.AgentCenterCompatibility", set()),
        "get_agent": (
            "agent.protocols.AgentCenterCompatibility",
            {"agent.protocols.AgentLivenessChecker"},
        ),
        "get_agent_card_from_url": ("agent.protocols.AgentCenterCompatibility", set()),
        "get_all_active_agents": ("agent.protocols.AgentCenterCompatibility", set()),
        "get_agent_list": ("agent.protocols.AgentCenterCompatibility", set()),
        "register_agent": ("agent.protocols.AgentCenterCompatibility", set()),
        "get_capability_issues": (
            "agent.protocols.AgentCapabilityIssueStore",
            {"common.protocols.AgentRegistry"},
        ),
        "resolve_all_capability_issues": (
            "agent.protocols.AgentCapabilityIssueStore",
            {"common.protocols.AgentRegistry"},
        ),
        "resolve_capability_issue": (
            "agent.protocols.AgentCapabilityIssueStore",
            {"common.protocols.AgentRegistry"},
        ),
    }
    violations: list[str] = []
    for name, (owner, supporting) in expectations.items():
        route = by_name[name]
        if route["owning_protocol"] != owner:
            violations.append(f"{name}: owner={route['owning_protocol']}")
        missing_supporting = supporting - set(route.get("supporting_protocols") or [])
        if missing_supporting:
            violations.append(f"{name}: missing {sorted(missing_supporting)}")

    assert not violations, (
        "Agent route inventory mismatches live protocols:\n" + "\n".join(violations)
    )


def test_sse_cancel_route_inventory_records_execution_owner():
    routes = json.loads(Path("tests/fixtures/phase9_api_routes.json").read_text())
    route = next(route for route in routes if route["name"] == "cancel_message")

    assert route["module"] == "api_gateway.routes.sse_routes"
    assert route["owning_protocol"] == "common.protocols.ExecutionEngine"
    assert set(route.get("supporting_protocols") or []) == {
        "common.protocols.SSEStateReader",
    }


def test_hitl_route_inventory_records_room_ownership_support():
    routes = json.loads(Path("tests/fixtures/phase9_api_routes.json").read_text())
    violations = [
        route["name"]
        for route in routes
        if route["module"] == "api_gateway.routes.hitl_routes"
        and "common.protocols.RoomOwnershipReader"
        not in set(route.get("supporting_protocols") or [])
    ]

    assert not violations, (
        "HITL routes omit RoomOwnershipReader support:\n" + "\n".join(violations)
    )


def test_file_upload_route_inventory_records_room_ownership_support():
    routes = json.loads(Path("tests/fixtures/phase9_api_routes.json").read_text())
    route = next(route for route in routes if route["name"] == "upload_file")

    assert route["module"] == "api_gateway.routes.files_routes"
    assert route["owning_protocol"] == "common.protocols.FileStorage"
    assert "common.protocols.RoomOwnershipReader" in set(
        route.get("supporting_protocols") or []
    )


def test_gateway_and_discovery_routes_record_rate_limit_support():
    routes = json.loads(Path("tests/fixtures/phase9_api_routes.json").read_text())
    violations = [
        f"{route['module']}.{route['name']}"
        for route in routes
        if route["module"]
        in {
            "api_gateway.routes.platform_gateway_routes",
            "api_gateway.routes.discovery_routes",
        }
        and "common.protocols.APIKeyRateLimiter"
        not in set(route.get("supporting_protocols") or [])
    ]

    assert not violations, (
        "Gateway/discovery routes omit rate limiter support:\n" + "\n".join(violations)
    )


def test_file_upload_route_uses_room_ownership_reader_protocol():
    import inspect
    from typing import get_type_hints

    from api_gateway import dependencies as gateway_deps
    from api_gateway.routes import files_routes as files
    from common.protocols import FileStorage, RoomOwnershipReader

    storage_provider_hints = get_type_hints(gateway_deps.get_file_storage)
    room_ownership_provider_hints = get_type_hints(
        gateway_deps.get_room_ownership_reader
    )
    route_hints = get_type_hints(files.upload_file)

    assert storage_provider_hints["return"] is FileStorage
    assert room_ownership_provider_hints["return"] is RoomOwnershipReader
    assert route_hints["storage"] is FileStorage
    assert route_hints["room_ownership"] is RoomOwnershipReader
    assert "room_ownership" in inspect.signature(files.upload_file).parameters


def test_route_inventory_protocols_do_not_expose_broad_or_wildcard_shapes():
    from collections.abc import Iterable, Mapping, Sequence
    from typing import Any, get_args, get_origin, get_type_hints

    from common.dto.base import FrozenDTO

    symbolic_owners = {"fastapi.documentation"}
    bare_container_types = {dict, list, set, tuple, Mapping, Sequence, Iterable}

    def annotation_is_broad(annotation, seen: set[object] | None = None) -> bool:
        if seen is None:
            seen = set()
        if annotation in seen:
            return False
        seen.add(annotation)
        if annotation in {Any, object, inspect.Signature.empty}:
            return True
        if annotation in {dict, list}:
            return True
        origin = get_origin(annotation)
        if origin is None:
            if inspect.isclass(annotation) and issubclass(annotation, FrozenDTO):
                return any(
                    annotation_is_broad(field.annotation, seen)
                    for field in annotation.model_fields.values()
                )
            if inspect.isclass(annotation) and is_dataclass(annotation):
                hints = get_type_hints(annotation)
                return any(
                    annotation_is_broad(hints.get(field.name, field.type), seen)
                    for field in fields(annotation)
                )
            return False
        if origin in bare_container_types and not get_args(annotation):
            return True
        return any(annotation_is_broad(arg, seen) for arg in get_args(annotation))

    route_symbols = set()
    for route in json.loads(Path("tests/fixtures/phase9_api_routes.json").read_text()):
        route_symbols.add(route["owning_protocol"])
        route_symbols.update(route.get("supporting_protocols") or [])

    violations: list[str] = []
    for owner in sorted(route_symbols - symbolic_owners):
        module_name, _, symbol_name = owner.rpartition(".")
        protocol = getattr(importlib.import_module(module_name), symbol_name)
        for name, member in protocol.__dict__.items():
            if name.startswith("_") or not callable(member):
                continue
            signature = inspect.signature(member)
            hints = get_type_hints(member)
            return_annotation = hints.get("return", signature.return_annotation)
            if annotation_is_broad(return_annotation):
                violations.append(f"{owner}.{name}.return")
            for parameter in signature.parameters.values():
                if parameter.name == "self":
                    continue
                if parameter.kind in {
                    inspect.Parameter.VAR_POSITIONAL,
                    inspect.Parameter.VAR_KEYWORD,
                }:
                    violations.append(f"{owner}.{name}.{parameter.name}")
                else:
                    annotation = hints.get(parameter.name, parameter.annotation)
                    if annotation_is_broad(annotation):
                        violations.append(f"{owner}.{name}.{parameter.name}")

    assert not violations, (
        "Route inventory protocols expose broad shapes:\n" + "\n".join(violations)
    )


def test_route_protocol_broad_shape_gate_catches_nested_any_and_bare_containers():
    from typing import Any, get_args, get_origin

    from common.dto.base import FrozenDTO
    from common.protocols import JsonValue

    class NestedBroadDTO(FrozenDTO):
        payload: dict[str, Any]

    def annotation_is_broad(annotation, seen: set[object] | None = None) -> bool:
        if seen is None:
            seen = set()
        if annotation in seen:
            return False
        seen.add(annotation)
        if annotation in {Any, object, inspect.Signature.empty}:
            return True
        if annotation in {dict, list}:
            return True
        origin = get_origin(annotation)
        if origin is None:
            if inspect.isclass(annotation) and issubclass(annotation, FrozenDTO):
                return any(
                    annotation_is_broad(field.annotation, seen)
                    for field in annotation.model_fields.values()
                )
            return False
        if origin in {dict, list} and not get_args(annotation):
            return True
        return any(annotation_is_broad(arg, seen) for arg in get_args(annotation))

    assert annotation_is_broad(dict)
    assert annotation_is_broad(list)
    assert annotation_is_broad(dict[str, Any])
    assert annotation_is_broad(list[dict[str, Any]])
    assert annotation_is_broad(NestedBroadDTO)
    assert not annotation_is_broad(dict[str, JsonValue])


def test_health_check_service_uses_request_state_not_main_closures():
    import main
    from common.health_check import RuntimeHealthCheck

    main_source = inspect.getsource(main)
    health_source = inspect.getsource(RuntimeHealthCheck)

    assert "_relay_streams_available" not in main_source
    assert "relay_streams_available=" not in main_source
    assert "request.app.state" in health_source
    assert "_relay_streams_available" not in health_source


@pytest.mark.asyncio
async def test_health_check_service_fails_closed_when_index_state_is_missing():
    from common.health_check import RuntimeHealthCheck

    compute_health_status = MagicMock(
        return_value={"body": {"status": "degraded"}, "status_code": 503}
    )
    service = RuntimeHealthCheck(
        redis_url=None,
        compute_health_status=compute_health_status,
    )
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))

    response = await service.check(request)

    assert response.status_code == 503
    kwargs = compute_health_status.call_args.kwargs
    assert kwargs["agent_search_index_ready"] is False
    assert kwargs["memory_search_index_ready"] is False
    assert kwargs["search_indexes_ready"] is False


def test_route_protocol_surfaces_are_specific():
    from agent.protocols import AgentGroupStoreCompatibility, AgentInspection
    from common.protocols import (
        A2ATaskStatusReader,
        RoomRouteReader,
        SSEStateReader,
        WebhookReceiver,
    )

    for protocol in (
        AgentInspection,
        AgentGroupStoreCompatibility,
        A2ATaskStatusReader,
        RoomRouteReader,
        SSEStateReader,
        WebhookReceiver,
    ):
        for name, value in protocol.__dict__.items():
            if not callable(value) or name.startswith("_"):
                continue
            params = inspect.signature(value).parameters
            assert not any(
                parameter.kind
                in {
                    inspect.Parameter.VAR_POSITIONAL,
                    inspect.Parameter.VAR_KEYWORD,
                }
                for parameter in params.values()
            ), f"{protocol.__name__}.{name} uses wildcard parameters"


def test_route_owner_protocols_do_not_expose_any_annotations():
    from typing import Any

    from agent.protocols import AgentGroupStoreCompatibility
    from common.protocols import (
        A2ATaskStatusReader,
        APIKeyStore,
        RoomRouteReader,
        SSEStateReader,
    )

    protocols = (
        A2ATaskStatusReader,
        APIKeyStore,
        RoomRouteReader,
        SSEStateReader,
        AgentGroupStoreCompatibility,
    )
    violations: list[str] = []

    for protocol in protocols:
        for name, value in protocol.__dict__.items():
            if not callable(value) or name.startswith("_"):
                continue
            signature = inspect.signature(value)
            if signature.return_annotation in {Any, object}:
                violations.append(f"{protocol.__name__}.{name} return")
            for parameter in signature.parameters.values():
                if parameter.annotation in {Any, object}:
                    violations.append(f"{protocol.__name__}.{name}.{parameter.name}")

    assert not violations, (
        "Route owner protocols expose broad annotations:\n" + "\n".join(violations)
    )


def test_route_protocols_do_not_expose_broad_annotations():
    import agent.protocols as agent_protocols
    import room.protocols as room_protocols
    from agent.protocols import AgentGroupStoreCompatibility
    from common.protocols import (
        A2ATaskStatusReader,
        HealthCheck,
        RoomRouteReader,
        SSEStateReader,
    )

    protocols = [
        getattr(module, name)
        for module in (agent_protocols, room_protocols)
        for name in module.__all__
        if isinstance(getattr(module, name, None), type)
    ]
    protocols.extend([A2ATaskStatusReader, RoomRouteReader, SSEStateReader])
    protocols.append(AgentGroupStoreCompatibility)
    protocols.append(HealthCheck)
    violations: list[str] = []

    for protocol in protocols:
        for name, value in protocol.__dict__.items():
            if not callable(value) or name.startswith("_"):
                continue
            signature = inspect.signature(value)
            if signature.return_annotation is inspect.Signature.empty:
                violations.append(f"{protocol.__name__}.{name} return")
            elif _annotation_contains_broad_object(signature.return_annotation):
                violations.append(f"{protocol.__name__}.{name} return")
            for parameter in signature.parameters.values():
                if parameter.name == "self":
                    continue
                if parameter.annotation is inspect.Signature.empty:
                    violations.append(f"{protocol.__name__}.{name}.{parameter.name}")
                elif _annotation_contains_broad_object(parameter.annotation):
                    violations.append(f"{protocol.__name__}.{name}.{parameter.name}")

    assert not violations, "Route protocols expose broad shapes:\n" + "\n".join(
        violations
    )


def test_platform_route_protocols_do_not_expose_any_or_wildcard_params():
    from typing import Any

    from common.protocols import (
        APIKeyRateLimiter,
        FileStorage,
        GatewayDiscoveryProvider,
        GatewayService,
        RateLimiter,
    )

    protocols = (
        APIKeyRateLimiter,
        FileStorage,
        GatewayDiscoveryProvider,
        GatewayService,
        RateLimiter,
    )
    violations: list[str] = []

    for protocol in protocols:
        for name, value in protocol.__dict__.items():
            if not callable(value) or name.startswith("_"):
                continue
            signature = inspect.signature(value)
            if signature.return_annotation in {Any, inspect.Signature.empty}:
                violations.append(f"{protocol.__name__}.{name} return")
            if protocol in {GatewayDiscoveryProvider, GatewayService}:
                if _annotation_contains_broad_object(signature.return_annotation):
                    violations.append(f"{protocol.__name__}.{name} return")
            for parameter in signature.parameters.values():
                if parameter.kind in {
                    inspect.Parameter.VAR_POSITIONAL,
                    inspect.Parameter.VAR_KEYWORD,
                }:
                    violations.append(f"{protocol.__name__}.{name}.{parameter.name}")
                if parameter.annotation is Any:
                    violations.append(f"{protocol.__name__}.{name}.{parameter.name}")
                if protocol in {GatewayDiscoveryProvider, GatewayService}:
                    if _annotation_contains_broad_object(parameter.annotation):
                        violations.append(
                            f"{protocol.__name__}.{name}.{parameter.name}"
                        )

    assert not violations, (
        "Platform route protocols expose broad shapes:\n" + "\n".join(violations)
    )


def test_route_protocols_have_single_runtime_marker():
    for path in (
        Path("agent/protocols.py"),
        Path("room/protocols.py"),
    ):
        source = path.read_text()
        assert "@runtime_checkable\n@runtime_checkable" not in source


def test_inspection_protocol_uses_route_contract_types():
    from typing import get_type_hints

    from agent.protocols import AgentInspection
    from models.request import InspectionCenterRequest
    from models.response import InspectionCenterResponse

    inspect_card = get_type_hints(AgentInspection.inspect_agent_card)
    inspect_connection = get_type_hints(AgentInspection.inspect_a2a_connection)

    assert inspect_card["request"] is InspectionCenterRequest
    assert inspect_card["return"] is InspectionCenterResponse
    assert inspect_connection["request"] is InspectionCenterRequest
    assert inspect_connection["return"] is InspectionCenterResponse

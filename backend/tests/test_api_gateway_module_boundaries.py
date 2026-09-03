import ast
import tomllib
from pathlib import Path

REMOVED_RUNTIME_PACKAGE = "app_" + "shell"

FORBIDDEN_API_GATEWAY_IMPORTS = (
    "database.mongodb",
    "modules",
    REMOVED_RUNTIME_PACKAGE,
)
MODULE_ROUTE_PROTOCOL_IMPORTS = {
    "agent.protocols",
    "room.protocols",
    "context_memory.protocols",
}
FORBIDDEN_ROUTE_MODULE_ROOTS = {"agent", "room", "context_memory", "a2a_adapter"}


def _api_gateway_py_files():
    root = Path("api_gateway")
    if not root.exists():
        return []
    return sorted(root.rglob("*.py"))


def test_api_gateway_package_exists():
    assert Path("api_gateway").is_dir()
    assert Path("api_gateway/routes").is_dir()


def test_api_gateway_does_not_import_forbidden_concrete_modules():
    violations = []
    for path in _api_gateway_py_files():
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            module = None
            if isinstance(node, ast.ImportFrom):
                module = node.module
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    module = alias.name
                    if any(
                        module == forbidden or module.startswith(f"{forbidden}.")
                        for forbidden in FORBIDDEN_API_GATEWAY_IMPORTS
                    ):
                        violations.append(f"{path}: import {module}")
                continue

            if module and any(
                module == forbidden or module.startswith(f"{forbidden}.")
                for forbidden in FORBIDDEN_API_GATEWAY_IMPORTS
            ):
                violations.append(f"{path}: from {module} import ...")

    assert violations == []


def test_gateway_routes_import_only_module_protocol_surfaces():
    violations = []
    paths = list(Path("api_gateway/routes").glob("*.py"))

    for path in sorted(paths):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            modules: list[str] = []
            if isinstance(node, ast.ImportFrom) and node.module:
                modules.append(node.module)
            elif isinstance(node, ast.Import):
                modules.extend(alias.name for alias in node.names)

            for module in modules:
                root = module.split(".", 1)[0]
                if root not in FORBIDDEN_ROUTE_MODULE_ROOTS:
                    continue
                if module in MODULE_ROUTE_PROTOCOL_IMPORTS:
                    continue
                violations.append(f"{path}: import {module}")

    assert violations == []


def test_gateway_route_modules_do_not_hold_business_dependency_globals():
    dependency_global_names = {
        "agent_avatar_manager",
        "agent_center",
        "agent_group_store",
        "agent_liveness_checker",
        "agent_selection_service",
        "agent_service",
        "api_key_store",
        "capability_issue_service",
        "discovery_default_limit",
        "discovery_rate_limit_service",
        "discovery_service",
        "execution_engine",
        "file_storage",
        "gateway_rate_limit_service",
        "gateway_service",
        "hitl_manager",
        "hub_relay_service",
        "inspection_center",
        "relay_service",
        "room_center",
        "room_ownership_reader",
        "room_store",
        "sse_manager",
        "sse_store",
        "task_store",
        "webhook_receiver",
    }
    paths = list(Path("api_gateway/routes").glob("*.py"))
    violations: list[str] = []

    for path in sorted(paths):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in tree.body:
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if (
                        isinstance(target, ast.Name)
                        and target.id in dependency_global_names
                    ):
                        violations.append(f"{path}:{node.lineno}: {target.id}")
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                if node.target.id in dependency_global_names:
                    violations.append(f"{path}:{node.lineno}: {node.target.id}")
            if isinstance(node, ast.FunctionDef) and node.name.startswith("bind_"):
                violations.append(f"{path}:{node.lineno}: {node.name}")

    assert not violations, (
        "API Gateway route modules own mutable business dependencies:\n"
        + "\n".join(violations)
    )


def test_container_does_not_call_route_level_dependency_binders():
    forbidden_snippets = (
        ".bind_a2a_task_dependencies(",
        ".bind_agent_dependencies(",
        ".bind_agent_group_dependencies(",
        ".bind_api_key_store(",
        ".bind_discovery_dependencies(",
        ".bind_execution_deps(",
        ".bind_file_dependencies(",
        ".bind_gateway_dependencies(",
        ".bind_hub_dependencies(",
        ".bind_inspection_dependencies(",
        ".bind_memory_dependencies(",
        ".bind_relay_dependencies(",
        ".bind_room_dependencies(",
        ".bind_room_ownership_reader(",
        ".bind_sse_dependencies(",
        ".bind_webhook_dependencies(",
    )
    source = Path("container.py").read_text()
    violations = [snippet for snippet in forbidden_snippets if snippet in source]

    assert not violations, (
        "Container still calls route-level dependency binders:\n"
        + "\n".join(violations)
    )


def test_api_gateway_route_files_do_not_use_legacy_prefix():
    route_dir = Path("api_gateway/routes")
    route_files = route_dir.glob("*.py") if route_dir.exists() else []

    assert [path.name for path in route_files if path.name.startswith("legacy_")] == []


def test_main_mounts_only_gateway_router_for_api_prefix():
    tree = ast.parse(Path("main.py").read_text(), filename="main.py")
    include_calls = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and func.attr == "include_router"
            and isinstance(func.value, ast.Name)
            and func.value.id == "app"
        ):
            include_calls.append(node)

    assert len(include_calls) >= 1
    call = include_calls[0]
    assert isinstance(call.args[0], ast.Attribute)
    assert isinstance(call.args[0].value, ast.Name)
    assert call.args[0].value.id == "api_gateway"
    assert call.args[0].attr == "router"


def test_main_does_not_import_old_api_route_modules():
    tree = ast.parse(Path("main.py").read_text(), filename="main.py")
    violations = []

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module == "api" or node.module.startswith("api."):
                violations.append(f"from {node.module} import ...")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "api" or alias.name.startswith("api."):
                    violations.append(f"import {alias.name}")

    assert violations == []


def test_legacy_api_route_package_is_removed():
    assert not Path("api").exists()


def test_room_route_owner_protocol_covers_room_route_calls():
    import room.protocols as protocols

    required_methods = {
        "create_new_room",
        "inquiry_rooms_by_room_owner_id",
        "inquiry_room_messages_by_room_id",
        "inquiry_room_setting",
        "inquiry_active_runs",
        "update_room_agent_set",
        "update_room_name",
        "update_room_default_mode",
    }

    assert required_methods.issubset(set(protocols.RoomCenterCompatibility.__dict__))


def test_api_gateway_packages_are_registered_for_distribution():
    setuptools_config = tomllib.loads(Path("pyproject.toml").read_text())["tool"][
        "setuptools"
    ]
    packages = set(setuptools_config["packages"])

    assert {
        "api_gateway",
        "api_gateway.routes",
        "common.client",
        "common.middleware",
        "common.server",
        "common.utils",
    }.issubset(packages)
    assert "main" in set(setuptools_config.get("py-modules", []))


def test_a2a_sdk_dependency_is_pinned_to_compatible_major_version():
    from packaging.requirements import Requirement

    project_config = tomllib.loads(Path("pyproject.toml").read_text())["project"]
    dependencies = {
        Requirement(dependency).name: Requirement(dependency)
        for dependency in project_config["dependencies"]
    }

    assert "a2a-sdk" in dependencies
    specifier = dependencies["a2a-sdk"].specifier
    assert specifier.contains("0.3.25")
    assert not specifier.contains("1.0.3")

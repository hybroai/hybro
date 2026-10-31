import ast
import sys
from pathlib import Path

REMOVED_RUNTIME_PACKAGE = "app_" + "shell"

PRODUCTION_LLM_CONSUMER_ROOTS = [
    Path("container.py"),
    Path("main.py"),
    Path("__main__.py"),
    Path("api_gateway"),
    Path("agent"),
    Path("common"),
    Path("context_memory"),
    Path("delivery"),
    Path("execution"),
    Path("hub_runtime_bridge"),
    Path("jobs"),
    Path("llm_gateway"),
    Path("platform_module"),
    Path("chat"),
    Path("conversation"),
]

PROVIDER_NAMED_REMOVED_RUNTIME_MODULES = {
    f"{REMOVED_RUNTIME_PACKAGE}.openai_service",
    f"{REMOVED_RUNTIME_PACKAGE}.gemini_service",
}

PROVIDER_NAMED_REMOVED_RUNTIME_LEAF_MODULES = {
    module.rsplit(".", 1)[-1] for module in PROVIDER_NAMED_REMOVED_RUNTIME_MODULES
}

PROVIDER_NAMED_RUNTIME_SYMBOLS = {
    "OpenAIService",
    "GeminiService",
}

LLM_SETTINGS_FIELDS = {
    "lead_ai_model",
    "classifier_ai_model",
    "embedding_model",
    "gemini_model_name",
    "gemini_embedding_model_name",
    "supervisor_model",
    "deepseek_api_key",
    "deepseek_model_name",
    "openai_api_key",
    "google_api_key",
}


def test_llm_gateway_services_import_boundary():
    services_path = Path("llm_gateway/services")
    assert services_path.exists(), "llm_gateway/services package must exist"
    forbidden_domain_modules = {
        "execution.orchestration.synthesis_coordinator",
        "conversation.runtime",
    }
    allowed_roots = set(sys.stdlib_module_names) | {
        "__future__",
        "common",
        "llm_gateway",
    }
    forbidden_roots = {
        "models",
        REMOVED_RUNTIME_PACKAGE,
        "execution",
        "chat",
        "conversation",
        "agent",
        "platform_module",
        "api",
        "api_gateway",
    }
    forbidden_provider_names = {
        "OpenAIProvider",
        "GeminiProvider",
        "DeepSeekProvider",
    }
    for path in services_path.rglob("*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            imported_modules = _imported_modules(node)
            assert imported_modules.isdisjoint(forbidden_domain_modules), (
                f"{path} imports forbidden domain module "
                f"{imported_modules & forbidden_domain_modules}"
            )
            imported_roots = _imported_roots(node)
            assert imported_roots.isdisjoint(forbidden_roots), (
                f"{path} imports forbidden domain root "
                f"{imported_roots & forbidden_roots}"
            )
            assert "llm_gateway.providers" not in imported_modules, (
                f"{path} must depend on gateway protocols, not raw providers"
            )
            unexpected = imported_roots - allowed_roots
            assert not unexpected, f"{path} imports unexpected root {unexpected}"
            if isinstance(node, ast.Name):
                assert node.id not in forbidden_provider_names, (
                    f"{path} references raw provider type {node.id}"
                )


def test_provider_hint_is_not_public_protocol_or_service_api():
    paths = [Path("common/protocols/llm_protocols.py")]
    paths.extend(Path("llm_gateway/services").rglob("*.py"))
    for path in paths:
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                arg_names = [arg.arg for arg in node.args.args + node.args.kwonlyargs]
                assert "provider_hint" not in arg_names, (
                    f"{path}:{node.name} exposes provider_hint publicly"
                )


def test_provider_named_llm_runtime_symbols_are_gone_from_runtime_modules():
    violations: list[str] = []
    for path in _python_files(PRODUCTION_LLM_CONSUMER_ROOTS):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            symbol = _provider_named_runtime_symbol(node)
            if symbol:
                violations.append(f"{path}:{node.lineno}: {symbol}")

    assert not violations, (
        "Runtime modules still reference provider-named LLM service symbols: "
        f"{violations}"
    )


def test_container_binds_focused_llm_services_to_production_consumers():
    source = Path("container.py").read_text()
    main_source = Path("main.py").read_text()
    expected_snippets = [
        "provider_runtime=_provider_runtime,",
        "settings_obj=runtime.settings,",
        "agent_selection_llm_service = AgentSelectionLLMService(",
    ]
    forbidden_snippets = [
        "discovery_llm_service = DiscoveryLLMService(",
        f"from {REMOVED_RUNTIME_PACKAGE}.openai_service import",
        f"from {REMOVED_RUNTIME_PACKAGE}.gemini_service import",
        "openai_service.bind_llm_gateway(",
        "gemini_service.bind_llm_gateway(",
        "openai_service.bind_debate_service(",
    ]
    missing = [snippet for snippet in expected_snippets if snippet not in source]
    leaked_to_main = [
        snippet for snippet in expected_snippets if snippet in main_source
    ]
    leaked_legacy = [snippet for snippet in forbidden_snippets if snippet in source]
    leaked_legacy.extend(_provider_named_removed_runtime_imports(Path("container.py")))
    leaked_legacy.extend(_provider_named_removed_runtime_calls(Path("container.py")))
    assert missing == [], f"container.py missing focused LLM bindings: {missing}"
    assert leaked_to_main == [], f"main.py owns focused LLM bindings: {leaked_to_main}"
    assert leaked_legacy == [], (
        "container.py still contains legacy provider-named "
        "runtime package"
        " wiring: "
        f"{leaked_legacy}"
    )


def test_llm_settings_are_not_read_by_feature_runtime_modules():
    allowed_prefixes = (
        "common/config/",
        "llm_gateway/config.py",
        "llm_gateway/model_registry.py",
        "llm_gateway/providers/",
    )
    scan_roots = [
        Path("execution"),
        Path("api_gateway"),
        Path("agent"),
        Path("chat"),
        Path("conversation"),
        Path("context_memory"),
        Path("llm_gateway/services"),
    ]
    violations: list[str] = []
    for root in scan_roots:
        if not root.exists():
            continue
        for path in root.rglob("*.py"):
            normalized = path.as_posix()
            if normalized.startswith(allowed_prefixes):
                continue
            tree = ast.parse(path.read_text(), filename=str(path))
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Attribute)
                    and isinstance(node.value, ast.Name)
                    and node.value.id.endswith("settings")
                    and node.attr in LLM_SETTINGS_FIELDS
                ):
                    violations.append(f"{path}:{node.attr}")
                if _is_settings_getattr(node):
                    violations.append(f"{path}:{node.args[1].value}")
    assert not violations


def _imported_modules(node: ast.AST) -> set[str]:
    if isinstance(node, ast.Import):
        return {alias.name for alias in node.names}
    if isinstance(node, ast.ImportFrom) and node.module:
        modules = {node.module}
        if node.module == REMOVED_RUNTIME_PACKAGE:
            modules.update(
                f"{REMOVED_RUNTIME_PACKAGE}.{alias.name}"
                for alias in node.names
                if alias.name in PROVIDER_NAMED_REMOVED_RUNTIME_LEAF_MODULES
            )
        return modules
    return set()


def _imported_modules_for_path(path: Path, node: ast.AST) -> set[str]:
    modules = set(_imported_modules(node))
    modules.update(_relative_provider_named_removed_runtime_modules(path, node))
    return modules


def _imported_roots(node: ast.AST) -> set[str]:
    return {module.split(".")[0] for module in _imported_modules(node)}


def _python_files(paths: list[Path]) -> list[Path]:
    files: list[Path] = []
    for path in paths:
        if path.is_file() and path.suffix == ".py":
            files.append(path)
        elif path.is_dir():
            files.extend(
                child
                for child in path.rglob("*.py")
                if "__pycache__" not in child.parts
                and child.parts[:2] != ("llm_gateway", "providers")
            )
    return sorted(files)


def _provider_named_removed_runtime_imports(path: Path) -> list[str]:
    violations: list[str] = []
    tree = ast.parse(path.read_text(), filename=str(path))
    for node in ast.walk(tree):
        leaked = (
            _imported_modules_for_path(path, node)
            & PROVIDER_NAMED_REMOVED_RUNTIME_MODULES
        )
        if leaked:
            violations.append(f"{path}:{node.lineno}: imports {sorted(leaked)}")
    return violations


def _provider_named_removed_runtime_calls(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    legacy_binding_names = _provider_named_removed_runtime_binding_names(tree)
    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            call_name = _call_name(node.func)
            if call_name in {
                f"{name}.bind_llm_gateway" for name in legacy_binding_names
            } | {f"{name}.bind_llm_services" for name in legacy_binding_names} | {
                f"{name}.bind_debate_service" for name in legacy_binding_names
            }:
                violations.append(f"{path}:{node.lineno}: calls {call_name}()")
    return violations


def _provider_named_removed_runtime_binding_names(tree: ast.AST) -> set[str]:
    binding_names: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.ImportFrom)
            and node.module in PROVIDER_NAMED_REMOVED_RUNTIME_MODULES
        ):
            binding_names.update(alias.asname or alias.name for alias in node.names)
        elif (
            isinstance(node, ast.ImportFrom) and node.module == REMOVED_RUNTIME_PACKAGE
        ):
            binding_names.update(
                alias.asname or alias.name
                for alias in node.names
                if alias.name in PROVIDER_NAMED_REMOVED_RUNTIME_LEAF_MODULES
            )
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in PROVIDER_NAMED_REMOVED_RUNTIME_MODULES:
                    binding_names.add(alias.asname or alias.name.split(".")[-1])
    return binding_names


def _relative_provider_named_removed_runtime_modules(
    path: Path, node: ast.AST
) -> set[str]:
    if (
        not isinstance(node, ast.ImportFrom)
        or node.level == 0
        or path.parts[:1] != (REMOVED_RUNTIME_PACKAGE,)
    ):
        return set()

    modules: set[str] = set()
    if node.module in PROVIDER_NAMED_REMOVED_RUNTIME_LEAF_MODULES:
        modules.add(f"{REMOVED_RUNTIME_PACKAGE}.{node.module}")

    modules.update(
        f"{REMOVED_RUNTIME_PACKAGE}.{alias.name}"
        for alias in node.names
        if alias.name in PROVIDER_NAMED_REMOVED_RUNTIME_LEAF_MODULES
    )
    return modules


def _provider_named_runtime_symbol(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name) and node.id in PROVIDER_NAMED_RUNTIME_SYMBOLS:
        return node.id
    if isinstance(node, ast.Attribute) and node.attr in PROVIDER_NAMED_RUNTIME_SYMBOLS:
        return node.attr
    if (
        isinstance(
            node,
            ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef,
        )
        and node.name in PROVIDER_NAMED_RUNTIME_SYMBOLS
    ):
        return node.name
    return None


def _call_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _call_name(node.value)
        if parent:
            return f"{parent}.{node.attr}"
    return None


def _is_settings_getattr(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "getattr"
        and len(node.args) >= 2
        and isinstance(node.args[0], ast.Name)
        and node.args[0].id.endswith("settings")
        and isinstance(node.args[1], ast.Constant)
        and node.args[1].value in LLM_SETTINGS_FIELDS
    )

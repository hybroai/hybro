from __future__ import annotations

import ast
import json
import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REMOVED_RUNTIME_PACKAGE = "app_" + "shell"
MANIFEST = ROOT / "tests/fixtures/dal_database_convergence_manifest.json"
DEFAULT_EXCLUDE_DIRS = {
    "tests",
    "docs",
    "scripts",
    "__pycache__",
    ".git",
    ".venv",
    ".pytest_cache",
    ".ruff_cache",
    "logs",
    "hybro_backend.egg-info",
    "multi_agents_backend.egg-info",
}
LEGACY_RUNTIME_FILES = (
    f"{REMOVED_RUNTIME_PACKAGE}/database_service.py",
    "database/mongodb.py",
    "database/pinecone_db.py",
    "database/repository.py",
)
OBJECT_STORAGE_SHIM_IMPORT_PREFIX = f"{REMOVED_RUNTIME_PACKAGE}.s3_service"
OBJECT_STORAGE_SHIM_EXEMPT_FILES = {f"{REMOVED_RUNTIME_PACKAGE}/s3_service.py"}
AWS_SDK_IMPORT_PREFIXES = {"aioboto3", "botocore"}
AWS_SDK_ALLOWED_PREFIXES = ()
AWS_SDK_TEMPORARY_ALLOWLIST = {}


@dataclass(frozen=True)
class Blocker:
    path: str
    line: int
    symbol: str

    def as_manifest_entry(self) -> str:
        return f"{self.path}:{self.line}:{self.symbol}"


def _rel(path: Path) -> str:
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return path.name


def _tree(path: Path) -> ast.AST:
    return ast.parse(path.read_text(), filename=str(path))


def _is_module_match(module: str, prefix: str) -> bool:
    return module == prefix or module.startswith(prefix + ".")


def _manifest() -> dict:
    return json.loads(MANIFEST.read_text())


def _py_files() -> list[Path]:
    manifest = _manifest()
    exclude_dirs = set(DEFAULT_EXCLUDE_DIRS) | set(
        manifest.get("exclude_dirs", DEFAULT_EXCLUDE_DIRS)
    )
    exclude_files = set(manifest.get("exclude_files", []))
    filtered: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        current = Path(dirpath)
        rel_dir = current.relative_to(ROOT).as_posix()
        if rel_dir == ".":
            rel_dir = ""
        dirnames[:] = [
            dirname
            for dirname in dirnames
            if not _is_excluded_path(
                f"{rel_dir}/{dirname}".lstrip("/"),
                exclude_dirs,
            )
        ]
        for filename in filenames:
            if not filename.endswith(".py"):
                continue
            path = current / filename
            rel = path.relative_to(ROOT).as_posix()
            if rel in exclude_files or _is_excluded_path(rel, exclude_dirs):
                continue
            filtered.append(path)
    return filtered


def _is_excluded_path(rel: str, exclude_dirs: set[str]) -> bool:
    parts = rel.split("/")
    return any(
        rel == excluded
        or rel.startswith(f"{excluded.rstrip('/')}/")
        or excluded in parts
        for excluded in exclude_dirs
    )


def _iter_imports(path: Path) -> list[Blocker]:
    rel = _rel(path)
    imports: list[Blocker] = []
    for node in ast.walk(_tree(path)):
        if isinstance(node, ast.Import):
            imports.extend(
                Blocker(rel, node.lineno, alias.name) for alias in node.names
            )
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.append(Blocker(rel, node.lineno, node.module))
            imports.extend(
                Blocker(rel, node.lineno, f"{node.module}.{alias.name}")
                for alias in node.names
                if alias.name != "*"
            )
    return imports


def _iter_dynamic_imports(path: Path) -> list[Blocker]:
    rel = _rel(path)
    imports: list[Blocker] = []
    for node in ast.walk(_tree(path)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        is_import_module = (
            isinstance(func, ast.Attribute)
            and func.attr == "import_module"
            and isinstance(func.value, ast.Name)
            and func.value.id == "importlib"
        ) or (isinstance(func, ast.Name) and func.id == "import_module")
        is_dunder_import = isinstance(func, ast.Name) and func.id == "__import__"
        if (
            (is_import_module or is_dunder_import)
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            imports.append(Blocker(rel, node.lineno, node.args[0].value))
    return imports


def _violating_files(prefix: str) -> list[str]:
    found: list[str] = []
    for path in _py_files():
        for blocker in [*_iter_imports(path), *_iter_dynamic_imports(path)]:
            if _is_module_match(blocker.symbol, prefix):
                found.append(blocker.as_manifest_entry())
    return sorted(found)


def _hidden_mongo_fallback_blockers(path: Path) -> list[Blocker]:
    rel = _rel(path)
    blockers: list[Blocker] = []
    tree = _tree(path)
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "mongodb"
            for target in node.targets
        ):
            blockers.append(Blocker(rel, node.lineno, "mongodb"))
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "mongodb"
        ):
            blockers.append(Blocker(rel, node.lineno, "mongodb"))
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in {"getattr", "hasattr"}
            and node.args
            and isinstance(node.args[0], ast.Name)
            and node.args[0].id == "mongodb"
        ):
            blockers.append(Blocker(rel, node.lineno, node.func.id))
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and isinstance(node.func.value, ast.Call)
            and isinstance(node.func.value.func, ast.Name)
            and node.func.value.func.id == "globals"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value == "mongodb"
        ):
            blockers.append(Blocker(rel, node.lineno, 'globals().get("mongodb")'))
        if (
            isinstance(node, ast.FunctionDef)
            and node.name == "__getattr__"
            and _function_exposes_mongodb(node)
        ):
            blockers.append(Blocker(rel, node.lineno, "__getattr__:mongodb"))
    return blockers


def _function_exposes_mongodb(node: ast.FunctionDef) -> bool:
    for child in ast.walk(node):
        if isinstance(child, ast.Constant) and child.value == "mongodb":
            return True
    return False


def _name_blockers(path: Path, names: set[str]) -> list[Blocker]:
    rel = _rel(path)
    blockers: list[Blocker] = []
    for node in ast.walk(_tree(path)):
        if isinstance(node, ast.Name) and node.id in names:
            blockers.append(Blocker(rel, node.lineno, node.id))
        elif isinstance(node, ast.Attribute) and node.attr in names:
            blockers.append(Blocker(rel, node.lineno, node.attr))
        elif isinstance(node, ast.arg) and node.arg in names:
            blockers.append(Blocker(rel, node.lineno, node.arg))
    return blockers


def _string_literal_blockers(path: Path, names: set[str]) -> list[Blocker]:
    rel = _rel(path)
    blockers: list[Blocker] = []
    for node in ast.walk(_tree(path)):
        if isinstance(node, ast.Constant) and node.value in names:
            blockers.append(Blocker(rel, node.lineno, repr(node.value)))
    return blockers


def _aliased_import_name_blockers(
    path: Path,
    *,
    module_prefixes: set[str],
    names: set[str],
) -> list[Blocker]:
    rel = _rel(path)
    tree = _tree(path)
    blockers: list[Blocker] = []
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or node.module is None:
            continue
        if not any(_is_module_match(node.module, prefix) for prefix in module_prefixes):
            continue
        for alias in node.names:
            if alias.name not in names or alias.asname is None:
                continue
            aliases[alias.asname] = alias.name
            blockers.append(
                Blocker(rel, node.lineno, f"{alias.name} as {alias.asname}")
            )

    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in aliases:
            blockers.append(Blocker(rel, node.lineno, f"{aliases[node.id]}:{node.id}"))
    return blockers


def _database_service_blockers(path: Path) -> list[Blocker]:
    blockers: list[Blocker] = []
    for blocker in [*_iter_imports(path), *_iter_dynamic_imports(path)]:
        if _is_module_match(
            blocker.symbol, f"{REMOVED_RUNTIME_PACKAGE}.database_service"
        ):
            blockers.append(blocker)
    blockers.extend(_name_blockers(path, {"database_service", "db_service", "_db_svc"}))
    blockers.extend(_string_literal_blockers(path, {"database_service", "db_service"}))
    return _unique_blockers(blockers)


def _mongo_singleton_blockers(path: Path) -> list[Blocker]:
    blockers: list[Blocker] = []
    for blocker in [*_iter_imports(path), *_iter_dynamic_imports(path)]:
        if _is_module_match(blocker.symbol, "database.mongodb"):
            blockers.append(blocker)
    blockers.extend(
        _name_blockers(
            path,
            {
                "_legacy_mongo",
                "_mongodb_backend",
                "_bind_mongodb_backend",
                "bind_mongo_backend",
            },
        )
    )
    blockers.extend(
        _aliased_import_name_blockers(
            path,
            module_prefixes={"context_memory.search_adapter"},
            names={"bind_mongo_backend"},
        )
    )
    blockers.extend(_hidden_mongo_fallback_blockers(path))
    return _unique_blockers(blockers)


def _database_repository_blockers(path: Path) -> list[Blocker]:
    blockers: list[Blocker] = []
    for blocker in [*_iter_imports(path), *_iter_dynamic_imports(path)]:
        if _is_module_match(blocker.symbol, "database.repository"):
            blockers.append(blocker)
    rel = _rel(path)
    for node in ast.walk(_tree(path)):
        if isinstance(node, ast.keyword):
            if node.arg == "create_repository" and _is_name(node.value, "Repository"):
                blockers.append(
                    Blocker(rel, node.value.lineno, "create_repository=Repository")
                )
            if node.arg == "db_provider" and _is_name(node.value, "get_db"):
                blockers.append(Blocker(rel, node.value.lineno, "db_provider=get_db"))
    return _unique_blockers(blockers)


def _pinecone_singleton_blockers(path: Path) -> list[Blocker]:
    blockers: list[Blocker] = []
    for blocker in [*_iter_imports(path), *_iter_dynamic_imports(path)]:
        if _is_module_match(blocker.symbol, "database.pinecone_db"):
            blockers.append(blocker)
    blockers.extend(
        _name_blockers(
            path,
            {
                "pinecone_db",
                "_pinecone_backend",
                "bind_pinecone_backend",
            },
        )
    )
    blockers.extend(
        _aliased_import_name_blockers(
            path,
            module_prefixes={"context_memory.search_adapter"},
            names={"bind_pinecone_backend"},
        )
    )
    rel = _rel(path)
    for node in ast.walk(_tree(path)):
        if (
            isinstance(node, ast.Attribute)
            and node.attr == "pinecone"
            and isinstance(node.value, ast.Name)
            and node.value.id == "self"
        ):
            blockers.append(Blocker(rel, node.lineno, "self.pinecone"))
    return _unique_blockers(blockers)


def _direct_pinecone_blockers(path: Path) -> list[Blocker]:  # noqa: C901
    rel = _rel(path)
    if rel.startswith("dal/pinecone/"):
        return []
    tree = _tree(path)
    blockers: list[Blocker] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "pinecone" or alias.name.startswith("pinecone."):
                    blockers.append(Blocker(rel, node.lineno, alias.name))
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.module == "pinecone" or node.module.startswith("pinecone."):
                blockers.append(Blocker(rel, node.lineno, node.module))

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if _is_pinecone_constructor(node):
            blockers.append(Blocker(rel, node.lineno, "pinecone.Pinecone"))
        if isinstance(node.func, ast.Attribute) and node.func.attr == "Index":
            blockers.append(Blocker(rel, node.lineno, _callable_name(node.func)))
    return _unique_blockers(blockers)


def _callable_name(node: ast.AST) -> str:
    try:
        return ast.unparse(node)
    except Exception:
        return "<expr>.Index"


def _is_name(node: ast.AST, name: str) -> bool:
    return isinstance(node, ast.Name) and node.id == name


def _is_pinecone_constructor(node: ast.AST | None) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "Pinecone"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "pinecone"
    )


def _unique_blockers(blockers: list[Blocker]) -> list[Blocker]:
    return sorted(set(blockers), key=lambda item: item.as_manifest_entry())


def _import_root(symbol: str) -> str:
    return symbol.split(".", 1)[0]


def _is_aws_sdk_import_allowed(blocker: Blocker) -> bool:
    root = _import_root(blocker.symbol)
    allowlisted = (blocker.path, root) in AWS_SDK_TEMPORARY_ALLOWLIST
    return blocker.path.startswith(AWS_SDK_ALLOWED_PREFIXES) or allowlisted


def _aws_sdk_import_blockers() -> list[str]:
    blockers: list[str] = []
    for path in _py_files():
        for blocker in [*_iter_imports(path), *_iter_dynamic_imports(path)]:
            root = _import_root(blocker.symbol)
            if root in AWS_SDK_IMPORT_PREFIXES and not _is_aws_sdk_import_allowed(
                blocker
            ):
                blockers.append(blocker.as_manifest_entry())
    return sorted(blockers)


def _entries(blockers: list[Blocker]) -> list[str]:
    return sorted(blocker.as_manifest_entry() for blocker in blockers)


def _all_entries(scanner) -> list[str]:
    found: list[str] = []
    for path in _py_files():
        found.extend(_entries(scanner(path)))
    return sorted(found)


def _legacy_runtime_file_entries() -> list[str]:
    return sorted(path for path in LEGACY_RUNTIME_FILES if (ROOT / path).exists())


def _has_database_service_duck_usage(path: Path) -> bool:
    source = path.read_text()
    if (
        "database_service" not in source
        and "db_service" not in source
        and "DatabaseService" not in source
    ):
        return False
    tree = ast.parse(source, filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            for arg in [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]:
                if arg.arg in {"database_service", "db_service"}:
                    return True
        if isinstance(node, ast.Assign):
            target_names = {
                target.attr if isinstance(target, ast.Attribute) else target.id
                for target in node.targets
                if isinstance(target, ast.Attribute | ast.Name)
            }
            if target_names.intersection({"_db", "_database_service", "_db_service"}):
                if isinstance(node.value, ast.Name) and node.value.id in {
                    "database_service",
                    "db_service",
                }:
                    return True

    forbidden_snippets = (
        "self.database_service",
        "self._database_service",
        "self._db_service",
        "self._db = db_service",
        "database_service=",
        "db_service=",
        "DatabaseHITLPersistenceAdapter",
        "_DatabaseServiceLike",
        '"database_service":',
        '"db_service":',
        "'database_service':",
        "'db_service':",
        "db_service:",
        "db_service =",
        "global db_service",
        '("db_service"',
        "('db_service'",
    )
    return any(snippet in source for snippet in forbidden_snippets)


def test_convergence_scanner_detects_dynamic_imports_and_pinecone_calls(tmp_path):
    sample = tmp_path / "sample.py"
    sample.write_text(
        "\n".join(
            [
                "import importlib",
                "import pinecone",
                'importlib.import_module("database.mongodb")',
                'importlib.import_module("database.pinecone_db")',
                "pc = pinecone.Pinecone(api_key='x')",
                "pc.Index('idx')",
                "client.Index('idx')",
                "self.client.Index('idx')",
            ]
        )
    )

    assert "sample.py:3:database.mongodb" in _entries(_mongo_singleton_blockers(sample))
    assert "sample.py:4:database.pinecone_db" in _entries(
        _pinecone_singleton_blockers(sample)
    )
    direct = _entries(_direct_pinecone_blockers(sample))
    assert "sample.py:2:pinecone" in direct
    assert "sample.py:5:pinecone.Pinecone" in direct
    assert "sample.py:6:pc.Index" in direct
    assert "sample.py:7:client.Index" in direct
    assert "sample.py:8:self.client.Index" in direct


def test_dynamic_import_scanner_detects_dunder_import_application_shell_s3_service(
    tmp_path,
):
    sample = tmp_path / "sample.py"
    removed_runtime_package = REMOVED_RUNTIME_PACKAGE
    sample.write_text(f'__import__("{removed_runtime_package}.s3_service")')

    assert f"sample.py:1:{removed_runtime_package}.s3_service" in _entries(
        _iter_dynamic_imports(sample)
    )


def test_dynamic_import_scanner_detects_importlib_application_shell_s3_service(
    tmp_path,
):
    sample = tmp_path / "sample.py"
    removed_runtime_package = REMOVED_RUNTIME_PACKAGE
    sample.write_text(
        "\n".join(
            [
                "import importlib",
                f'importlib.import_module("{removed_runtime_package}.s3_service")',
            ]
        )
    )

    assert f"sample.py:2:{removed_runtime_package}.s3_service" in _entries(
        _iter_dynamic_imports(sample)
    )


def test_convergence_scanner_detects_from_import_legacy_modules(tmp_path):
    sample = tmp_path / "sample.py"
    removed_runtime_package = REMOVED_RUNTIME_PACKAGE
    sample.write_text(
        "\n".join(
            [
                "from database import mongodb, pinecone_db, repository",
                f"from {removed_runtime_package} import database_service",
            ]
        )
    )

    assert "sample.py:1:database.mongodb" in _entries(_mongo_singleton_blockers(sample))
    assert "sample.py:1:database.pinecone_db" in _entries(
        _pinecone_singleton_blockers(sample)
    )
    assert "sample.py:1:database.repository" in _entries(
        _database_repository_blockers(sample)
    )
    assert f"sample.py:2:{removed_runtime_package}.database_service" in _entries(
        _database_service_blockers(sample)
    )


def test_convergence_scanner_detects_aliased_legacy_backend_binds(tmp_path):
    sample = tmp_path / "sample.py"
    sample.write_text(
        "\n".join(
            [
                "from context_memory.search_adapter import (",
                "    bind_mongo_backend as bind_memory_store,",
                "    bind_pinecone_backend as bind_vector_store,",
                ")",
                "bind_memory_store(mongo)",
                "bind_vector_store(pinecone)",
            ]
        )
    )

    mongo = _entries(_mongo_singleton_blockers(sample))
    assert "sample.py:1:bind_mongo_backend as bind_memory_store" in mongo
    assert "sample.py:5:bind_mongo_backend:bind_memory_store" in mongo

    pinecone = _entries(_pinecone_singleton_blockers(sample))
    assert "sample.py:1:bind_pinecone_backend as bind_vector_store" in pinecone
    assert "sample.py:6:bind_pinecone_backend:bind_vector_store" in pinecone


def test_convergence_manifest_schema_is_current():
    manifest = _manifest()
    assert set(manifest) >= {
        "exclude_dirs",
        "database_service_blockers",
        "mongo_singleton_blockers",
        "database_repository_blockers",
        "pinecone_singleton_blockers",
        "direct_pinecone_blockers",
        "legacy_runtime_files",
    }


def test_database_service_blockers_are_exact():
    assert _all_entries(_database_service_blockers) == sorted(
        _manifest()["database_service_blockers"]
    )


def test_mongo_singleton_blockers_are_exact():
    assert _all_entries(_mongo_singleton_blockers) == sorted(
        _manifest()["mongo_singleton_blockers"]
    )


def test_database_repository_blockers_are_exact():
    assert _all_entries(_database_repository_blockers) == sorted(
        _manifest()["database_repository_blockers"]
    )


def test_pinecone_singleton_blockers_are_exact():
    assert _all_entries(_pinecone_singleton_blockers) == sorted(
        _manifest()["pinecone_singleton_blockers"]
    )


def test_direct_pinecone_blockers_are_exact():
    assert _all_entries(_direct_pinecone_blockers) == sorted(
        _manifest()["direct_pinecone_blockers"]
    )


def test_legacy_runtime_files_are_exact():
    assert _legacy_runtime_file_entries() == sorted(_manifest()["legacy_runtime_files"])


def test_production_object_storage_access_goes_through_dal():
    """Ensure production object storage access goes through DAL ownership."""
    offenders = []
    for path in _py_files():
        rel = _rel(path)
        if rel in OBJECT_STORAGE_SHIM_EXEMPT_FILES:
            continue
        for blocker in [*_iter_imports(path), *_iter_dynamic_imports(path)]:
            if _is_module_match(blocker.symbol, OBJECT_STORAGE_SHIM_IMPORT_PREFIX):
                offenders.append(blocker.as_manifest_entry())
    assert offenders == [], f"Direct S3 imports in production code: {offenders}"


def test_aws_sdk_imports_are_confined_to_dal_s3():
    assert _aws_sdk_import_blockers() == []


def test_aws_sdk_temporary_allowlist_is_exact_and_documented():
    assert AWS_SDK_TEMPORARY_ALLOWLIST == {}

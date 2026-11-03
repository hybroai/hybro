import ast
import inspect
import re
import sys
import tomllib
from datetime import UTC
from pathlib import Path

import pytest

from context_memory.events import ProjectionPort
from context_memory.ports import MemoryRepository
from conversation.deletion import RoomMemoryCleanupPort
from jobs.compaction_sweep import CompactionPort

REMOVED_RUNTIME_PACKAGE = "app_" + "shell"


class FakeMongo:
    def collection(self, name: str):
        return object()


class FakeMemoryRepository:
    async def get_room_memory(self, conversation_id: str) -> dict | None:
        return None

    async def upsert_room_memory(self, conversation_id: str, memory: dict) -> None:
        return None

    async def delete_room_memory(self, conversation_id: str) -> bool:
        return True

    async def create_room_memory(self, memory: dict) -> str:
        return memory["memory_id"]

    async def ensure_room_memory(self, conversation_id: str, defaults: dict) -> dict:
        return {"room_id": conversation_id, **defaults}

    async def push_and_trim_conversation_turn(
        self,
        conversation_id: str,
        turn: dict,
        *,
        max_turns: int,
        summary_stub: str,
        max_summary_chars: int,
    ) -> tuple[bool, bool]:
        return True, True

    async def push_and_trim_conversation_turn_if_absent(
        self,
        conversation_id: str,
        turn: dict,
        *,
        turn_id: str,
        max_turns: int,
        summary_stub: str,
        max_summary_chars: int,
    ) -> tuple[bool, bool, bool]:
        return True, True, False

    async def compact_turns_bulk(
        self, conversation_id: str, compacted_turns: list[dict]
    ) -> bool:
        return True

    async def list_room_ids_with_memory(self, limit: int | None = None) -> list[str]:
        return []


class FakeContentRepository:
    async def upsert_full_content(self, **kwargs) -> str:
        return kwargs["document_id"]

    async def get_content_by_turn_id(
        self, conversation_id: str, turn_id: str
    ) -> dict | None:
        return None

    async def delete_content_by_turn_id(
        self, conversation_id: str, turn_id: str
    ) -> bool:
        return True

    async def delete_content_by_room_id(self, conversation_id: str) -> int:
        return 0


class FakeRoomHistoryReader:
    async def get_messages_for_room(
        self, conversation_id: str, limit: int, before=None
    ):
        return []

    async def get_messages_by_ids(self, message_ids: list[str]):
        return []

    async def get_message_thread(self, parent_message_id: str):
        return []


def _facade():
    from datetime import datetime

    from context_memory import ContextMemoryFacade

    return ContextMemoryFacade(
        memory_repository=FakeMemoryRepository(),
        content_repository=FakeContentRepository(),
        room_history_reader=FakeRoomHistoryReader(),
        id_factory=lambda: "id-1",
        now=lambda: datetime.now(UTC),
    )


def test_context_memory_port_conformance():
    from context_memory import ContentStorageMongoRepository, MemoryMongoRepository
    from context_memory.ports import ContentStorageRepository

    facade = _facade()

    assert isinstance(facade, ProjectionPort)
    assert isinstance(facade, CompactionPort)
    assert isinstance(facade, RoomMemoryCleanupPort)
    assert isinstance(MemoryMongoRepository(mongo=FakeMongo()), MemoryRepository)
    assert isinstance(
        ContentStorageMongoRepository(mongo=FakeMongo()), ContentStorageRepository
    )


def test_retired_context_memory_residue_does_not_return():
    retired_text = {
        "ChatContext",
        "RuntimeChatContext",
        "ChatContextGenerationInput",
        "LegacyChatContextAPI",
        "/memoryCenter/addChatContext",
        "/memoryCenter/getChatContextBySessionId",
        "/memoryCenter/updateChatContextBySessionId",
        "/memoryCenter/deleteChatContextBySessionId",
        "legacy_search",
        "legacy_create_room_memory",
        "legacy_get_room_memory_by_room_id",
        "legacy_get_room_memory_by_memory_id",
        "legacy_update_room_memory_by_room_id",
        "legacy_update_room_memory_by_memory_id",
        "legacy_delete_room_memory_by_room_id",
        "legacy_delete_room_memory_by_memory_id",
        "initialize_or_update_room_memory",
        "add_agent_response_to_memory",
        "RoomCenterMemoryRequest",
        "RoomCenterMemoryResponse",
        "RoomMemoryInfo",
        "add_turn_to_history",
        "build_context_for_agent",
        "context_memory.compat",
        "context_memory.legacy_assembly",
        "context_memory_legacy_json_model",
    }
    checked_suffixes = {".py", ".json", ".toml", ".yaml", ".yml"}
    violations: list[str] = []
    for path in Path(".").rglob("*"):
        if (
            not path.is_file()
            or path.suffix not in checked_suffixes
            or "tests" in path.parts
            or "docs" in path.parts
            or ".venv" in path.parts
        ):
            continue
        source = path.read_text(errors="ignore")
        for residue in retired_text:
            if residue in source:
                violations.append(f"{path}:{residue}")

    retired_paths = {
        Path("context_memory/compat"),
        Path("context_memory/legacy_assembly.py"),
        Path("context_memory/protocols.py"),
        Path("api_gateway/routes/memory_routes.py"),
    }
    violations.extend(str(path) for path in retired_paths if path.exists())

    frontend_types = Path(__file__).resolve().parents[2] / "frontend/src/lib/types"
    retired_frontend_paths = {frontend_types / "memory.ts"}
    violations.extend(str(path) for path in retired_frontend_paths if path.exists())

    nested_history_type = re.compile(
        r"interface\s+MemoryContent\s*\{[^}]*\bconversation_history\b",
        re.DOTALL,
    )
    for path in frontend_types.glob("*.ts"):
        source = path.read_text()
        for residue in retired_text:
            if residue in source:
                violations.append(f"{path}:{residue}")
        if nested_history_type.search(source):
            violations.append(f"{path}:MemoryContent.conversation_history")

    # extend_info is active generic room/message metadata, not ChatContext residue.
    assert "extend_info" not in retired_text
    assert violations == []


def test_context_memory_exports_are_stable():
    import context_memory
    from context_memory import (
        ContentStorageMongoRepository,
        ContextMemoryFacade,
        MemoryMongoRepository,
    )
    from context_memory.repository import (
        ContentStorageMongoRepository as RepoContentStorageMongoRepository,
    )
    from context_memory.repository import (
        MemoryMongoRepository as RepoMemoryMongoRepository,
    )

    assert ContextMemoryFacade.__name__ == "ContextMemoryFacade"
    assert MemoryMongoRepository is RepoMemoryMongoRepository
    assert ContentStorageMongoRepository is RepoContentStorageMongoRepository
    assert context_memory.__all__ == [
        "ContextMemoryFacade",
        "MemoryMongoRepository",
        "ContentStorageMongoRepository",
    ]


def test_context_memory_packages_are_listed():
    pyproject = tomllib.loads(Path("pyproject.toml").read_text())
    packages = set(pyproject["tool"]["setuptools"]["packages"])

    assert {"context_memory", "context_memory.repository"}.issubset(packages)


def test_context_memory_protocol_method_sets():
    from context_memory import ports as protocols

    def public_methods(protocol):
        return {
            name
            for name, member in protocol.__dict__.items()
            if inspect.isfunction(member) and not name.startswith("_")
        }

    assert public_methods(protocols.ContentStorageRepository) == {
        "upsert_full_content",
        "get_content_by_turn_id",
        "delete_content_by_turn_id",
        "delete_content_by_room_id",
    }


def test_context_memory_facade_uses_settings_compaction_concurrency(monkeypatch):
    from common.config import settings
    from container import create_context_memory_facade

    monkeypatch.setattr(settings, "compaction_concurrency", 11)

    facade = create_context_memory_facade(
        mongo=FakeMongo(),
        room_history_reader=FakeRoomHistoryReader(),
    )

    assert facade.compaction_config.concurrency == 11


def test_context_memory_config_defaults_read_common_settings(monkeypatch):
    from common.config import settings
    from context_memory.config import CompactionConfig

    monkeypatch.setattr(settings, "compaction_enabled", False)
    monkeypatch.setattr(settings, "compaction_max_full_turns", 7)
    monkeypatch.setattr(settings, "compaction_max_total_tokens", 777)
    monkeypatch.setattr(settings, "compaction_preserve_recent", 3)
    monkeypatch.setattr(settings, "compaction_content_ttl_days", 9)
    monkeypatch.setattr(settings, "compaction_concurrency", 4)

    compaction = CompactionConfig()

    assert compaction.enabled is False
    assert compaction.max_full_turns == 7
    assert compaction.max_total_tokens == 777
    assert compaction.preserve_recent_turns == 3
    assert compaction.content_ttl_days == 9
    assert compaction.concurrency == 4


def test_context_memory_setting_helper_does_not_swallow_import_failures(monkeypatch):
    import builtins
    import importlib

    context_memory_config = importlib.import_module("context_memory.config")

    real_import = builtins.__import__

    def failing_import(name, *args, **kwargs):
        if name == "common.config":
            raise RuntimeError("settings validation failed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", failing_import)

    with pytest.raises(RuntimeError, match="settings validation failed"):
        context_memory_config._setting("context_model_window", 128000)


def test_room_delete_has_no_stale_direct_context_memory_cleanup():
    from conversation.deletion import RoomDeletionService

    source = inspect.getsource(RoomDeletionService.delete_room_by_room_id)

    assert "room_memories_collection" not in source
    assert "conversation_content_collection" not in source


def test_room_delete_logs_when_context_memory_cleanup_is_unbound():
    from conversation.deletion import RoomDeletionService

    source = inspect.getsource(RoomDeletionService._cleanup_context_memory_for_room)

    assert '"cleanup_unbound"' in source
    assert "_memory_cleanup is None" in source


def test_message_write_flows_do_not_call_context_memory_write_shims():
    forbidden = {
        "initialize_or_update_room_memory",
        "add_agent_response_to_memory",
    }
    checked_paths = [
        Path("conversation/runtime.py"),
    ]
    violations: list[str] = []
    for path in checked_paths:
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr in forbidden:
                    violations.append(f"{path}:{node.lineno}:{node.func.attr}")

    assert violations == []


def test_execution_startup_adapter_does_not_inject_agent_memory_write_shim():
    source = Path("container.py").read_text()

    for shim in {
        "initialize_or_update_room_memory",
        "add_agent_response_to_memory",
    }:
        assert f"room_memory_service.{shim}" not in source
        assert f"{shim}=(" not in source


def test_context_memory_import_boundary():
    forbidden = {
        "a2a_adapter",
        "agent",
        "api",
        "config",
        "container",
        "database",
        "infrastructure",
        "llm_gateway",
        "main",
        "models",
        "modules",
        "openai",
        "pymongo",
        "chat",
        "conversation",
        "services",
    }
    allowed_stdlib = set(sys.stdlib_module_names) | {"__future__"}
    allowed_roots = allowed_stdlib | {"common", "context_memory"}
    path_legacy_compat_imports: dict[Path, set[str]] = {}

    for path in Path("context_memory").rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            root = None
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name in path_legacy_compat_imports.get(path, set()):
                        continue
                    root = alias.name.split(".", 1)[0]
                    assert root in allowed_roots and root not in forbidden, (
                        path,
                        alias.name,
                    )
            elif isinstance(node, ast.ImportFrom) and node.module:
                if node.module in path_legacy_compat_imports.get(path, set()):
                    continue
                root = node.module.split(".", 1)[0]
                assert root in allowed_roots and root not in forbidden, (
                    path,
                    node.module,
                )


def test_non_protocol_helper_call_boundary():
    allowed_call_sites = {
        f"{REMOVED_RUNTIME_PACKAGE}/memory_service.py": {
            "add_synthesis_to_history",
            "update_room_summary",
        },
        f"{REMOVED_RUNTIME_PACKAGE}/compaction_service.py": {
            "should_compact",
            "compact_if_needed",
            "compact_room_memory",
            "expand_turn_content",
            "expand_turn_content_from_turn",
            "fetch_turn_content",
            "get_compaction_stats",
        },
        "context_memory/events.py": {"project_message_for_event"},
    }
    helper_names = set().union(*allowed_call_sites.values())
    path_allowed_helpers = {
        Path(path): helpers for path, helpers in allowed_call_sites.items()
    }

    violations = []
    for path in Path(".").rglob("*.py"):
        if (
            path.parts[0] == "tests"
            or (path.parts[0] == "context_memory" and path not in path_allowed_helpers)
            or path in {Path("container.py"), Path("main.py")}
            or ".venv" in path.parts
            or ".worktrees" in path.parts
        ):
            continue
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = None
            receiver = ""
            if isinstance(node.func, ast.Attribute):
                name = node.func.attr
                receiver = ast.unparse(node.func.value)
            elif isinstance(node.func, ast.Name):
                name = node.func.id
            if name not in helper_names:
                continue
            if isinstance(node.func, ast.Attribute) and "facade" not in receiver:
                continue
            if name not in path_allowed_helpers.get(path, set()):
                violations.append(f"{path}:{node.lineno}:{name}")

    assert violations == []

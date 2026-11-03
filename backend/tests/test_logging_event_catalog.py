"""Backend event catalog contract and the logging call-site guard."""

import ast
import json
import logging
import re
from pathlib import Path

from common.observability import events, get_logger
from common.observability.catalog import EventSpec
from common.observability.logging import StructuredFormatter

BACKEND_ROOT = Path(__file__).resolve().parents[1]

_SKIPPED_PARTS = frozenset({"tests", "build", ".venv", "scripts", "__pycache__"})
_LOG_METHODS = frozenset(
    {"debug", "info", "warning", "warn", "error", "exception", "critical", "log"}
)
_LOGGER_RECEIVER = re.compile(r"^_?(log|logger|[a-z_]+_logger)$")
# Files allowed to use stdlib logging directly:
# - logging.py configures library loggers;
# - catalog.py holds EventLogger, which calls the wrapped stdlib logger;
# - runtime_entrypoint.py prints to stderr before logging exists.
_EXCEPTED = frozenset(
    {
        "common/observability/logging.py",
        "common/observability/catalog.py",
        "runtime_entrypoint.py",
    }
)
# Migration ratchet: files that still call stdlib logger methods. Each
# migration batch removes its files; the set is deleted once it is empty.
_PENDING_MIGRATION = frozenset(
    {
        "agent/bundled.py",
        "agent/catalog.py",
        "agent/facade.py",
        "agent/health.py",
        "agent/inspection.py",
        "agent/liveness.py",
        "agent/repository/documents.py",
        "agent/repository/runtime_store.py",
        "agent/selection_service.py",
        "approval/application.py",
        "approval/reconciler.py",
        "approval/repository/hitl_lifecycle_store.py",
        "approval/repository/hitl_store.py",
        "approval/service.py",
        "conversation/deletion.py",
        "conversation/facade.py",
        "conversation/membership_source.py",
        "conversation/mentions.py",
        "conversation/message_reads.py",
        "conversation/message_sending.py",
        "conversation/preflight.py",
        "conversation/repository/indexes.py",
        "conversation/repository/message_documents.py",
        "conversation/repository/messages.py",
        "conversation/repository/rooms.py",
        "conversation/target_scope.py",
        "conversation/timeline_projection.py",
        "conversation/user_message_persistence.py",
    }
)


def _event_spec_names() -> frozenset[str]:
    return frozenset(
        name for name, value in vars(events).items() if isinstance(value, EventSpec)
    )


def _receiver_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _is_logger(node: ast.expr) -> bool:
    if isinstance(node, ast.Call):
        # get_logger(__name__).info(...)
        return _receiver_name(node.func) == "get_logger"
    name = _receiver_name(node)
    return name is not None and (
        name == "logging" or bool(_LOGGER_RECEIVER.fullmatch(name))
    )


def _violations(source: str, path: str, spec_names: frozenset[str]) -> list[str]:
    if path in _EXCEPTED:
        return []
    found: list[tuple[int, str]] = []
    for node in ast.walk(ast.parse(source)):
        if (
            isinstance(node, ast.Attribute)
            and node.attr in _LOG_METHODS
            and _is_logger(node.value)
        ):
            found.append((node.lineno, f"stdlib logger.{node.attr}"))
        if isinstance(node, ast.ImportFrom) and node.module == "logging":
            found.extend(
                (node.lineno, f"logging.{alias.name}")
                for alias in node.names
                if alias.name in {"getLogger", "basicConfig"}
            )
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        func = node.func
        if func.attr == "emit" and _is_logger(func.value):
            first = node.args[0] if node.args else None
            if not (isinstance(first, ast.Attribute) and first.attr in spec_names):
                found.append((node.lineno, "emit without a catalog event"))
        if (
            func.attr in {"getLogger", "basicConfig"}
            and isinstance(func.value, ast.Name)
            and func.value.id == "logging"
        ):
            found.append((node.lineno, f"logging.{func.attr}"))
    return [f"{path}:{line}: {problem}" for line, problem in sorted(found)]


def _scan() -> dict[str, list[str]]:
    spec_names = _event_spec_names()
    result: dict[str, list[str]] = {}
    for file in sorted(BACKEND_ROOT.rglob("*.py")):
        relative = file.relative_to(BACKEND_ROOT)
        if _SKIPPED_PARTS & set(relative.parts):
            continue
        path = relative.as_posix()
        found = _violations(file.read_text(encoding="utf-8"), path, spec_names)
        if found:
            result[path] = found
    return result


def test_logging_call_sites_emit_catalog_events():
    scanned = _scan()

    unmigrated = {
        path: found for path, found in scanned.items() if path not in _PENDING_MIGRATION
    }
    assert unmigrated == {}, (
        "Log through get_logger(__name__).emit(events.X, **fields):\n"
        + "\n".join(line for found in unmigrated.values() for line in found)
    )


def test_pending_migration_entries_are_not_stale():
    scanned = _scan()

    stale = sorted(
        path
        for path in _PENDING_MIGRATION
        if not (BACKEND_ROOT / path).is_file() or path not in scanned
    )
    assert stale == [], f"Remove migrated paths from _PENDING_MIGRATION: {stale}"


def test_guard_reports_free_text_aliases_and_unknown_events():
    snippet = (
        "logger.info('x')\n"
        "m = logger.error\n"
        "logger.emit(E.NOPE)\n"
        "logger.emit(E.HTTP_REQUEST_COMPLETED, status=200)\n"
        "audit.info('not a logger')\n"
        "logging.warning('root logger')\n"
        "get_logger(__name__).info('chained')\n"
        "from logging import getLogger\n"
    )
    spec_names = _event_spec_names()

    found = _violations(snippet, "planted/module.py", spec_names)
    excepted = _violations(snippet, "common/observability/catalog.py", spec_names)

    assert found == [
        "planted/module.py:1: stdlib logger.info",
        "planted/module.py:2: stdlib logger.error",
        "planted/module.py:3: emit without a catalog event",
        "planted/module.py:6: stdlib logger.warning",
        "planted/module.py:7: stdlib logger.info",
        "planted/module.py:8: logging.getLogger",
    ]
    assert excepted == []


def test_guard_reports_direct_stdlib_logger_configuration():
    snippet = "logger = logging.getLogger(__name__)\nlogging.basicConfig()\n"

    assert _violations(snippet, "planted/module.py", _event_spec_names()) == [
        "planted/module.py:1: logging.getLogger",
        "planted/module.py:2: logging.basicConfig",
    ]


def test_every_catalog_event_formats_as_its_name(caplog):
    formatter = StructuredFormatter(
        output_format="json", environment="test", service_version="test"
    )
    specs = list(events.CATALOG)
    logger = get_logger("tests.event_catalog")

    with caplog.at_level(logging.DEBUG, logger="tests.event_catalog"):
        for spec in specs:
            logger.emit(spec, **dict.fromkeys(spec.required))

    records = [r for r in caplog.records if r.name == "tests.event_catalog"]
    assert len(records) == len(specs)
    for spec, record in zip(specs, records, strict=True):
        output = json.loads(formatter.format(record))
        assert output["event"] == spec.name
        assert "message_redacted" not in output
        assert "catalog_violation" not in output


def test_catalog_names_are_unique_and_constants_match_registry():
    specs = list(events.CATALOG)
    constants = [
        value for value in vars(events).values() if isinstance(value, EventSpec)
    ]

    assert len({spec.name for spec in specs}) == len(specs)
    assert {spec.name for spec in constants} == {spec.name for spec in specs}


def test_transport_events_carry_their_call_site_fields():
    assert events.HTTP_REQUEST_COMPLETED.required == {
        "request_id",
        "method",
        "route",
        "status",
        "outcome",
        "duration_ms",
    }
    assert events.LLM_CALL_COMPLETED.required == {
        "provider",
        "model",
        "operation",
        "attempt",
        "outcome",
        "duration_ms",
    }
    assert events.A2A_CALL_COMPLETED.required == {
        "agent",
        "operation",
        "attempt",
        "outcome",
        "duration_ms",
    }
    assert all(
        spec.system
        for spec in (
            events.HTTP_REQUEST_COMPLETED,
            events.LLM_CALL_COMPLETED,
            events.A2A_CALL_COMPLETED,
        )
    )

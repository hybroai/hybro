import asyncio
import json
import logging
import runpy
import shlex
import sys
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from common.observability.logging import (
    StructuredFormatter,
    bind_log_context,
    configure_logging,
    get_log_context,
    safe_exception_metadata,
)
from common.observability.tracing import traced_create_task


class _Outcome(StrEnum):
    OK = "ok"


def _record(message: str = "test_event", **extra) -> logging.LogRecord:
    record = logging.LogRecord(
        "tests.observability",
        logging.INFO,
        __file__,
        1,
        message,
        (),
        None,
    )
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def _formatter(output_format: str = "json") -> StructuredFormatter:
    return StructuredFormatter(
        output_format=output_format,
        environment="test",
        service_version="test-version",
    )


def test_json_formatter_includes_context_extra_and_fixed_fields():
    with bind_log_context(trace_id="trace-1", conversation_id="room-1"):
        output = json.loads(
            _formatter().format(
                _record(
                    _hybro_event=True,
                    nested={"answer": 42, "outcome": _Outcome.OK},
                    happened_at=datetime(2026, 1, 2, tzinfo=UTC),
                )
            )
        )

    assert output["event"] == "test_event"
    assert output["service"] == "hybro-backend"
    assert output["environment"] == "test"
    assert output["version"] == "test-version"
    assert len(output["instance_id"]) == 8
    assert output["trace_id"] == "trace-1"
    assert output["conversation_id"] == "room-1"
    assert output["nested.answer"] == 42
    assert output["nested.outcome"] == "ok"
    assert output["happened_at"] == "2026-01-02T00:00:00+00:00"
    assert get_log_context() == {}


def test_formatter_handles_cycles_bytes_unserializable_and_truncation():
    cyclic: dict = {}
    cyclic["self"] = cyclic

    class Broken:
        def __str__(self):
            raise RuntimeError("cannot render")

    output = json.loads(
        _formatter().format(
            _record(
                details=cyclic,
                binary=b"private bytes",
                broken=Broken(),
                oversized="x" * 3000,
            )
        )
    )

    assert output["details.self"] == "<circular>"
    assert output["binary"] == "<bytes length=13>"
    assert output["broken"] == "<unserializable Broken>"
    assert "[TRUNCATED" in output["oversized"]


def test_formatter_redacts_secrets_content_and_url_credentials():
    sentinel = "PRIVATE_SENTINEL"
    output = _formatter().format(
        _record(
            authorization=f"Bearer {sentinel}",
            headers={"x-api-key": sentinel},
            prompt=sentinel,
            content=sentinel,
            raw_failure_detail=sentinel,
            base64=sentinel,
            endpoint=f"https://user:{sentinel}@example.com/path?q={sentinel}#fragment",
            malformed_endpoint=(
                f"https://user:{sentinel}@example.com:invalid/path"
                f"?token={sentinel}#fragment"
            ),
        )
    )

    assert sentinel not in output
    parsed = json.loads(output)
    assert parsed["authorization"] == "[REDACTED]"
    assert parsed["headers"] == "[REDACTED]"
    assert parsed["prompt"] == "[REDACTED]"
    assert parsed["content"] == "[REDACTED]"
    assert parsed["raw_failure_detail"] == "[REDACTED]"
    assert parsed["base64"] == "[REDACTED]"
    assert parsed["endpoint"] == "https://example.com/path"
    assert parsed["malformed_endpoint"] == "https://example.com/path"


def test_formatter_safely_renders_legacy_arguments_and_embedded_urls():
    sentinel = "PRIVATE_LEGACY_EXCEPTION_SENTINEL"
    endpoint = f"https://user:{sentinel}@example.com/path?token={sentinel}#fragment"
    record = logging.LogRecord(
        "tests.observability",
        logging.ERROR,
        __file__,
        1,
        "legacy failure: %s endpoint=%s attempt=%d",
        (RuntimeError(sentinel), endpoint, 2),
        None,
    )

    output = json.loads(_formatter().format(record))
    embedded = json.loads(
        _formatter().format(_record(message=f"failed at ({endpoint})."))
    )

    assert output["message"] == (
        "legacy failure: RuntimeError endpoint=https://example.com/path attempt=2"
    )
    assert embedded["message"] == "[REDACTED]"
    assert sentinel not in json.dumps(output)
    assert sentinel not in json.dumps(embedded)


def test_formatter_preserves_safe_exception_stack_without_message():
    sentinel = "PRIVATE_EXCEPTION_SENTINEL"

    def explode():
        raise RuntimeError(sentinel)

    try:
        explode()
    except RuntimeError:
        record = _record()
        record.exc_info = sys.exc_info()

    output = json.loads(_formatter().format(record))

    assert output["error_type"] == "RuntimeError"
    assert output["error_chain"] == "RuntimeError"
    assert len(output["error_fingerprint"]) == 16
    assert "test_observability_logging.py:" in output["error_stack"]
    assert ":explode" in output["error_stack"]
    assert sentinel not in json.dumps(output)
    assert len(output["error_stack"]) <= 2 * 1024 + 64


def test_safe_exception_fingerprint_is_stable_and_excludes_chained_messages():
    def explode():
        try:
            raise ValueError("PRIVATE_INNER_SENTINEL")
        except ValueError as exc:
            raise RuntimeError("PRIVATE_OUTER_SENTINEL") from exc

    captured: RuntimeError | None = None
    try:
        explode()
    except RuntimeError as exc:
        captured = exc

    assert captured is not None
    first = safe_exception_metadata(captured)
    second = safe_exception_metadata(captured)

    assert first["error_chain"] == "RuntimeError <- ValueError"
    assert first["error_fingerprint"] == second["error_fingerprint"]
    assert ":explode" in first["error_stack"]
    assert "PRIVATE_INNER_SENTINEL" not in json.dumps(first)
    assert "PRIVATE_OUTER_SENTINEL" not in json.dumps(first)


def test_logfmt_is_parseable_and_preserves_extra():
    record = logging.LogRecord(
        "tests.observability",
        logging.INFO,
        __file__,
        1,
        "legacy status: %s",
        ("safe",),
        None,
    )
    record.outcome = "success"
    record.duration_ms = 1.5
    output = _formatter("logfmt").format(record)
    fields = dict(part.split("=", 1) for part in shlex.split(output))

    assert fields["event"] == "library_message"
    assert fields["message"] == "legacy status: [REDACTED]"
    assert fields["outcome"] == "success"
    assert fields["duration_ms"] == "1.5"


def test_nested_bind_restores_outer_context_and_rejects_unknown_fields():
    with bind_log_context(trace_id="outer"):
        with bind_log_context(conversation_id="room"):
            assert get_log_context() == {"trace_id": "outer", "conversation_id": "room"}
        with bind_log_context(trace_id=None):
            assert get_log_context() == {}
        with bind_log_context(trace_id={"PRIVATE_TRACE": "payload"}):
            assert get_log_context() == {}
        with bind_log_context(trace_id="x" * 129):
            assert get_log_context() == {}
        assert get_log_context() == {"trace_id": "outer"}
    assert get_log_context() == {}

    with pytest.raises(ValueError, match="Unsupported log context"):
        with bind_log_context(unknown="value"):
            pass


@pytest.mark.asyncio
async def test_traced_tasks_copy_context_without_cross_task_leaks():
    release = asyncio.Event()

    async def observe():
        await release.wait()
        return get_log_context()

    with bind_log_context(trace_id="trace-a", conversation_id="room-a"):
        first = traced_create_task(observe(), name="trace-a")
    with bind_log_context(trace_id="trace-b", conversation_id="room-b"):
        second = traced_create_task(observe(), name="trace-b")

    release.set()
    first_context, second_context = await asyncio.gather(first, second)

    assert first_context == {"trace_id": "trace-a", "conversation_id": "room-a"}
    assert second_context == {"trace_id": "trace-b", "conversation_id": "room-b"}
    assert get_log_context() == {}


def test_configure_logging_is_idempotent_and_auto_selects_format():
    root = logging.getLogger()
    original_level = root.level
    original_handlers = tuple(root.handlers)
    original_handler_state = {
        handler: (handler.level, handler.formatter) for handler in original_handlers
    }
    settings = SimpleNamespace(
        app_env="development",
        log_level="INFO",
        log_format="auto",
    )
    try:
        configure_logging(settings)
        first_handlers = tuple(root.handlers)
        foreign_handler = logging.StreamHandler()
        root.addHandler(foreign_handler)
        configure_logging(settings)

        assert tuple(root.handlers) == first_handlers
        assert foreign_handler not in root.handlers
        configured_handler = next(
            handler
            for handler in first_handlers
            if isinstance(handler.formatter, StructuredFormatter)
        )
        assert configured_handler.formatter.output_format == "logfmt"

        configure_logging(
            SimpleNamespace(
                app_env="production",
                log_level="WARNING",
                log_format="auto",
            )
        )
        assert configured_handler.formatter.output_format == "json"
        assert root.level == logging.WARNING
    finally:
        root.setLevel(original_level)
        for handler in tuple(root.handlers):
            if handler not in original_handlers:
                root.removeHandler(handler)
        for handler in original_handlers:
            if handler not in root.handlers:
                root.addHandler(handler)
            level, formatter = original_handler_state[handler]
            handler.setLevel(level)
            handler.setFormatter(formatter)


def test_configure_logging_names_the_service():
    root = logging.getLogger()
    original_level = root.level
    original_handlers = tuple(root.handlers)
    original_handler_state = {
        handler: (handler.level, handler.formatter) for handler in original_handlers
    }
    try:
        configure_logging(
            SimpleNamespace(log_level="INFO", log_format="json"), service="hybro-mcp"
        )
        [formatter] = [
            handler.formatter
            for handler in root.handlers
            if isinstance(handler.formatter, StructuredFormatter)
        ]
        assert formatter.service == "hybro-mcp"
        assert formatter.output_format == "json"
    finally:
        root.setLevel(original_level)
        for handler in tuple(root.handlers):
            if handler not in original_handlers:
                root.removeHandler(handler)
        for handler in original_handlers:
            if handler not in root.handlers:
                root.addHandler(handler)
            level, formatter = original_handler_state[handler]
            handler.setLevel(level)
            handler.setFormatter(formatter)


def test_console_entrypoint_does_not_replace_structured_logging(monkeypatch):
    import uvicorn

    import main

    captured: dict = {}
    monkeypatch.setattr(
        uvicorn,
        "run",
        lambda *args, **kwargs: captured.update(args=args, kwargs=kwargs),
    )

    main.main()

    assert captured["kwargs"]["log_config"] is None
    assert captured["kwargs"]["access_log"] is False


def test_python_module_entrypoint_owns_uvicorn_logging(monkeypatch):
    import uvicorn

    captured: dict = {}
    bootstrap = ModuleType("common.observability.bootstrap")
    bootstrap.settings = SimpleNamespace()
    monkeypatch.setitem(sys.modules, "common.observability.bootstrap", bootstrap)
    monkeypatch.setattr(
        uvicorn,
        "run",
        lambda *args, **kwargs: captured.update(args=args, kwargs=kwargs),
    )

    runpy.run_path(
        str(Path(__file__).parents[1] / "__main__.py"),
        run_name="__main__",
    )

    assert captured["kwargs"]["log_config"] is None
    assert captured["kwargs"]["access_log"] is False


def test_formatter_rejects_hostile_keys_spoofed_context_and_nonfinite_numbers():
    class HostileKey:
        def __str__(self):
            raise RuntimeError("PRIVATE_KEY_SENTINEL")

    with bind_log_context(trace_id="trusted-trace"):
        record = _record(
            trace_id="spoofed-trace",
            nested={
                "line\nbreak=value": "safe",
                "pass\nword": "PRIVATE_HOSTILE_PASSWORD_SENTINEL",
                HostileKey(): "also-safe",
                "\ud800": "\ud800",
            },
            nan=float("nan"),
            infinity=float("inf"),
        )
        output = _formatter().format(record)

    parsed = json.loads(output)
    assert parsed["trace_id"] == "trusted-trace"
    assert parsed["nan"] == "<non-finite>"
    assert parsed["infinity"] == "<non-finite>"
    assert "\n" not in output
    assert "PRIVATE_KEY_SENTINEL" not in output
    assert "PRIVATE_HOSTILE_PASSWORD_SENTINEL" not in output
    assert "\ud800" not in output
    assert "[REDACTED]" in output
    assert any(key.startswith("nested.line_break_value_") for key in parsed)
    assert any(key.startswith("nested.HostileKey") for key in parsed)


def test_formatter_redacts_prerendered_text_and_malformed_urls():
    sentinel = "PRIVATE_PRERENDERED_SENTINEL"
    output = _formatter().format(
        _record(
            message=(
                f"failure {sentinel} at "
                f"https:/alice:pw@example.com/path?q={sentinel}#fragment"
            )
        )
    )

    assert sentinel not in output
    assert "alice" not in output
    assert "pw" not in output
    parsed = json.loads(output)
    assert parsed["message"] == "[REDACTED]"
    assert parsed["message_redacted"] is True
    assert parsed["source"].endswith("test_observability_logging.py:1:None")


def test_release_version_reads_the_nearest_version_file(tmp_path):
    from common.observability.logging import release_version

    (tmp_path / "VERSION").write_text("1.2.3\n", encoding="utf-8")
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)

    assert release_version(nested / "module.py") == "1.2.3"


def test_release_version_is_unknown_without_a_version_file(tmp_path):
    from common.observability.logging import release_version

    assert release_version(tmp_path / "module.py") == "unknown"


def test_formatter_defaults_to_the_repository_version():
    import common.observability.logging as logging_module

    repo_root = Path(__file__).resolve().parents[2]
    expected = (repo_root / "VERSION").read_text(encoding="utf-8").strip()

    assert not hasattr(logging_module, "_package_version")
    assert (
        StructuredFormatter(output_format="json", environment="test").service_version
        == expected
    )


def test_image_ships_the_version_file():
    dockerfile = Path(__file__).resolve().parents[2] / "Dockerfile"

    assert (
        "COPY VERSION /app/VERSION"
        in dockerfile.read_text(encoding="utf-8").splitlines()
    )


def test_release_version_is_unknown_when_the_file_is_unreadable(tmp_path):
    from common.observability.logging import release_version

    (tmp_path / "VERSION").write_bytes(b"\xff\xfe")

    assert release_version(tmp_path / "module.py") == "unknown"


def _library_record(logger_name: str, message: str, *args) -> logging.LogRecord:
    return logging.getLogger(logger_name).makeRecord(
        logger_name, logging.INFO, __file__, 1, message, args, None
    )


def test_passthrough_library_text_is_kept_and_sanitised():
    free_text = json.loads(
        _formatter().format(
            _library_record("uvicorn.error", "Waiting for application startup.")
        )
    )
    templated = json.loads(
        _formatter().format(
            _library_record(
                "uvicorn.error",
                "Uvicorn running on %s://%s:%d",
                "http",
                "127.0.0.1",
                8000,
            )
        )
    )
    credentialed = json.loads(
        _formatter().format(
            _library_record("py.warnings", "connect to %s", "mongodb://user:pw@host")
        )
    )

    assert free_text["event"] == "library_message"
    assert free_text["logger"] == "uvicorn.error"
    assert free_text["message"] == "Waiting for application startup."
    assert "message_redacted" not in free_text
    assert templated["message"] == "Uvicorn running on http://127.0.0.1:8000"
    assert credentialed["message"] == "connect to mongodb://host"


def test_other_library_records_keep_todays_redaction():
    free_text = json.loads(
        _formatter().format(_library_record("some.library", "connected to cluster"))
    )
    templated = json.loads(
        _formatter().format(
            _library_record(
                "some.library",
                "Uvicorn running on %s://%s:%d",
                "http",
                "127.0.0.1",
                8000,
            )
        )
    )
    event_shaped = json.loads(
        _formatter().format(_library_record("some.library", "cache_warmed"))
    )

    assert free_text["event"] == "library_message"
    assert free_text["logger"] == "some.library"
    assert free_text["message"] == "[REDACTED]"
    assert free_text["message_redacted"] is True
    assert templated["event"] == "library_message"
    assert templated["message"] == "Uvicorn running on [REDACTED]://[REDACTED]:8000"
    assert "message_redacted" not in templated
    assert event_shaped["event"] == "library_message"
    assert event_shaped["message"] == "cache_warmed"
    assert "message_redacted" not in event_shaped


def test_reserved_name_sets_live_in_the_catalog_module():
    import common.observability.logging as logging_module
    from common.observability import catalog

    assert not hasattr(logging_module, "_STANDARD_RECORD_FIELDS")
    assert not hasattr(logging_module, "_FIXED_FIELDS")
    assert logging_module.RESERVED_RECORD_FIELDS is catalog.RESERVED_RECORD_FIELDS
    assert logging_module.FIXED_FIELDS is catalog.FIXED_FIELDS


def test_emitted_event_renders_its_name(caplog):
    from common.observability import events, get_logger

    spec = events.HTTP_REQUEST_COMPLETED
    with caplog.at_level(logging.INFO, logger="tests.emitted"):
        get_logger("tests.emitted").emit(
            spec,
            request_id="req-1",
            method="GET",
            route="/x",
            status=200,
            outcome="success",
            duration_ms=1.0,
        )

    [record] = [r for r in caplog.records if r.name == "tests.emitted"]
    output = json.loads(_formatter().format(record))
    assert output["event"] == "http_request_completed"
    assert "message" not in output
    assert "_hybro_event" not in output


def test_system_event_output_omits_the_system_marker(caplog):
    from common.observability import events, get_logger

    with caplog.at_level(logging.INFO, logger="tests.emitted"):
        get_logger("tests.emitted").emit(events.JOB_STOPPED, job="j")
        get_logger("tests.emitted").emit(
            events.PROVIDER_CONFIG_RELOADED, provider_state="configured"
        )

    record = [r for r in caplog.records if r.name == "tests.emitted"][-1]
    assert record._hybro_system is True
    output = json.loads(_formatter().format(record))
    assert output["event"] == "provider_config_reloaded"
    assert not [key for key in output if key.startswith("_")]


def test_get_logger_returns_event_logger_bound_to_backend_catalog():
    from common.observability import events, get_logger
    from common.observability.catalog import EventLogger

    logger = get_logger("tests.bound")

    assert isinstance(logger, EventLogger)
    assert logger.catalog is events.CATALOG
    assert logger.name == "tests.bound"


def test_passthrough_is_limited_to_exact_logger_names():
    access = json.loads(
        _formatter().format(_library_record("uvicorn.access", "GET /private"))
    )

    assert access["event"] == "library_message"
    assert access["message"] == "[REDACTED]"
    assert access["message_redacted"] is True


@pytest.mark.parametrize(
    "text",
    [
        "client_secret=PRIVATE_VALUE",
        "refresh_token: PRIVATE_VALUE",
        '{"access_token": "PRIVATE_VALUE"}',
        "x-api-key=PRIVATE_VALUE",
        '{"password": "PRIVATE VALUE"}',
        "password='PRIVATE \\' VALUE'",
        "password=PRIVATE'VALUE}",
        "a" * 80 + "_token=PRIVATE_VALUE",
        "aws_secret_access_key=PRIVATE_VALUE",
        "private_key=PRIVATE_VALUE",
        "secret_key=PRIVATE_VALUE",
        "jwt=PRIVATE_VALUE",
        "credentials=PRIVATE_VALUE",
        "Authorization: Basic PRIVATE_VALUE",
        "authorization: Digest username=PRIVATE_VALUE, nonce=PRIVATE_VALUE",
        '{"Authorization": "Basic PRIVATE VALUE", "x": 1}',
        "Cookie: session=PRIVATE_VALUE; theme=PRIVATE_VALUE",
        "Set-Cookie: sid=PRIVATE_VALUE; Path=/",
        "HTTP_AUTHORIZATION=Basic PRIVATE_VALUE",
        "HTTP_COOKIE=sid=PRIVATE_VALUE",
        "secrets=PRIVATE_VALUE",
        "passwords=PRIVATE_VALUE",
        "api_keys=PRIVATE_VALUE",
        "tokens: PRIVATE_VALUE",
    ],
)
def test_passthrough_text_redacts_suffixed_and_quoted_credentials(text):
    output = _formatter().format(_library_record("py.warnings", "%s", text))

    assert "PRIVATE" not in output
    assert "VALUE" not in output


def test_header_redaction_keeps_the_rest_of_a_quoted_mapping():
    from common.observability.logging import _sanitize_string

    text = "{'Authorization': 'Basic PRIVATE', 'Accept': 'text/html'}"

    assert _sanitize_string(text) == (
        "{'Authorization': [REDACTED], 'Accept': 'text/html'}"
    )


@pytest.mark.parametrize(
    "key",
    [
        "aws_secret_access_key",
        "secret_access_key",
        "secret_key",
        "private_key",
        "signing_key",
        "jwt",
        "id_token",
        "credentials",
        "user_prompt",
        "system_prompt",
        "raw_body",
        "request_payload",
        "message_content",
        "response_headers",
    ],
)
def test_formatter_redacts_sensitive_key_variants(key):
    output = json.loads(_formatter().format(_record(**{key: "PRIVATE_VALUE"})))

    assert output[key] == "[REDACTED]"


def test_formatter_keeps_counts_and_metadata_under_sensitive_words():
    output = json.loads(
        _formatter().format(
            _record(
                tokens_saved=12,
                prompt_tokens=30,
                token_budget=1.5,
                content_type="application/json",
                password=1234,
            )
        )
    )

    assert output["tokens_saved"] == 12
    assert output["prompt_tokens"] == 30
    assert output["token_budget"] == 1.5
    assert output["content_type"] == "application/json"
    # An exact credential name is redacted whatever its type.
    assert output["password"] == "[REDACTED]"


def test_formatter_redacts_numeric_values_of_credential_keys():
    output = json.loads(
        _formatter().format(
            _record(
                db_password=123456,
                otp_token=123456,
                client_secret=42,
                secret_key=123456,
                api_keys="PRIVATE",
                passwords=2,
            )
        )
    )

    for key in ("db_password", "otp_token", "client_secret", "secret_key"):
        assert output[key] == "[REDACTED]", key
    # A plural is redacted as text but, as a number, is a count.
    assert output["api_keys"] == "[REDACTED]"
    assert output["passwords"] == 2


def test_field_key_rules_cover_each_path():
    output = json.loads(
        _formatter().format(
            _record(
                token_present=True,
                tokenizer="cl100k",
                db_passwd=1234,
                session_cookie=99,
                raw_body=3,
                raw_body_text="PRIVATE",
            )
        )
    )

    # Embedded word: a flag stays, text goes.
    assert output["token_present"] is True
    assert output["tokenizer"] == "[REDACTED]"
    # Ending with a credential word: redacted even as a number.
    assert output["db_passwd"] == "[REDACTED]"
    assert output["session_cookie"] == "[REDACTED]"
    # Content suffix: a number stays; a leaf that does not end with it stays.
    assert output["raw_body"] == 3
    assert output["raw_body_text"] == "PRIVATE"


def test_credential_words_are_matched_on_the_leaf_key_only():
    output = json.loads(_formatter().format(_record(**{"token_usage.model": "gpt"})))

    assert output["token_usage.model"] == "gpt"


def test_credential_redaction_is_linear_on_long_identifiers():
    import time

    from common.observability.logging import _sanitize_string

    started = time.perf_counter()
    _sanitize_string("a-" * 5000)
    _sanitize_string("a_" * 5000 + "key")
    _sanitize_string("authorization:" + "x" * 10000)
    assert time.perf_counter() - started < 0.2


@pytest.mark.parametrize(
    ("value", "logged"),
    [
        ("agent-1", "agent-1"),
        ("a.b:c_d-1", "a.b:c_d-1"),
        ("has space", "invalid"),
        ("x" * 129, "invalid"),
        ("", "invalid"),
        (42, "invalid"),
        (None, "invalid"),
    ],
)
def test_log_id_keeps_only_id_shaped_strings(value, logged):
    from common.observability import log_id

    assert log_id(value) == logged


def test_log_ids_gates_and_caps_at_ten():
    from common.observability import log_ids

    assert log_ids(["a", "b c", *[f"id{n}" for n in range(12)]]) == [
        "a",
        "invalid",
        *[f"id{n}" for n in range(8)],
    ]

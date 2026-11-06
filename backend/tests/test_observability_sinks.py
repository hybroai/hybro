"""Log sinks: the sink handler, the ring buffer and capture installation."""

import logging
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from common.observability import (
    bind_log_context,
    events,
    get_logger,
    log_sinks,
    ring_buffer,
    sanitize_text,
)
from common.observability import logging as logging_module
from common.observability.logging import StructuredFormatter, configure_logging
from common.observability.sinks import LogEntry, RingBufferSink, SinkHandler

_SETTINGS = SimpleNamespace(log_level="DEBUG", log_format="json")


@pytest.fixture(autouse=True)
def _restore_logging(monkeypatch):
    root = logging.getLogger()
    original_level = root.level
    original_handlers = tuple(root.handlers)
    original_state = {
        handler: (handler.level, handler.formatter) for handler in original_handlers
    }
    monkeypatch.setattr(logging_module, "_sink_handler", None)
    monkeypatch.setattr(logging_module, "_configured_handler", None)
    try:
        yield
    finally:
        root.setLevel(original_level)
        for handler in tuple(root.handlers):
            if handler not in original_handlers:
                root.removeHandler(handler)
        for handler in original_handlers:
            if handler not in root.handlers:
                root.addHandler(handler)
            level, formatter = original_state[handler]
            handler.setLevel(level)
            handler.setFormatter(formatter)


def _sink_handlers() -> list[SinkHandler]:
    return [h for h in logging.getLogger().handlers if isinstance(h, SinkHandler)]


def _stdout_handlers() -> list[logging.Handler]:
    return [
        h
        for h in logging.getLogger().handlers
        if isinstance(h.formatter, StructuredFormatter)
    ]


def _entry(sequence: int) -> LogEntry:
    return LogEntry(
        sequence=sequence,
        timestamp=datetime.now(UTC),
        level="info",
        level_no=logging.INFO,
        service="hybro-backend",
        event=f"event_{sequence}",
        logger="tests",
        system=False,
        fields={},
    )


def test_emit_reaches_a_sink_rendered_and_redacted():
    configure_logging(_SETTINGS, capture=True)
    received: list[LogEntry] = []
    log_sinks().add_sink(received.append)
    logger = get_logger("tests.sinks")

    with bind_log_context(trace_id="trace-1"):
        logger.emit(events.JOB_RUN_COMPLETED, job="j", duration_ms=3, token="abc")
        logger.emit(events.JOB_STARTED, job="j", interval_seconds=30)

    system_entry, plain_entry = received
    assert system_entry.event == "job_run_completed"
    assert system_entry.level == "info"
    assert system_entry.level_no == logging.INFO
    assert system_entry.service == "hybro-backend"
    assert system_entry.logger == "tests.sinks"
    assert system_entry.system is True
    assert system_entry.timestamp.tzinfo is not None
    assert system_entry.fields["token"] == "[REDACTED]"
    assert system_entry.fields["trace_id"] == "trace-1"
    assert system_entry.fields["job"] == "j"
    assert "instance_id" in system_entry.fields
    assert "version" in system_entry.fields
    for fixed in ("timestamp", "level", "service", "event", "logger"):
        assert fixed not in system_entry.fields
    assert plain_entry.system is False
    assert plain_entry.sequence > system_entry.sequence


def test_a_raising_sink_does_not_stop_other_sinks_or_stdout(capsys):
    configure_logging(_SETTINGS, capture=True)
    received: list[LogEntry] = []

    def broken(entry: LogEntry) -> None:
        raise RuntimeError("sink down")

    log_sinks().add_sink(broken)
    log_sinks().add_sink(received.append)

    get_logger("tests.sinks").emit(events.JOB_STARTED, job="j", interval_seconds=1)

    assert [entry.event for entry in received] == ["job_started"]
    assert "job_started" in capsys.readouterr().out


def test_remove_sink_stops_delivery():
    configure_logging(_SETTINGS, capture=True)
    received: list[LogEntry] = []
    log_sinks().add_sink(received.append)
    log_sinks().remove_sink(received.append)

    get_logger("tests.sinks").emit(events.JOB_STOPPED, job="j")

    assert received == []


def test_ring_buffer_keeps_the_newest_entries_in_order():
    ring = RingBufferSink(capacity=3)
    for sequence in range(5):
        ring.accept(_entry(sequence))

    assert ring.capacity == 3
    assert len(ring) == 3
    assert [entry.sequence for entry in ring.entries()] == [2, 3, 4]


def test_capture_installs_one_sink_handler_and_keeps_it():
    configure_logging(_SETTINGS, capture=True)
    configure_logging(_SETTINGS, capture=True)

    assert len(_sink_handlers()) == 1
    assert len(_stdout_handlers()) == 1
    handler = log_sinks()
    assert handler is _sink_handlers()[0]
    assert handler.formatter is None
    assert ring_buffer() is not None

    configure_logging(_SETTINGS)

    assert _sink_handlers() == [handler]
    assert len(_stdout_handlers()) == 1


def test_without_capture_no_sink_handler_is_installed():
    configure_logging(_SETTINGS)

    assert _sink_handlers() == []
    assert log_sinks() is None
    assert ring_buffer() is None


def test_ring_buffer_collects_captured_records():
    configure_logging(_SETTINGS, capture=True)

    get_logger("tests.sinks").emit(events.JOB_STOPPED, job="ring")

    assert ring_buffer().entries()[-1].event == "job_stopped"


def test_library_records_arrive_as_library_message():
    configure_logging(_SETTINGS, capture=True)
    received: list[LogEntry] = []
    log_sinks().add_sink(received.append)

    logging.getLogger("uvicorn.error").warning("server going away")

    [entry] = received
    assert entry.event == "library_message"
    assert entry.logger == "uvicorn.error"
    assert entry.system is False


def test_sink_handler_follows_the_configured_level():
    configure_logging(
        SimpleNamespace(log_level="INFO", log_format="json"), capture=True
    )
    received: list[LogEntry] = []
    log_sinks().add_sink(received.append)
    child = logging.getLogger("tests.sinks.verbose")
    original = child.level
    child.setLevel(logging.DEBUG)
    try:
        child.debug("below the configured level")
    finally:
        child.setLevel(original)

    assert received == []


def test_sticky_capture_refreshes_the_renderer():
    configure_logging(_SETTINGS, capture=True)
    configure_logging(_SETTINGS, service="hybro-other")
    received: list[LogEntry] = []
    log_sinks().add_sink(received.append)

    get_logger("tests.sinks").emit(events.JOB_STOPPED, job="j")

    assert received[-1].service == "hybro-other"


def test_entry_fields_are_read_only():
    configure_logging(_SETTINGS, capture=True)
    received: list[LogEntry] = []
    log_sinks().add_sink(received.append)

    get_logger("tests.sinks").emit(events.JOB_STOPPED, job="j")

    with pytest.raises(TypeError):
        received[-1].fields["job"] = "changed"  # type: ignore[index]


def test_a_raising_renderer_reaches_no_sink_and_does_not_raise():
    class _Broken(StructuredFormatter):
        def event_dict(self, record):
            raise RuntimeError("render failed")

    handler = SinkHandler(_Broken(output_format="json", environment="test"))
    received: list[LogEntry] = []
    handler.add_sink(received.append)

    handler.handle(logging.makeLogRecord({"msg": "x", "levelno": logging.INFO}))

    assert received == []


def test_bootstrap_installs_capture():
    import importlib

    import common.observability.bootstrap as bootstrap

    importlib.reload(bootstrap)

    assert log_sinks() is not None
    assert ring_buffer() is not None


def test_sanitize_text_redacts_and_truncates():
    assert "hunter2" not in sanitize_text("mongodb://u:hunter2@db/x", limit=100)
    assert sanitize_text("x" * 20, limit=5).startswith("xxxxx...[TRUNCATED")

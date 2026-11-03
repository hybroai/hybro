import logging
import sys

import pytest

from common.observability.catalog import (
    FIXED_FIELDS,
    RESERVED_RECORD_FIELDS,
    Catalog,
    CatalogViolation,
    EventLogger,
)
from common.observability.logging import (
    bind_log_context,
    get_log_context,
    safe_exception_metadata,
)

_LOGGER_NAME = "tests.catalog_emit"


def _catalog(*, strict: bool = False) -> Catalog:
    catalog = Catalog()
    catalog.strict = strict
    return catalog


def _event_logger(catalog: Catalog) -> EventLogger:
    return EventLogger(
        logging.getLogger(_LOGGER_NAME),
        catalog,
        exception_fields=safe_exception_metadata,
        context=get_log_context,
    )


def _line_after() -> int:
    """The caller's next line number."""
    return sys._getframe(1).f_lineno + 1


def _records(caplog) -> list[logging.LogRecord]:
    return [record for record in caplog.records if record.name == _LOGGER_NAME]


def test_define_rejects_duplicate_and_malformed_names():
    catalog = _catalog()
    catalog.define("room_deleted", logging.INFO)

    with pytest.raises(ValueError, match="duplicate"):
        catalog.define("room_deleted", logging.INFO)
    with pytest.raises(ValueError, match="name"):
        catalog.define("Room Deleted", logging.INFO)


@pytest.mark.parametrize("field", ["module", "message", "version", "event"])
def test_define_rejects_reserved_required_fields(field):
    with pytest.raises(ValueError, match="reserved"):
        _catalog().define("room_deleted", logging.INFO, required={field})


def test_reserved_name_sets_cover_record_attributes_and_fixed_fields():
    assert {"name", "module", "msg", "args", "message", "asctime"} <= (
        RESERVED_RECORD_FIELDS
    )
    assert {"timestamp", "level", "version", "event", "logger"} <= FIXED_FIELDS


def test_emit_uses_spec_level_name_and_marks_the_record(caplog):
    catalog = _catalog()
    spec = catalog.define("room_deleted", logging.WARNING, required={"deleted"})

    with caplog.at_level(logging.DEBUG, logger=_LOGGER_NAME):
        _event_logger(catalog).emit(spec, deleted=3)

    [record] = _records(caplog)
    assert record.levelno == logging.WARNING
    assert record.getMessage() == "room_deleted"
    assert record.deleted == 3
    assert record._hybro_event is True
    assert not hasattr(record, "catalog_violation")


def test_emit_level_override_is_honoured(caplog):
    catalog = _catalog()
    spec = catalog.define("llm_call_completed", logging.INFO)

    with caplog.at_level(logging.DEBUG, logger=_LOGGER_NAME):
        _event_logger(catalog).emit(spec, level=logging.ERROR)

    assert _records(caplog)[0].levelno == logging.ERROR


def test_required_fields_are_satisfied_by_bound_context(caplog):
    catalog = _catalog(strict=True)
    spec = catalog.define("room_deleted", logging.INFO, required={"conversation_id"})

    with caplog.at_level(logging.DEBUG, logger=_LOGGER_NAME):
        with bind_log_context(conversation_id="room-1"):
            _event_logger(catalog).emit(spec)

    assert not hasattr(_records(caplog)[0], "catalog_violation")


def test_required_field_present_as_none_counts_as_present(caplog):
    catalog = _catalog(strict=True)
    spec = catalog.define("room_deleted", logging.INFO, required={"deleted"})

    with caplog.at_level(logging.DEBUG, logger=_LOGGER_NAME):
        _event_logger(catalog).emit(spec, deleted=None)

    assert _records(caplog)[0].deleted is None


def test_missing_fields_annotate_in_normal_mode_and_raise_in_strict_mode(caplog):
    catalog = _catalog()
    spec = catalog.define("room_deleted", logging.INFO, required={"a", "b"})

    with caplog.at_level(logging.DEBUG, logger=_LOGGER_NAME):
        _event_logger(catalog).emit(spec, a=1)

    [record] = _records(caplog)
    assert record.catalog_violation == "missing_fields"
    assert record.missing_fields == ["b"]

    catalog.strict = True
    with pytest.raises(CatalogViolation, match="missing_fields"):
        _event_logger(catalog).emit(spec, a=1)


def test_unknown_spec_annotates_in_normal_mode_and_raises_in_strict_mode(caplog):
    foreign = _catalog().define("foreign_event", logging.INFO)
    catalog = _catalog()

    with caplog.at_level(logging.DEBUG, logger=_LOGGER_NAME):
        _event_logger(catalog).emit(foreign)

    [record] = _records(caplog)
    assert record.getMessage() == "foreign_event"
    assert record.catalog_violation == "unknown_event"

    catalog.strict = True
    with pytest.raises(CatalogViolation, match="unknown_event"):
        _event_logger(catalog).emit(foreign)


def test_reserved_and_fixed_keys_are_renamed(caplog):
    catalog = _catalog()
    spec = catalog.define("room_deleted", logging.INFO)

    with caplog.at_level(logging.DEBUG, logger=_LOGGER_NAME):
        _event_logger(catalog).emit(spec, module="rooms", version="v2")

    [record] = _records(caplog)
    assert record.field_module == "rooms"
    assert record.field_version == "v2"
    assert record.module != "rooms"
    assert record.catalog_violation == "reserved_field"
    assert record.reserved_fields == ["module", "version"]

    catalog.strict = True
    with pytest.raises(CatalogViolation, match="reserved_field"):
        _event_logger(catalog).emit(spec, module="rooms")


def test_exc_puts_error_fields_on_the_record_without_exc_info(caplog):
    catalog = _catalog()
    spec = catalog.define("room_delete_failed", logging.ERROR)

    try:
        raise RuntimeError("PRIVATE")
    except RuntimeError as exc:
        with caplog.at_level(logging.DEBUG, logger=_LOGGER_NAME):
            _event_logger(catalog).emit(spec, exc=exc)

    [record] = _records(caplog)
    assert record.error_type == "RuntimeError"
    assert len(record.error_fingerprint) == 16
    assert record.exc_info is None


def test_emit_reports_the_caller_as_source(caplog):
    catalog = _catalog()
    spec = catalog.define("room_deleted", logging.INFO)
    event_logger = _event_logger(catalog)

    with caplog.at_level(logging.DEBUG, logger=_LOGGER_NAME):
        emit_line = _line_after()
        event_logger.emit(spec)

    [emitted] = _records(caplog)
    assert emitted.pathname == __file__
    assert emitted.lineno == emit_line
    assert emitted.funcName == "test_emit_reports_the_caller_as_source"


@pytest.mark.parametrize(
    "method", ["debug", "info", "warning", "error", "exception", "critical", "log"]
)
def test_event_logger_has_no_free_text_methods(method):
    assert not hasattr(_event_logger(_catalog()), method)

"""Container wiring of the diagnostics store."""

import inspect
import re

import pytest

import container
from common.observability.logging import StructuredFormatter
from common.observability.sinks import RingBufferSink, SinkHandler
from diagnostics.store_sink import StoreSink


class _Repository:
    async def insert(self, collection, documents):
        del collection, documents


@pytest.mark.asyncio
async def test_store_is_not_started_when_not_ready():
    assert container.start_diagnostics_store(_Repository(), ready=False) is None


@pytest.mark.asyncio
async def test_store_is_not_started_without_capture(monkeypatch):
    monkeypatch.setattr(container, "log_sinks", lambda: None)

    assert container.start_diagnostics_store(_Repository(), ready=True) is None


@pytest.mark.asyncio
async def test_store_starts_on_the_sink_handler(monkeypatch):
    handler = SinkHandler(StructuredFormatter(output_format="json", environment="t"))
    ring = RingBufferSink()
    monkeypatch.setattr(container, "log_sinks", lambda: handler)
    monkeypatch.setattr(container, "ring_buffer", lambda: ring)

    sink = container.start_diagnostics_store(_Repository(), ready=True)
    try:
        assert isinstance(sink, StoreSink)
        assert sink.stats()["ready"] is True
        assert sink.accept in handler._sinks
    finally:
        await sink.stop()


def test_lifespan_stops_the_store_before_closing_mongo():
    source = inspect.getsource(container._runtime_lifespan)

    mongo_steps = [m.start() for m in re.finditer(r'\("mongo"', source)]
    store_steps = [m.start() for m in re.finditer(r'\("diagnostics-store"', source)]
    assert len(mongo_steps) == 2
    assert len(store_steps) == 2
    for store, mongo in zip(store_steps, mongo_steps, strict=True):
        assert store < mongo
        guard = source.rfind("if _diagnostics_sink is not None", 0, store)
        assert guard != -1 and source.count("\n", guard, store) <= 1

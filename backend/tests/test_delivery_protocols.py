import ast
import asyncio
import importlib
import inspect
import tomllib
from dataclasses import fields
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

FORBIDDEN_DELIVERY_ROOTS = {
    "a2a_adapter",
    "agent",
    "api",
    "config",
    "container",
    "context_memory",
    "dal",
    "database",
    "execution",
    "hub_runtime_bridge",
    "infrastructure",
    "jobs",
    "llm_gateway",
    "main",
    "models",
    "modules",
    "platform_module",
    "chat",
    "conversation",
    "services",
}

PRODUCTION_REVERSE_IMPORT_ROOTS = [
    Path("main.py"),
    Path("a2a_adapter"),
    Path("agent"),
    Path("common"),
    Path("config"),
    Path("context_memory"),
    Path("dal"),
    Path("database"),
    Path("execution"),
    Path("hub_runtime_bridge"),
    Path("infrastructure"),
    Path("jobs"),
    Path("llm_gateway"),
    Path("models"),
    Path("modules"),
    Path("platform_module"),
    Path("chat"),
    Path("conversation"),
    Path("services"),
    Path("api_gateway"),
]


def _python_files(path: Path) -> list[Path]:
    if path.is_file():
        return [path]
    if not path.exists():
        return []
    return sorted(p for p in path.rglob("*.py") if "__pycache__" not in p.parts)


def _static_string(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _attribute_chain(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _attribute_chain(node.value)
        if base:
            return f"{base}.{node.attr}"
    return None


def test_delivery_package_skeleton_and_config_exports():
    delivery = importlib.import_module("delivery")

    from delivery.config import DeliveryConfig
    from delivery.types import RoomSubscriptionLimitExceeded, TaskRunner

    if getattr(delivery, "__all__", []) != []:
        assert delivery.__all__ == [
            "DeliveryFacade",
            "EventPublisherImpl",
            "SSETransportImpl",
        ]

    config = DeliveryConfig()
    assert config.heartbeat_interval_seconds == 30.0
    assert config.sse_connection_queue_maxsize == 100
    assert config.shutdown_drain_seconds == 5.0
    assert config.terminal_reservation_ttl_seconds == 30
    assert config.terminal_redis_io_timeout_seconds == 1.0
    assert config.redis_sse_channel_prefix == "sse:room:"
    assert config.redis_dead_letter_channel == "delivery:dead_letter"
    assert config.redis_room_subscription_production_limit == 40
    assert config.redis_room_subscription_ready_timeout_seconds == 5.0
    assert config.redis_subscription_reserved_connections == 10
    assert config.redis_max_connections == 50
    assert config.terminal_processing_statuses == frozenset(
        {
            "completed",
            "failed",
            "canceled",
            "rejected",
            "expired",
            "rate_limited",
            "error",
        }
    )

    assert issubclass(RoomSubscriptionLimitExceeded, RuntimeError)
    assert callable(TaskRunner)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("heartbeat_interval_seconds", 0),
        ("sse_connection_queue_maxsize", 0),
        ("shutdown_drain_seconds", 0),
        ("terminal_dedup_ttl_seconds", 0),
        ("terminal_reservation_ttl_seconds", 0),
        ("terminal_redis_io_timeout_seconds", 0),
        ("terminal_dedup_cache_maxsize", 0),
        ("dead_letter_memory_maxlen", 0),
        ("redis_reconnect_delay", 0),
        ("redis_reconnect_max_delay", 0),
        ("redis_max_connections", 0),
        ("redis_subscription_reserved_connections", 0),
        ("redis_room_subscription_production_limit", 0),
        ("redis_room_subscription_ready_timeout_seconds", 0),
        ("redis_sse_channel_prefix", ""),
        ("redis_dead_letter_channel", ""),
        ("redis_terminal_key_prefix", ""),
    ],
)
def test_delivery_config_rejects_invalid_values(field, value):
    from delivery.config import DeliveryConfig

    with pytest.raises(ValueError):
        DeliveryConfig(**{field: value})


@pytest.mark.parametrize(
    "kwargs",
    [
        {"redis_reconnect_delay": 2.0, "redis_reconnect_max_delay": 1.0},
        {
            "redis_max_connections": 50,
            "redis_subscription_reserved_connections": 10,
            "redis_room_subscription_production_limit": 100,
        },
        {"terminal_processing_statuses": "failed,completed"},
        {"terminal_processing_statuses": frozenset()},
        {"terminal_processing_statuses": frozenset({" "})},
        {"terminal_processing_statuses": frozenset({"failed", 1})},
    ],
)
def test_delivery_config_rejects_invalid_combinations(kwargs):
    from delivery.config import DeliveryConfig

    with pytest.raises(ValueError):
        DeliveryConfig(**kwargs)


def test_delivery_config_normalizes_terminal_statuses():
    from delivery.config import DeliveryConfig

    config = DeliveryConfig(terminal_processing_statuses={" Completed ", "FAILED"})

    assert config.terminal_processing_statuses == frozenset({"completed", "failed"})


def test_common_settings_expose_delivery_config_fields():
    from common.config.loader import Settings
    from delivery.config import DeliveryConfig

    settings = Settings()
    values = {
        field: getattr(settings, field) for field in DeliveryConfig.__dataclass_fields__
    }

    assert DeliveryConfig(**values) == DeliveryConfig()

    custom = Settings(
        terminal_processing_statuses="Done, FAILED",
    )
    assert custom.terminal_processing_statuses == frozenset({"done", "failed"})


def test_delivery_packages_are_registered():
    pyproject = tomllib.loads(Path("pyproject.toml").read_text())
    packages = set(pyproject["tool"]["setuptools"]["packages"])

    assert {"delivery", "delivery.sse", "delivery.event_bus"}.issubset(packages)


def test_sse_transport_connect_is_sync_async_iterator_factory():
    from delivery.types import SSETransport

    assert inspect.iscoroutinefunction(SSETransport.connect) is False


def test_delivery_facade_exports_and_protocol_conformance():
    import delivery
    from common.protocols import EventPublisher
    from delivery import DeliveryFacade, EventPublisherImpl, SSETransportImpl
    from delivery.types import SSETransport

    assert delivery.__all__ == [
        "DeliveryFacade",
        "EventPublisherImpl",
        "SSETransportImpl",
    ]
    assert isinstance(EventPublisherImpl, type)
    assert isinstance(SSETransportImpl, type)
    assert DeliveryFacade is delivery.DeliveryFacade
    assert isinstance(EventPublisherImpl.__new__(EventPublisherImpl), EventPublisher)
    assert isinstance(SSETransportImpl.__new__(SSETransportImpl), SSETransport)


@pytest.mark.asyncio
async def test_facade_treats_confirmed_stable_task_delivery_as_success():
    from common.dto import DeliveryEmitStatus
    from delivery.config import DeliveryConfig
    from delivery.facade import DeliveryFacade

    facade = DeliveryFacade.__new__(DeliveryFacade)
    facade._event_publisher = SimpleNamespace(
        emit_checked=AsyncMock(return_value=DeliveryEmitStatus.ALREADY_DELIVERED)
    )
    facade.config = DeliveryConfig()
    facade._record_terminal_delivery = MagicMock()

    assert await facade.send_task_update(
        conversation_id="room-1",
        message_id="sys-msg-1",
        status="completed",
        delivery_id="terminal:event-1:system-task",
    )
    event = facade._event_publisher.emit_checked.await_args.args[0]
    assert event.delivery_id == "terminal:event-1:system-task"


def test_tracing_helpers_preserve_explicit_trace_context():
    from common.observability import (
        get_current_trace_id,
        trace_id_context,
        traced_create_task,
    )

    async def read_trace_id():
        await asyncio.sleep(0)
        return get_current_trace_id()

    async def run():
        assert get_current_trace_id() is None
        with trace_id_context("trace-123"):
            task = traced_create_task(read_trace_id(), name="delivery-test-task")
            assert task.get_name() == "delivery-test-task"
            assert await task == "trace-123"
        assert get_current_trace_id() is None

    asyncio.run(run())


def test_delivery_import_boundary():
    delivery_root = Path("delivery")
    assert delivery_root.exists()

    for path in _python_files(delivery_root):
        tree = ast.parse(path.read_text(), filename=str(path))
        imported_common_config_aliases: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root = alias.name.split(".")[0]
                    assert root not in FORBIDDEN_DELIVERY_ROOTS, path
                    if alias.name in {"common.config", "common.config.loader"}:
                        imported_common_config_aliases.add(alias.asname or alias.name)
            elif isinstance(node, ast.ImportFrom) and node.module:
                root = node.module.split(".")[0]
                assert root not in FORBIDDEN_DELIVERY_ROOTS, path
                if node.module in {"common.config", "common.config.loader"}:
                    imported = {alias.name for alias in node.names}
                    assert "settings" not in imported, path
            elif isinstance(node, ast.Call):
                call_name = _attribute_chain(node.func)
                if call_name in {"importlib.import_module", "__import__"} and node.args:
                    target = _static_string(node.args[0])
                    if target:
                        root = target.split(".")[0]
                        assert root not in FORBIDDEN_DELIVERY_ROOTS, path
                        assert target not in {
                            "common.config",
                            "common.config.loader",
                        }, path
            elif isinstance(node, ast.Attribute) and node.attr == "settings":
                chain = _attribute_chain(node.value)
                assert chain not in imported_common_config_aliases, path
                assert chain not in {"common.config", "common.config.loader"}, path


def test_business_modules_do_not_import_delivery_concretes():
    for root in PRODUCTION_REVERSE_IMPORT_ROOTS:
        for path in _python_files(root):
            if path == Path("container.py"):
                continue
            tree = ast.parse(path.read_text(), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported = {alias.name.split(".")[0] for alias in node.names}
                    assert "delivery" not in imported, path
                elif isinstance(node, ast.ImportFrom) and node.module:
                    assert node.module.split(".")[0] != "delivery", path
                elif isinstance(node, ast.Call):
                    call_name = _attribute_chain(node.func)
                    if (
                        call_name in {"importlib.import_module", "__import__"}
                        and node.args
                    ):
                        target = _static_string(node.args[0])
                        assert not (target and target.split(".")[0] == "delivery"), path


def test_delivery_background_tasks_use_traced_task_runner():
    for path in _python_files(Path("delivery")):
        tree = ast.parse(path.read_text(), filename=str(path))
        asyncio_aliases = {"asyncio"}
        create_task_names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "asyncio":
                        asyncio_aliases.add(alias.asname or alias.name)
            elif isinstance(node, ast.ImportFrom) and node.module == "asyncio":
                for alias in node.names:
                    if alias.name == "create_task":
                        create_task_names.add(alias.asname or alias.name)

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Name):
                assert node.func.id not in create_task_names, path
            if isinstance(node.func, ast.Attribute) and node.func.attr == "create_task":
                owner = _attribute_chain(node.func.value)
                assert owner not in asyncio_aliases, path
                assert owner != "loop", path


class _LifecycleComponent:
    def __init__(self, name: str, calls: list[str]):
        self.name = name
        self.calls = calls
        self.started = False
        self.stopped = False

    async def start(self):
        self.calls.append(f"{self.name}.start")
        self.started = True

    async def stop(self):
        self.calls.append(f"{self.name}.stop")
        self.stopped = True


class _FakeTransport(_LifecycleComponent):
    def __init__(self, calls: list[str]):
        super().__init__("transport", calls)
        self.closed = False
        self.draining = False
        self.compat_calls: list[tuple] = []

    async def start_cancellation_watcher(self):
        self.calls.append("transport.start_cancellation_watcher")

    async def stop_cancellation_watcher(self):
        self.calls.append("transport.stop_cancellation_watcher")

    async def close_all_connections(self):
        self.calls.append("transport.close_all_connections")
        self.closed = True

    def set_draining(self, draining: bool):
        self.draining = draining

    async def open_connection(self, conversation_id: str):
        self.compat_calls.append(("open_connection", conversation_id))
        return "connection"

    async def remove_connection(self, conversation_id: str, connection_id: str):
        self.compat_calls.append(("remove_connection", conversation_id, connection_id))

    def get_room_status(self, conversation_id: str):
        return {"room_id": conversation_id}


class _FakePublisher(_LifecycleComponent):
    def __init__(self, calls: list[str]):
        super().__init__("publisher", calls)


class _FakeBus(_LifecycleComponent):
    def __init__(self, calls: list[str], connected=True):
        super().__init__("bus", calls)
        self.connected = connected
        self.health_refreshed = False

    @property
    def is_connected(self):
        return self.connected

    async def refresh_health(self):
        self.health_refreshed = True


class _FakeRedisKV:
    def __init__(self, result=True):
        self.result = result
        self.closed = 0

    async def ping(self):
        return self.result

    async def close(self):
        self.closed += 1


def _make_facade(redis_kv=None, bus_connected=True):
    from delivery.config import DeliveryConfig
    from delivery.facade import DeliveryFacade

    calls: list[str] = []
    publisher = _FakePublisher(calls)
    transport = _FakeTransport(calls)
    bus = _FakeBus(calls, connected=bus_connected)
    facade = DeliveryFacade(
        event_publisher=publisher,
        sse_transport=transport,
        event_bus=bus,
        redis_kv=redis_kv,
        config=DeliveryConfig(),
        instance_id="worker-1",
    )
    return facade, publisher, transport, bus, calls


@pytest.mark.asyncio
async def test_delivery_facade_lifecycle_and_health():
    redis_kv = _FakeRedisKV(result=True)
    facade, publisher, transport, bus, calls = _make_facade(redis_kv=redis_kv)

    await facade.start()
    assert calls == ["bus.start"]
    assert facade.instance_id == "worker-1"
    assert facade.delivery_kv_connected is True
    assert facade.delivery_pubsub_connected is True
    assert facade.redis_connected is True
    assert facade.broker_connected is True

    facade.set_draining(True)
    assert transport.draining is True

    await facade.stop()
    assert calls[-2:] == [
        "transport.close_all_connections",
        "bus.stop",
    ]
    assert redis_kv.closed == 1


@pytest.mark.asyncio
async def test_delivery_facade_refresh_health_handles_ping_failure():
    redis_kv = _FakeRedisKV(result=False)
    facade, _, _, bus, _ = _make_facade(redis_kv=redis_kv, bus_connected=False)

    await facade.refresh_health()

    assert bus.health_refreshed is True
    assert facade.delivery_kv_connected is False
    assert facade.delivery_pubsub_connected is False


def test_container_delivery_factories_and_config_mapping():
    from common.protocols import EventPublisher
    from container import (
        DeliveryDeps,
        create_delivery_config,
        create_delivery_deps,
        create_delivery_facade,
        create_delivery_redis_clients,
    )
    from delivery.config import DeliveryConfig
    from delivery.facade import DeliveryFacade
    from delivery.types import SSETransport

    values = {
        field: getattr(DeliveryConfig(), field)
        for field in DeliveryConfig.__dataclass_fields__
    }
    values.update(
        {
            "heartbeat_interval_seconds": 2.5,
            "sse_connection_queue_maxsize": 23,
            "shutdown_drain_seconds": 1.25,
            "terminal_dedup_ttl_seconds": 43,
            "terminal_reservation_ttl_seconds": 4,
            "terminal_redis_io_timeout_seconds": 0.2,
            "terminal_dedup_cache_maxsize": 46,
            "redis_sse_channel_prefix": "custom:sse:",
            "redis_dead_letter_channel": "custom:dead",
            "redis_terminal_key_prefix": "custom:terminal:",
            "dead_letter_memory_maxlen": 47,
            "redis_reconnect_delay": 0.25,
            "redis_reconnect_max_delay": 3.0,
            "redis_max_connections": 120,
            "redis_subscription_reserved_connections": 10,
            "redis_room_subscription_production_limit": 100,
            "redis_room_subscription_ready_timeout_seconds": 0.75,
            "terminal_processing_statuses": "Done, FAILED",
        }
    )
    config = create_delivery_config(SimpleNamespace(**values))

    assert config.heartbeat_interval_seconds == 2.5
    assert config.sse_connection_queue_maxsize == 23
    assert config.terminal_reservation_ttl_seconds == 4
    assert config.terminal_redis_io_timeout_seconds == 0.2
    assert config.redis_sse_channel_prefix == "custom:sse:"
    assert config.redis_dead_letter_channel == "custom:dead"
    assert config.redis_max_connections == 120
    assert config.redis_room_subscription_production_limit == 100
    assert config.redis_room_subscription_ready_timeout_seconds == 0.75
    assert config.terminal_processing_statuses == frozenset({"done", "failed"})

    legacy_values = dict(values)
    legacy_values.pop("terminal_redis_io_timeout_seconds")
    legacy_config = create_delivery_config(SimpleNamespace(**legacy_values))
    assert legacy_config.terminal_redis_io_timeout_seconds == 1.0

    redis_kv, redis_pubsub = create_delivery_redis_clients(
        redis_url="redis://localhost:6379/0",
        config=config,
    )
    assert redis_kv is not None
    assert redis_pubsub is not None
    assert redis_pubsub.max_connections == 120
    assert create_delivery_redis_clients(redis_url="", config=config) == (None, None)

    facade = create_delivery_facade(
        redis_kv=None,
        redis_pubsub=None,
        config=DeliveryConfig(),
        instance_id="worker-1",
        id_factory=lambda: "conn-1",
    )
    deps = create_delivery_deps(facade)

    assert isinstance(facade, DeliveryFacade)
    assert facade.event_publisher.single_instance is True
    assert (
        create_delivery_facade(
            redis_kv=redis_kv, redis_pubsub=redis_pubsub, instance_id="worker-1"
        ).event_publisher.single_instance
        is False
    )
    assert isinstance(deps, DeliveryDeps)
    assert [field.name for field in fields(DeliveryDeps)] == [
        "event_publisher",
        "sse_transport",
    ]
    assert deps.event_publisher is facade.event_publisher
    assert deps.sse_transport is facade.sse_transport
    assert isinstance(deps.event_publisher, EventPublisher)
    assert isinstance(deps.sse_transport, SSETransport)


def test_main_does_not_import_or_instantiate_concrete_dal():
    forbidden_names = {
        "MongoDALImpl",
        "RedisKVImpl",
        "RedisPubSubImpl",
    }
    tree = ast.parse(Path("main.py").read_text(), filename="main.py")
    invalid_collection_calls: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name.split(".")[0] != "dal"
        elif isinstance(node, ast.ImportFrom) and node.module:
            assert node.module.split(".")[0] != "dal"
        elif isinstance(node, ast.Name):
            assert node.id not in forbidden_names
            assert not node.id.endswith("DALImpl")
        elif isinstance(node, ast.Attribute):
            assert node.attr != "cancelled_messages_collection"
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "collection"
        ):
            if not (
                isinstance(node.func.value, ast.Name)
                and node.func.value.id == "mongo_dal"
            ):
                invalid_collection_calls.append(f"line {node.lineno}")

    assert not invalid_collection_calls


def test_main_does_not_construct_legacy_sse_broker():
    tree = ast.parse(Path("main.py").read_text(), filename="main.py")
    forbidden_names = {"create_event_broker", "RedisBroker"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            assert node.module.split(".", 1)[0] != "infrastructure"
        elif isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name.split(".", 1)[0] != "infrastructure"
        elif isinstance(node, ast.Name):
            assert node.id not in forbidden_names

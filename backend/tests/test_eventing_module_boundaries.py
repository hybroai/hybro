import ast
from pathlib import Path

from common.eventing import InternalEventBus, InternalEventPublisher

BACKEND = Path(__file__).resolve().parents[1]


def test_focused_eventing_protocol_method_lists():
    publisher_methods = {
        name for name in dir(InternalEventPublisher) if not name.startswith("_")
    }
    bus_methods = {name for name in dir(InternalEventBus) if not name.startswith("_")}
    assert publisher_methods == {"publish"}
    assert bus_methods == {
        "is_connected",
        "publish",
        "refresh_health",
        "register_handler",
        "start",
        "stop",
    }


def test_common_eventing_does_not_depend_on_delivery_or_sse_ownership_types():
    forbidden_names = {"DeliveryEvent", "Room", "CancellationToken"}
    for path in (BACKEND / "common" / "eventing").glob("*.py"):
        tree = ast.parse(path.read_text())
        imports = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        imported_roots = {
            alias.name.split(".", 1)[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
        assert not any(
            module == "delivery" or module.startswith("delivery.") for module in imports
        )
        assert "delivery" not in imported_roots
        assert not (names & forbidden_names)
        assert "SSE" not in path.read_text()


def test_delivery_cross_instance_bus_has_no_internal_event_api():
    module = __import__(
        "delivery.event_bus.cross_instance",
        fromlist=["CrossInstanceEventBus"],
    )
    public = {
        name for name in dir(module.CrossInstanceEventBus) if not name.startswith("_")
    }
    assert (
        not {
            "handle_internal_message",
            "publish_internal",
            "set_internal_callback",
        }
        & public
    )


def test_production_has_no_retired_internal_delivery_publisher_calls():
    retired = ("emit_internal", "register_internal_handler")
    offenders = []
    for path in BACKEND.rglob("*.py"):
        if {"tests", "build"} & set(path.relative_to(BACKEND).parts):
            continue
        text = path.read_text()
        for name in retired:
            if name in text:
                offenders.append(f"{path.relative_to(BACKEND)}: {name}")
    assert offenders == []


def test_redis_internal_eventing_concrete_lives_only_in_dal_redis():
    matches = []
    for path in BACKEND.rglob("*.py"):
        if {"tests", "build"} & set(path.relative_to(BACKEND).parts):
            continue
        if "class RedisInternalEventTransport" in path.read_text():
            matches.append(path.relative_to(BACKEND).as_posix())
    assert matches == ["dal/redis/internal_eventing.py"]

import inspect
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import Request
from fastapi.params import Depends as DependsParam


def _deps(**overrides):
    from api_gateway.dependencies import APIGatewayDeps

    values = {
        "task_store": MagicMock(),
        "agent_center": MagicMock(),
        "agent_service": MagicMock(),
        "capability_issue_service": MagicMock(),
        "agent_liveness_checker": AsyncMock(),
        "agent_group_store": MagicMock(),
        "api_key_store": MagicMock(),
        "discovery_service": MagicMock(),
        "discovery_rate_limiter": MagicMock(),
        "discovery_default_limit": 10,
        "file_storage": MagicMock(),
        "room_ownership_reader": MagicMock(),
        "hitl_manager": MagicMock(),
        "inspection_center": MagicMock(),
        "gateway_service": MagicMock(),
        "gateway_rate_limiter": MagicMock(),
        "room_center": MagicMock(),
        "room_store": MagicMock(),
        "agent_selection_service": MagicMock(),
        "execution_engine": MagicMock(),
        "sse_store": MagicMock(),
        "sse_transport": MagicMock(),
    }
    values.update(overrides)
    return APIGatewayDeps(**values)


def _request_with_state(**state_values):
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [],
        "app": SimpleNamespace(state=SimpleNamespace(**state_values)),
    }
    return Request(scope)


PROVIDER_FIELD_NAMES = [
    ("get_task_store", "task_store"),
    ("get_agent_center", "agent_center"),
    ("get_agent_service", "agent_service"),
    ("get_capability_issue_service", "capability_issue_service"),
    ("get_agent_liveness_checker", "agent_liveness_checker"),
    ("get_agent_group_store", "agent_group_store"),
    ("get_api_key_store", "api_key_store"),
    ("get_discovery_service", "discovery_service"),
    ("get_discovery_rate_limiter", "discovery_rate_limiter"),
    ("get_discovery_default_limit", "discovery_default_limit"),
    ("get_file_storage", "file_storage"),
    ("get_room_ownership_reader", "room_ownership_reader"),
    ("get_hitl_manager", "hitl_manager"),
    ("get_inspection_center", "inspection_center"),
    ("get_gateway_service", "gateway_service"),
    ("get_gateway_rate_limiter", "gateway_rate_limiter"),
    ("get_room_center", "room_center"),
    ("get_room_store", "room_store"),
    ("get_agent_selection_service", "agent_selection_service"),
    ("get_execution_engine", "execution_engine"),
    ("get_sse_store", "sse_store"),
    ("get_sse_transport", "sse_transport"),
]


def test_api_gateway_deps_report_missing_required_fields():
    from api_gateway.dependencies import missing_required_deps

    deps = _deps(file_storage=None, execution_engine=None)

    assert missing_required_deps(deps) == ["file_storage", "execution_engine"]


def test_api_gateway_deps_report_missing_app_state_binding():
    from api_gateway.dependencies import missing_required_deps

    assert missing_required_deps(None) == ["app.state.api_gateway_deps"]


def test_bind_api_gateway_deps_rejects_incomplete_bindings():
    from api_gateway.dependencies import bind_api_gateway_deps

    app = SimpleNamespace(state=SimpleNamespace())
    deps = _deps(room_center=None)

    with pytest.raises(RuntimeError, match="room_center"):
        bind_api_gateway_deps(app, deps)
    assert not hasattr(app.state, "api_gateway_deps")


def test_bind_api_gateway_deps_stores_deps_on_app_state():
    from api_gateway.dependencies import bind_api_gateway_deps

    app = SimpleNamespace(state=SimpleNamespace())
    deps = _deps()

    bind_api_gateway_deps(app, deps)

    assert app.state.api_gateway_deps is deps


def test_get_api_gateway_deps_reads_request_app_state():
    from api_gateway.dependencies import get_api_gateway_deps

    deps = _deps()
    request = _request_with_state(api_gateway_deps=deps)

    assert get_api_gateway_deps(request) is deps


def test_api_gateway_dependencies_are_app_state_injected_not_route_global():
    from api_gateway.dependencies import bind_api_gateway_deps, get_api_gateway_deps

    app = SimpleNamespace(state=SimpleNamespace())
    deps = _deps()

    bind_api_gateway_deps(app, deps)
    request = _request_with_state(api_gateway_deps=app.state.api_gateway_deps)

    assert get_api_gateway_deps(request) is deps

    source = Path("api_gateway/dependencies.py").read_text()
    assert "app.state.api_gateway_deps = deps" in source
    assert 'getattr(request.app.state, "api_gateway_deps", None)' in source


def test_get_api_gateway_deps_fails_when_startup_did_not_bind():
    from api_gateway.dependencies import get_api_gateway_deps

    request = _request_with_state()

    with pytest.raises(RuntimeError, match="APIGatewayDeps not bound"):
        get_api_gateway_deps(request)


@pytest.mark.parametrize(("provider_name", "field_name"), PROVIDER_FIELD_NAMES)
def test_named_providers_read_expected_fields(provider_name, field_name):
    from api_gateway import dependencies as gateway_deps

    deps = _deps()
    provider = getattr(gateway_deps, provider_name)

    assert provider(deps) is getattr(deps, field_name)


@pytest.mark.parametrize(("provider_name", "_field_name"), PROVIDER_FIELD_NAMES)
def test_named_providers_are_fastapi_dependency_providers(provider_name, _field_name):
    from api_gateway import dependencies as gateway_deps

    provider = getattr(gateway_deps, provider_name)
    parameter = inspect.signature(provider).parameters["deps"]

    assert isinstance(parameter.default, DependsParam)
    assert parameter.default.dependency is gateway_deps.get_api_gateway_deps

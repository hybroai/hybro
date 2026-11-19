"""The agent capability-issue routes are deprecated and will be removed."""

import pytest

DEPRECATED_ROUTES = [
    ("/api/v1/agent/{agent_id}/capability-issues", "get"),
    ("/api/v1/agent/{agent_id}/capability-issues/resolve-all", "post"),
    ("/api/v1/agent/capability-issues/{issue_id}/resolve", "post"),
]


@pytest.fixture(scope="module")
def openapi_paths():
    from main import app

    return app.openapi()["paths"]


@pytest.mark.parametrize(("path", "method"), DEPRECATED_ROUTES)
def test_capability_issue_route_is_deprecated_in_openapi(openapi_paths, path, method):
    assert openapi_paths[path][method].get("deprecated") is True


def test_other_agent_routes_are_not_deprecated(openapi_paths):
    deprecated = {
        (path, method)
        for path, operations in openapi_paths.items()
        if path.startswith("/api/v1/agent/")
        for method, operation in operations.items()
        if operation.get("deprecated")
    }
    assert deprecated == set(DEPRECATED_ROUTES)

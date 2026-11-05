"""The read-only diagnostics routes: authorisation, validation and errors."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api_gateway.routes.diagnostics_routes import router
from common.auth import ClerkUser, get_optional_user
from diagnostics.models import (
    DiagnosticPage,
    DiagnosticRecord,
    DiagnosticsSnapshot,
    RecordQuery,
    ServiceSection,
)
from diagnostics.protocols import InvalidCursor, StoreUnavailable

TOKEN = "a" * 64
HEADER = "X-Hybro-Diagnostics-Token"
T0 = datetime(2030, 1, 1, tzinfo=UTC)
PATHS = ("/api/v1/diagnostics/snapshot", "/api/v1/diagnostics/events")
LOGS = "/api/v1/diagnostics/logs"


class _Access:
    def __init__(self) -> None:
        self.error: Exception | None = None
        self.queries: list[tuple[str, RecordQuery, str | None]] = []

    def can_read(self, user_id: str) -> bool:
        return user_id == "admin"

    def token_matches(self, value: str) -> bool:
        return value == TOKEN

    async def snapshot(self) -> DiagnosticsSnapshot:
        return DiagnosticsSnapshot(
            generated_at=T0,
            service=ServiceSection(
                version="0.4.4",
                instance_id="ab12cd34",
                environment="test",
                started_at=T0,
                uptime_seconds=1.0,
                auth_mode="clerk",
                log_level="INFO",
                log_format="json",
            ),
            errors=[],
        )

    def _page(self) -> DiagnosticPage:
        if self.error is not None:
            raise self.error
        record = DiagnosticRecord(
            id="1",
            timestamp=T0,
            level="info",
            service="hybro-backend",
            event="startup_completed",
            logger="container",
            system=True,
            instance_id="ab12cd34",
            version="0.4.4",
            trace_id=None,
            request_id=None,
            fields={"nested": {"a": [1, 2]}},
        )
        return DiagnosticPage(items=[record], next_before=None)

    async def events(self, query: RecordQuery) -> DiagnosticPage:
        self.queries.append(("events", query, None))
        return self._page()

    async def logs(
        self, query: RecordQuery, *, source: Literal["store", "memory"]
    ) -> DiagnosticPage:
        self.queries.append(("logs", query, source))
        return self._page()


def _client(
    access: _Access | None, user: str | None = None
) -> tuple[TestClient, FastAPI]:
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.state.diagnostics_access = access
    app.dependency_overrides[get_optional_user] = lambda: (
        ClerkUser(user, "session", {}) if user is not None else None
    )
    return TestClient(app), app


@pytest.mark.parametrize("path", [*PATHS, LOGS])
def test_missing_service_is_503(path):
    client, _ = _client(None, user="admin")

    response = client.get(path, headers={HEADER: TOKEN})

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "diagnostics_unavailable"


@pytest.mark.parametrize("path", [*PATHS, LOGS])
def test_matching_token_reads_without_a_user(path):
    client, _ = _client(_Access())

    response = client.get(path, headers={HEADER: TOKEN})

    assert response.status_code == 200


@pytest.mark.parametrize(
    "header",
    [b"b" * 64, "été".encode("latin-1")],
    ids=["wrong", "non-ascii"],
)
def test_wrong_token_falls_through_to_the_user_check(header):
    client, _ = _client(_Access())

    assert client.get(PATHS[0], headers={HEADER: header}).status_code == 401

    client, _ = _client(_Access(), user="admin")
    assert client.get(PATHS[0], headers={HEADER: header}).status_code == 200


@pytest.mark.parametrize("path", [*PATHS, LOGS])
def test_no_user_is_401(path):
    client, _ = _client(_Access())

    assert client.get(path).status_code == 401


@pytest.mark.parametrize("path", [*PATHS, LOGS])
def test_non_admin_is_403(path):
    client, _ = _client(_Access(), user="user")

    assert client.get(path).status_code == 403


@pytest.mark.parametrize("path", [*PATHS, LOGS])
def test_admin_reads(path):
    client, _ = _client(_Access(), user="admin")

    assert client.get(path).status_code == 200


@pytest.mark.parametrize("path", ["/api/v1/diagnostics/events", LOGS])
@pytest.mark.parametrize(
    "params",
    [
        {"limit": "0"},
        {"limit": "501"},
        {"level": "verbose"},
        {"trace_id": "bad id!"},
        {"trace_id": "x" * 129},
        {"instance_id": "a/b"},
        {"since": "2030-01-01T00:00:00"},
        {"until": "2030-01-01T00:00:00"},
        {"event": [f"e{i}" for i in range(21)]},
    ],
)
def test_invalid_parameters_are_422(path, params):
    client, _ = _client(_Access(), user="admin")

    assert client.get(path, params=params).status_code == 422


def test_invalid_source_is_422():
    client, _ = _client(_Access(), user="admin")

    assert client.get(LOGS, params={"source": "disk"}).status_code == 422


def test_parameters_reach_the_service():
    access = _Access()
    client, _ = _client(access, user="admin")

    response = client.get(
        LOGS,
        params={
            "service": "hybro-backend",
            "level": "warning",
            "event": ["a", "b"],
            "trace_id": "t-1.x:y",
            "instance_id": "ab12cd34",
            "since": "2030-01-01T00:00:00+00:00",
            "until": "2030-01-02T00:00:00Z",
            "limit": "500",
            "before": "cursor",
            "source": "memory",
        },
    )

    assert response.status_code == 200
    [(kind, query, source)] = access.queries
    assert (kind, source) == ("logs", "memory")
    assert query == RecordQuery(
        service="hybro-backend",
        level="warning",
        event=("a", "b"),
        trace_id="t-1.x:y",
        instance_id="ab12cd34",
        since=T0,
        until=datetime(2030, 1, 2, tzinfo=UTC),
        limit=500,
        before="cursor",
    )


def test_logs_default_to_the_store():
    access = _Access()
    client, _ = _client(access, user="admin")

    client.get(LOGS)

    [(_, query, source)] = access.queries
    assert source == "store"
    assert query == RecordQuery()


@pytest.mark.parametrize("path", ["/api/v1/diagnostics/events", LOGS])
def test_store_error_is_503(path):
    access = _Access()
    access.error = StoreUnavailable("down")
    client, _ = _client(access, user="admin")

    response = client.get(path)

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "store_unavailable"


@pytest.mark.parametrize("path", ["/api/v1/diagnostics/events", LOGS])
def test_invalid_cursor_is_422(path):
    access = _Access()
    access.error = InvalidCursor("bad cursor")
    client, _ = _client(access, user="admin")

    response = client.get(path, params={"before": "x"})

    assert response.status_code == 422
    assert isinstance(response.json()["detail"], str)


def test_response_bodies_validate_against_the_models():
    client, _ = _client(_Access(), user="admin")

    snapshot = client.get(PATHS[0]).json()
    page = client.get(PATHS[1]).json()

    DiagnosticsSnapshot.model_validate(snapshot)
    assert DiagnosticPage.model_validate(page).items[0].fields == {
        "nested": {"a": [1, 2]}
    }


@pytest.mark.parametrize("path", [*PATHS, LOGS])
def test_post_is_405(path):
    client, _ = _client(_Access(), user="admin")

    assert client.post(path, headers={HEADER: TOKEN}).status_code == 405


def test_openapi_does_not_advertise_the_token_header():
    _, app = _client(_Access())

    assert HEADER.lower() not in str(app.openapi()).lower()

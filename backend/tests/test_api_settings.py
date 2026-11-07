"""Settings API routes over a temporary config directory; no Provider or network."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from api_gateway.errors import settings_request_error_handler
from api_gateway.routes.settings_routes import router
from common.auth import ClerkUser, get_current_user
from common.config.loader import Settings
from common.config.runtime_config import RuntimeConfig
from common.config.runtime_store import RuntimeConfigStore
from instance_settings import InstanceSettings, SettingsRequestError

BASE = "http://fixture/api/v1"
ROUTES = (("GET", "/settings"),)


class Harness:
    def __init__(
        self,
        home: Path,
        *,
        admins: frozenset[str] | None = None,
        wired: bool = True,
    ) -> None:
        self.store = RuntimeConfigStore(home)
        self.settings = InstanceSettings(
            store=self.store,
            settings=Settings(),
            startup_config=RuntimeConfig(),
            can_configure=(lambda user: True)
            if admins is None
            else (lambda user: user in admins),
            version="9.9.9",
        )
        self.user = "user_local_developer"
        app = FastAPI()
        app.include_router(router, prefix="/api/v1")
        app.add_exception_handler(SettingsRequestError, settings_request_error_handler)
        if wired:
            app.state.instance_settings = self.settings
        app.dependency_overrides[get_current_user] = lambda: ClerkUser(
            self.user, "session", {}
        )
        self.http = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=BASE
        )

    async def aclose(self) -> None:
        await self.http.aclose()


@pytest.fixture
async def harness(tmp_path):
    harness = Harness(tmp_path / "home")
    yield harness
    await harness.aclose()


async def test_mock_mode_reads_the_settings(harness):
    response = await harness.http.get("/settings")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["revision"] == RuntimeConfig().settings_revision
    assert body["system"]["version"] == "9.9.9"
    keys = {field["key"] for field in body["fields"]}
    assert "backend.log_level" in keys
    log_level = next(f for f in body["fields"] if f["key"] == "backend.log_level")
    assert log_level["apply"] == "live"
    assert log_level["options"] == ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


async def test_clerk_non_admin_is_forbidden_on_every_route(tmp_path):
    harness = Harness(tmp_path / "home", admins=frozenset({"user_admin"}))
    try:
        harness.user = "user_other"
        for method, path in ROUTES:
            response = await harness.http.request(
                method, path, json={"revision": "x", "values": {}}
            )
            assert response.status_code == 403, path
            assert response.json()["code"] == "forbidden"
        harness.user = "user_admin"
        assert (await harness.http.get("/settings")).status_code == 200
    finally:
        await harness.aclose()


async def test_missing_service_is_unavailable(tmp_path):
    harness = Harness(tmp_path / "home", wired=False)
    try:
        response = await harness.http.get("/settings")
        assert response.status_code == 503
        assert response.json()["code"] == "settings_unavailable"
    finally:
        await harness.aclose()


def test_application_registers_settings_routes_and_error_mapping():
    from main import app

    paths = {route.path for route in app.routes}
    assert "/api/v1/settings" in paths
    assert SettingsRequestError in app.exception_handlers

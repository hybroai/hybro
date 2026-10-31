"""Browser setup API: API-key and OAuth saves, login lifecycle, authorization.

The verifier and the OAuth code exchange are local fakes; no test reaches a
model Provider or auth.openai.com. The callback listener binds a free loopback
port instead of 1455.
"""

from __future__ import annotations

import asyncio
import socket
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi import FastAPI

from api_gateway.errors import setup_request_error_handler
from api_gateway.routes.setup_routes import router
from common.auth import ClerkUser, get_current_user
from common.config.runtime_config import RuntimeConfigurationError
from common.config.runtime_store import RuntimeConfigStore
from llm_gateway import openai_oauth
from llm_gateway.catalog import model_choices
from llm_gateway.provider_runtime import ProviderRuntime
from llm_gateway.setup_api import BrowserSetup, OAuthLogins, SetupRequestError
from llm_gateway.setup_service import SetupService
from tests.fakes.llm_runtime import oauth_credential

BASE = "http://fixture/api/v1/setup"
KEY = "sk-fixture-private-key-marker"


class Verifier:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.fail = False

    async def __call__(self, config, resolved, base_url) -> None:
        self.calls.append((config.provider.id, config.models.text))
        if self.fail:
            raise RuntimeError("private-provider-marker " + KEY)


class Exchange:
    def __init__(self) -> None:
        self.codes: list[str] = []
        self.fail = False

    async def __call__(self, code: str, verifier: str):
        self.codes.append(code)
        if self.fail:
            raise RuntimeConfigurationError("OAuth token exchange/refresh failed.")
        return oauth_credential(expires_at=time.time() + 3600)


@pytest.fixture
def port(monkeypatch) -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        free = probe.getsockname()[1]
    monkeypatch.setattr(openai_oauth, "CALLBACK_PORT", free)
    return free


class Harness:
    def __init__(
        self,
        home: Path,
        *,
        auth_mode: str = "mock",
        admins: frozenset[str] = frozenset(),
        timeout: float = 300,
    ) -> None:
        self.store = RuntimeConfigStore(home)
        self.runtime = ProviderRuntime(self.store)
        self.verifier = Verifier()
        self.exchange = Exchange()
        self.logins = OAuthLogins(timeout=timeout, exchange=self.exchange)
        self.setup = BrowserSetup(
            self.runtime,
            SetupService(self.store, model_choices, self.verifier),
            self.logins,
            auth_mode=auth_mode,
            admin_user_ids=admins,
        )
        self.user = "user_local_developer"
        app = FastAPI()
        app.include_router(router, prefix="/api/v1")
        app.add_exception_handler(SetupRequestError, setup_request_error_handler)
        app.state.setup_access = self.setup
        app.dependency_overrides[get_current_user] = lambda: ClerkUser(
            self.user, "session", {}
        )
        self.http = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=BASE
        )

    async def aclose(self) -> None:
        await self.http.aclose()
        await self.setup.aclose()


@pytest.fixture
async def harness(tmp_path, port):
    harness = Harness(tmp_path / "runtime")
    yield harness
    await harness.aclose()


def api_key_body(**overrides) -> dict:
    return {
        "provider": "deepseek",
        "api_key": KEY,
        "text_model": "deepseek-v4-flash",
        **overrides,
    }


async def callback(port: int, target: str) -> bytes:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(
        f"GET {target} HTTP/1.1\r\nHost: localhost:{port}\r\n\r\n".encode("ascii")
    )
    await writer.drain()
    response = await reader.read()
    writer.close()
    await writer.wait_closed()
    return response


def oauth_state(login: dict) -> str:
    return parse_qs(urlsplit(login["authorization_url"]).query)["state"][0]


async def test_unconfigured_status_and_model_choices(harness):
    status = (await harness.http.get("/status")).json()
    assert status == {
        "state": "unconfigured",
        "provider": None,
        "auth": None,
        "text_model": None,
        "image_model": None,
        "image_size": "1024x1024",
        "last_error": None,
    }
    models = await harness.http.get(
        "/models", params={"provider": "openai", "auth": "api_key"}
    )
    assert models.json() == {
        "text": ["gpt-5-mini", "gpt-4o-mini"],
        "image": ["gpt-image-2.5-flare", "gpt-image-2.5-sunburst"],
    }
    oauth = await harness.http.get(
        "/models", params={"provider": "openai", "auth": "oauth"}
    )
    assert oauth.json()["text"][0] == "gpt-6.1-sol" and oauth.json()["image"] == []
    rejected = await harness.http.get(
        "/models", params={"provider": "deepseek", "auth": "oauth"}
    )
    assert rejected.status_code == 400
    assert rejected.json()["code"] == "invalid_selection"


async def test_api_key_save_verifies_once_saves_and_applies_live(harness):
    response = await harness.http.post("/api-key", json=api_key_body())
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["changed"] is True
    assert body["status"]["state"] == "configured"
    assert body["status"]["provider"] == "deepseek"
    assert harness.verifier.calls == [("deepseek", "deepseek-v4-flash")]
    # The saving request already sees the new pair.
    assert harness.runtime.current.state == "configured"
    assert KEY in (harness.store.home / "auth.json").read_text()
    assert KEY not in response.text
    again = await harness.http.post("/api-key", json=api_key_body())
    assert again.json()["changed"] is False


async def test_failed_verification_saves_nothing_and_hides_the_key(harness):
    harness.verifier.fail = True
    response = await harness.http.post("/api-key", json=api_key_body())
    assert response.status_code == 400
    assert response.json()["code"] == "verification_failed"
    assert KEY not in response.text and "private-provider-marker" not in response.text
    assert not (harness.store.home / "config.json").exists()
    assert harness.runtime.current.state == "unconfigured"


@pytest.mark.parametrize(
    "body",
    [
        api_key_body(text_model="gpt-4o-mini"),
        api_key_body(image_model="gpt-image-2.5-flare"),
        api_key_body(api_key=""),
        api_key_body(extra="field"),
    ],
)
async def test_invalid_api_key_requests_are_rejected_locally(harness, body):
    response = await harness.http.post("/api-key", json=body)
    assert response.status_code in {400, 422}
    assert KEY not in response.text
    assert harness.verifier.calls == []


CODE = "fixture-authorization-code-marker"


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("POST", "/api-key", api_key_body(api_key=KEY + "x" * 4096)),
        ("POST", "/api-key", {"api_key": KEY, "text_model": "deepseek-v4-flash"}),
        ("POST", "/api-key", api_key_body(provider=KEY)),
        ("POST", "/api-key", api_key_body(text_model=123, note=KEY)),
        ("POST", "/api-key", api_key_body(**{KEY: KEY})),
        ("POST", "/api-key", [KEY]),
        ("POST", "/api-key", KEY),
        (
            "POST",
            "/oauth/complete",
            {"redirect_url": f"http://localhost:1455/auth/callback?code={CODE}"},
        ),
        (
            "POST",
            "/oauth/complete",
            {"login_id": "x" * 65, "redirect_url": f"http://x/?code={CODE}"},
        ),
        (
            "POST",
            "/oauth/complete",
            {"login_id": "missing", "redirect_url": f"http://x/?code={CODE}"},
        ),
        ("POST", "/oauth/models", {"login_id": KEY, "text_model": ""}),
        ("PUT", "/image-size", {"image_size": KEY}),
        ("GET", f"/models?provider={KEY}&auth=api_key", None),
    ],
)
async def test_no_error_response_echoes_a_secret(harness, method, path, body):
    response = await harness.http.request(method, path, json=body)
    assert 400 <= response.status_code < 500
    assert KEY not in response.text and CODE not in response.text
    assert "code" in response.json()
    assert harness.verifier.calls == []


@pytest.mark.parametrize("damage", ["invalid_json", "mode"])
async def test_unreadable_auth_file_is_reported_with_a_fix(harness, damage):
    await harness.http.post("/api-key", json=api_key_body())
    auth = harness.store.home / "auth.json"
    if damage == "invalid_json":
        auth.write_text("{")
    else:
        auth.chmod(0o644)
    status = (await harness.http.get("/status")).json()
    # The last valid pair keeps serving; the status names the problem.
    assert status["state"] == "configured"
    assert status["last_error"] == "config_invalid"
    response = await harness.http.post("/api-key", json=api_key_body())
    assert response.status_code == 409
    body = response.json()
    assert body["code"] == "credential_unreadable"
    assert "mode 0600" in body["detail"] and "rerun setup" not in body["detail"]
    assert KEY not in response.text
    oauth = await harness.http.post("/oauth/models", json={"text_model": "gpt-6-sol"})
    assert oauth.json()["code"] == "credential_unreadable"


async def test_save_repairs_a_missing_credential(harness):
    await harness.http.post("/api-key", json=api_key_body())
    (harness.store.home / "auth.json").unlink()
    assert (await harness.http.get("/status")).json()["state"] == "credential_invalid"
    repaired = await harness.http.post("/api-key", json=api_key_body())
    assert repaired.json()["status"]["state"] == "configured"


async def test_oauth_listener_callback_then_model_save(harness, port):
    login = (await harness.http.post("/oauth/start")).json()
    assert login["state"] == "pending" and login["listener"] is True
    wrong = await callback(port, "/auth/callback?code=fixture-code&state=wrong")
    assert wrong.startswith(b"HTTP/1.1 400")
    target = f"/auth/callback?code=fixture-code&state={oauth_state(login)}"
    assert (await callback(port, target)).startswith(b"HTTP/1.1 200")
    for _ in range(100):
        view = (await harness.http.get(f"/oauth/{login['login_id']}")).json()
        if view["state"] != "pending":
            break
        await asyncio.sleep(0.01)
    assert view["state"] == "authenticated"
    assert view["authorization_url"] is None and view["listener"] is False
    assert harness.exchange.codes == ["fixture-code"]

    saved = await harness.http.post(
        "/oauth/models",
        json={"login_id": login["login_id"], "text_model": "gpt-6-sol"},
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["status"]["auth"] == "oauth"
    assert harness.runtime.current.state == "configured"
    # The login is consumed by the save.
    gone = await harness.http.get(f"/oauth/{login['login_id']}")
    assert gone.status_code == 404
    # Models change later without a new sign-in.
    changed = await harness.http.post(
        "/oauth/models", json={"text_model": "gpt-6-luna"}
    )
    assert changed.json()["status"]["text_model"] == "gpt-6-luna"


async def test_oauth_paste_completion_checks_host_port_and_state(harness, port):
    login = (await harness.http.post("/oauth/start")).json()
    login_id, state = login["login_id"], oauth_state(login)
    for url in (
        f"http://127.0.0.1:{port}/auth/callback?code=c&state={state}",
        f"https://localhost:{port}/auth/callback?code=c&state={state}",
        f"http://localhost:{port + 1}/auth/callback?code=c&state={state}",
        f"http://user@localhost:{port}/auth/callback?code=c&state={state}",
        f"http://localhost:{port}/auth/callback?code=c&state=wrong",
        f"http://localhost:{port}/other?code=c&state={state}",
    ):
        rejected = await harness.http.post(
            "/oauth/complete", json={"login_id": login_id, "redirect_url": url}
        )
        assert rejected.status_code == 400, url
        assert rejected.json()["code"] == "invalid_callback"
    assert harness.exchange.codes == []
    done = await harness.http.post(
        "/oauth/complete",
        json={
            "login_id": login_id,
            "redirect_url": f"http://localhost:{port}/auth/callback?code=pasted&state={state}",
        },
    )
    assert done.json().get("state") == "authenticated", done.text
    assert harness.exchange.codes == ["pasted"]


async def test_oauth_paste_works_without_a_listener(tmp_path, port):
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", port))
        busy.listen()
        harness = Harness(tmp_path / "runtime")
        try:
            login = (await harness.http.post("/oauth/start")).json()
            assert login["listener"] is False
            url = (
                f"http://localhost:{port}/auth/callback"
                f"?code=pasted&state={oauth_state(login)}"
            )
            done = await harness.http.post(
                "/oauth/complete",
                json={"login_id": login["login_id"], "redirect_url": url},
            )
            assert done.json()["state"] == "authenticated"
        finally:
            await harness.aclose()


async def test_second_login_conflicts_until_cancelled(harness):
    first = (await harness.http.post("/oauth/start")).json()
    second = await harness.http.post("/oauth/start")
    assert second.status_code == 409
    assert second.json() == {
        "detail": "A sign-in is already in progress; resume or cancel it.",
        "code": "login_pending",
        "login_id": first["login_id"],
    }
    cancelled = await harness.http.delete(f"/oauth/{first['login_id']}")
    assert cancelled.status_code == 204
    assert (await harness.http.get(f"/oauth/{first['login_id']}")).status_code == 404
    third = await harness.http.post("/oauth/start")
    assert third.status_code == 200 and third.json()["login_id"] != first["login_id"]


async def test_cancelling_a_stale_login_keeps_the_newer_one(harness):
    logins = harness.logins
    stale = await logins.start()
    logins._login.status = "failed"
    async with logins._lock:
        # The stale cancel has passed the router and waits for the lock while a
        # new login replaces the failed one.
        cancel = asyncio.create_task(logins.cancel(stale.login_id))
        await asyncio.sleep(0)
        fresh = await logins._open()
    with pytest.raises(SetupRequestError) as caught:
        await cancel
    assert caught.value.code == "login_not_found"
    assert (await logins.get(fresh.login_id)).state == "pending"


async def test_expired_login_closes_and_cannot_save(tmp_path, port):
    harness = Harness(tmp_path / "runtime", timeout=0.05)
    try:
        login = (await harness.http.post("/oauth/start")).json()
        await asyncio.sleep(0.2)
        view = (await harness.http.get(f"/oauth/{login['login_id']}")).json()
        assert view["state"] == "expired" and view["listener"] is False
        with pytest.raises(OSError):
            await callback(port, "/auth/callback")
        saved = await harness.http.post(
            "/oauth/models",
            json={"login_id": login["login_id"], "text_model": "gpt-6-sol"},
        )
        assert saved.status_code == 409 and saved.json()["code"] == "login_expired"
        # An expired login does not block a new one.
        assert (await harness.http.post("/oauth/start")).status_code == 200
    finally:
        await harness.aclose()


async def test_denied_or_failed_sign_in_reports_failure(harness, port):
    login = (await harness.http.post("/oauth/start")).json()
    denied = f"/auth/callback?error=access_denied&state={oauth_state(login)}"
    assert (await callback(port, denied)).startswith(b"HTTP/1.1 400")
    view = (await harness.http.get(f"/oauth/{login['login_id']}")).json()
    assert view["state"] == "failed" and "denied" in view["error"]
    not_ready = await harness.http.post(
        "/oauth/models",
        json={"login_id": login["login_id"], "text_model": "gpt-6-sol"},
    )
    assert not_ready.json()["code"] == "login_not_ready"

    harness.exchange.fail = True
    retry = (await harness.http.post("/oauth/start")).json()
    url = f"http://localhost:{port}/auth/callback?code=c&state={oauth_state(retry)}"
    failed = await harness.http.post(
        "/oauth/complete", json={"login_id": retry["login_id"], "redirect_url": url}
    )
    assert failed.json()["state"] == "failed"


async def test_cancel_during_a_pasted_exchange_reports_the_cancel(harness, port):
    gate = asyncio.Event()
    exchange = harness.exchange

    async def blocked(code, verifier):
        await gate.wait()
        return await exchange(code, verifier)

    harness.logins._exchange = blocked
    login = (await harness.http.post("/oauth/start")).json()
    url = f"http://localhost:{port}/auth/callback?code=c&state={oauth_state(login)}"
    pending = asyncio.create_task(
        harness.http.post(
            "/oauth/complete", json={"login_id": login["login_id"], "redirect_url": url}
        )
    )
    await asyncio.sleep(0.05)
    assert (await harness.http.delete(f"/oauth/{login['login_id']}")).status_code == 204
    response = await pending
    assert response.status_code == 404 and response.json()["code"] == "login_not_found"
    assert exchange.codes == []


async def test_model_save_without_login_needs_a_saved_oauth_credential(harness):
    response = await harness.http.post(
        "/oauth/models", json={"text_model": "gpt-6-sol"}
    )
    assert response.status_code == 409 and response.json()["code"] == "login_required"


async def test_image_size_saves_and_says_restart(harness):
    response = await harness.http.put("/image-size", json={"image_size": "auto"})
    assert response.json() == {"image_size": "auto", "restart_required": True}
    assert harness.store.read_config().image_size == "auto"
    assert (await harness.http.get("/status")).json()["image_size"] == "auto"


async def test_clerk_mode_requires_an_admin(tmp_path, port):
    harness = Harness(
        tmp_path / "runtime", auth_mode="clerk", admins=frozenset({"user_admin"})
    )
    try:
        harness.user = "user_other"
        # Every signed-in user reads the status for banners; changes need an admin.
        assert (await harness.http.get("/status")).json()["state"] == "unconfigured"
        for method, path in (
            ("GET", "/models?provider=openai&auth=api_key"),
            ("POST", "/api-key"),
            ("POST", "/oauth/start"),
            ("PUT", "/image-size"),
        ):
            response = await harness.http.request(method, path, json={})
            assert response.status_code == 403, path
            assert response.json()["code"] == "forbidden"
        harness.user = "user_admin"
        assert (
            await harness.http.get("/models?provider=openai&auth=api_key")
        ).status_code == 200
    finally:
        await harness.aclose()


async def test_clerk_mode_without_admins_disables_browser_setup(tmp_path, port):
    harness = Harness(tmp_path / "runtime", auth_mode="clerk")
    try:
        assert (await harness.http.get("/status")).status_code == 200
        assert (await harness.http.post("/oauth/start")).status_code == 403
    finally:
        await harness.aclose()


def test_application_registers_setup_routes_and_error_mapping():
    from main import app

    paths = {route.path for route in app.routes}
    assert "/api/v1/setup/status" in paths
    assert "/api/v1/setup/oauth/{login_id}" in paths
    assert SetupRequestError in app.exception_handlers


async def test_model_save_without_a_key_reuses_the_saved_key(harness):
    missing = await harness.http.post("/api-key", json=api_key_body(api_key=None))
    assert missing.status_code == 409 and missing.json()["code"] == "key_required"
    await harness.http.post("/api-key", json=api_key_body())
    other = await harness.http.post(
        "/api-key",
        json=api_key_body(provider="openai", api_key=None, text_model="gpt-5-mini"),
    )
    assert other.status_code == 409 and other.json()["code"] == "key_required"
    choices = (
        await harness.http.get(
            "/models", params={"provider": "deepseek", "auth": "api_key"}
        )
    ).json()["text"]
    response = await harness.http.post(
        "/api-key", json=api_key_body(api_key=None, text_model=choices[-1])
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"]["text_model"] == choices[-1]
    assert harness.verifier.calls[-1] == ("deepseek", choices[-1])
    assert KEY in (harness.store.home / "auth.json").read_text()

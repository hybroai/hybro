"""Offline OAuth tests: fake listeners, fake browser, MockTransport and temp store."""

import asyncio
import base64
import hashlib
import json
import time
import traceback
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from common.config.runtime_config import RuntimeConfigurationError
from common.config.runtime_store import RuntimeConfigStore
from llm_gateway import openai_oauth as oauth
from tests.fakes.llm_runtime import oauth_config, oauth_credential, runtime_state


class Body(httpx.AsyncByteStream):
    def __init__(self, data):
        self.data = data
        self.closed = False

    async def __aiter__(self):
        yield self.data

    async def aclose(self):
        self.closed = True


def token_transport(calls, *, status=200, body=None):
    def handle(request):
        calls.append(request)
        assert str(request.url) == oauth.TOKEN_URL
        value = (
            body
            if body is not None
            else {
                "access_token": oauth_credential().access_token.get_secret_value(),
                "refresh_token": "rotated-fixture",
                "expires_in": 3600,
            }
        )
        return httpx.Response(status, stream=Body(json.dumps(value).encode()))

    return httpx.MockTransport(handle)


def test_pkce_and_callback_validation():
    verifier, state, url = oauth.authorization_flow()
    query = parse_qs(urlsplit(url).query)
    assert len(verifier) == 43 and len(state) == 32
    assert query["code_challenge"] == [
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .decode()
        .rstrip("=")
    ]
    assert query["originator"] == ["hybro"]
    assert query["client_id"] == [oauth.CLIENT_ID]
    assert query["redirect_uri"] == [oauth.REDIRECT_URI]
    assert (
        oauth.callback_code("/auth/callback?code=fixture-code&state=" + state, state)
        == "fixture-code"
    )


@pytest.mark.parametrize(
    "target",
    [
        "/other?code=c&state=s",
        "/auth/callback?code=c",
        "/auth/callback?code=c&state=wrong",
        "/auth/callback?code=c&code=d&state=s",
        "/auth/callback?code=c&state=s&state=s",
        "/auth/callback?state=s",
        "http://evil.invalid/auth/callback?code=c&state=s",
        "/auth/callback?code=c&state=s#fragment",
        "/auth/callback?error=fixture-secret-marker-denial&state=s",
        "/auth/callback?code=%FF&state=s",
        "/auth/callback?code=c&state=%C3%A9",
        "/auth/callback?code=c&state=非ascii",
    ],
)
def test_callback_rejects_malformed_state_and_errors(target):
    if "%FF" in target:
        # An invalid UTF-8 code must never reach token exchange.
        with pytest.raises(RuntimeConfigurationError):
            oauth.callback_code(target, "s")
    else:
        with pytest.raises(RuntimeConfigurationError):
            oauth.callback_code(target, "s")


@pytest.mark.parametrize("token", ["bad", "a.@@.b", "a.e30.b", "a.W10.b", "a.bnVsbA.b"])
def test_invalid_account_metadata(token):
    with pytest.raises(RuntimeConfigurationError):
        oauth.account_id(token)


@pytest.mark.parametrize(
    "body",
    [
        None,
        [],
        {},
        {
            "access_token": "fixture-secret-marker",
            "refresh_token": "fixture-secret-marker",
            "expires_in": True,
        },
        {
            "access_token": "fixture-secret-marker",
            "refresh_token": "fixture-secret-marker",
            "expires_in": -1,
        },
        {
            "access_token": "fixture-secret-marker",
            "refresh_token": "fixture-secret-marker",
            "expires_in": float("inf"),
        },
        {
            "access_token": "fixture-secret-marker",
            "refresh_token": "fixture-secret-marker",
            "expires_in": 10**1000,
        },
    ],
)
async def test_token_response_validation_and_redaction(body):
    calls = []
    # JSON null is distinct from token_transport's default successful body.
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, stream=Body(json.dumps(body).encode()))
    )
    with pytest.raises(RuntimeConfigurationError) as error:
        await oauth.refresh(oauth_credential(), transport=transport)
    assert "fixture-secret-marker" not in "".join(
        traceback.format_exception(error.value)
    )
    assert error.value.__context__ is None
    assert calls == []


@pytest.mark.parametrize("status", [302, 401, 429, 500])
async def test_token_http_failure_no_retry_or_redirect(status):
    calls = []
    with pytest.raises(RuntimeConfigurationError):
        await oauth.refresh(
            oauth_credential(),
            transport=token_transport(
                calls, status=status, body={"error": "fixture-secret-marker"}
            ),
        )
    assert len(calls) == 1


async def test_refresh_rotation_and_account_switch():
    calls = []
    result = await oauth.refresh(oauth_credential(), transport=token_transport(calls))
    assert result.refresh_token.get_secret_value() == "rotated-fixture"
    assert parse_qs(calls[0].content.decode())["grant_type"] == ["refresh_token"]
    other = oauth_credential("other")
    with pytest.raises(RuntimeConfigurationError, match="account changed"):
        await oauth.refresh(
            oauth_credential(),
            transport=token_transport(
                [],
                body={
                    "access_token": other.access_token.get_secret_value(),
                    "refresh_token": "new",
                    "expires_in": 3600,
                },
            ),
        )


class Writer:
    def __init__(self):
        self.data = b""
        self.closed = False

    def write(self, value):
        self.data += value

    async def drain(self):
        pass

    def close(self):
        self.closed = True

    async def wait_closed(self):
        pass


class Server:
    def __init__(self):
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.close()

    def close(self):
        self.closed = True

    async def wait_closed(self):
        pass


@pytest.mark.parametrize("opened", [True, False])
async def test_browser_login_mock_listener_exchange_cleanup(monkeypatch, opened):
    server, writers, calls, reports, callbacks = Server(), [], [], [], []

    async def start(handle, host, port, **kwargs):
        assert (host, port, kwargs) == ("127.0.0.1", 1455, {"limit": 8192})
        callbacks.append(handle)
        return server

    monkeypatch.setattr(oauth.asyncio, "start_server", start)
    monkeypatch.setattr(oauth, "_open_browser", AsyncMock(return_value=opened))
    pending = []

    def report(value):
        reports.append(value)
        if "https://auth.openai.com/oauth/authorize?" in value:
            state = parse_qs(urlsplit(value.split("\n")[-1]).query)["state"][0]

            async def callback():
                # Invalid state is ignored, then a valid one completes login.
                for supplied in ["wrong", "%C3%A9", state]:
                    reader, writer = asyncio.StreamReader(), Writer()
                    writers.append(writer)
                    reader.feed_data(
                        f"GET /auth/callback?code=fixture-code&state={supplied} HTTP/1.1\r\nHost: localhost:1455\r\n\r\n".encode()
                    )
                    await callbacks[0](reader, writer)

            pending.append(asyncio.create_task(callback()))

    result = await oauth.login(report, transport=token_transport(calls))
    await asyncio.gather(*pending)
    assert result.account_id == "fixture-account" and server.closed
    assert all(writer.closed for writer in writers)
    assert writers[0].data.startswith(b"HTTP/1.1 400")
    assert writers[1].data.startswith(b"HTTP/1.1 400")
    assert writers[2].data.startswith(b"HTTP/1.1 200")
    assert len(calls) == 1
    fields = parse_qs(calls[0].content.decode())
    assert fields["code"] == ["fixture-code"] and fields["redirect_uri"] == [
        oauth.REDIRECT_URI
    ]
    assert len(fields["code_verifier"][0]) == 43
    assert any("manually" in value for value in reports) is (not opened)
    assert all("fixture-code" not in value for value in reports)


@pytest.mark.parametrize("mode", ["cancel", "timeout", "bind"])
async def test_login_failure_cleans_listener_without_exchange(monkeypatch, mode):
    server, ready = Server(), asyncio.Event()

    async def start(*args, **kwargs):
        if mode == "bind":
            raise OSError("fixture-secret-marker")
        return server

    monkeypatch.setattr(oauth.asyncio, "start_server", start)
    monkeypatch.setattr(oauth, "_open_browser", AsyncMock(return_value=False))
    if mode == "timeout":
        monkeypatch.setattr(oauth, "LOGIN_TIMEOUT", 0.01)
    calls = []
    task = asyncio.create_task(
        oauth.login(lambda value: ready.set(), transport=token_transport(calls))
    )
    if mode == "cancel":
        await ready.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        with pytest.raises(RuntimeConfigurationError):
            await task
    assert not calls
    assert server.closed is (mode != "bind")


async def test_refresh_serialized_reread_and_revision_unchanged(tmp_path):
    store = RuntimeConfigStore(tmp_path)
    initial = oauth_credential(expires_at=time.time() - 1)
    config = oauth_config()
    store.save(config, initial, {})
    config_bytes = (tmp_path / "config.json").read_bytes()
    updated = oauth_credential(refresh="rotation")
    calls = []

    async def refresh(credential):
        calls.append(credential)
        await asyncio.sleep(0.03)
        return updated

    results = await asyncio.gather(
        *[
            RuntimeConfigStore(tmp_path).resolve_oauth(
                config, initial.account_id, {}, refresh
            )
            for _ in range(5)
        ]
    )
    assert results == [updated] * 5 and calls == [initial]
    assert store.load({}).authentication.credential == updated
    assert (tmp_path / "config.json").read_bytes() == config_bytes
    assert store.load({}).config.revision == config.revision


@pytest.mark.parametrize("change", ["config", "auth", "account", "delete"])
async def test_refresh_never_overwrites_switched_identity(tmp_path, change):
    store = RuntimeConfigStore(tmp_path)
    initial, config = oauth_credential(expires_at=1), oauth_config()
    store.save(config, initial, {})
    environment = {}
    if change == "config":
        store.save(
            config.model_copy(
                update={"models": config.models.model_copy(update={"text": "other"})}
            ),
            initial,
            {},
        )
    elif change == "auth":
        state = runtime_state()
        store.save(state.config, state.authentication.credential, {})
    elif change == "account":
        store.save(config, oauth_credential("other", expires_at=1), {})
    elif change == "delete":
        (tmp_path / "auth.json").unlink()
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    refresh = AsyncMock()
    with pytest.raises(RuntimeConfigurationError):
        await store.resolve_oauth(config, initial.account_id, environment, refresh)
    refresh.assert_not_awaited()
    assert before == {p.name: p.read_bytes() for p in tmp_path.iterdir()}


async def test_refresh_cancel_releases_lock_and_does_not_save(tmp_path, monkeypatch):
    monkeypatch.setattr("common.config.runtime_store._OAUTH_REFRESH_TIMEOUT", 0.03)
    store, ready = RuntimeConfigStore(tmp_path), asyncio.Event()
    initial, config = oauth_credential(expires_at=1), oauth_config()
    store.save(config, initial, {})

    async def refresh(value):
        ready.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(
        store.resolve_oauth(config, initial.account_id, {}, refresh)
    )
    await ready.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await asyncio.to_thread(store.load, {})
    assert store.load({}).authentication.credential == initial


async def test_cancel_after_rotation_received_still_persists_before_unlock(tmp_path):
    store, ready, cleanup = (
        RuntimeConfigStore(tmp_path),
        asyncio.Event(),
        asyncio.Event(),
    )
    config, initial, updated = (
        oauth_config(),
        oauth_credential(expires_at=1),
        oauth_credential(refresh="rotated"),
    )
    store.save(config, initial, {})

    async def refresh(value):
        # Simulate receipt/validation followed by asynchronous HTTP client cleanup.
        ready.set()
        await cleanup.wait()
        return updated

    task = asyncio.create_task(
        store.resolve_oauth(config, initial.account_id, {}, refresh)
    )
    await ready.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    cleanup.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert store.load({}).authentication.credential == updated


async def test_refresh_detects_noncooperating_write_during_network(tmp_path):
    store, config, initial = (
        RuntimeConfigStore(tmp_path),
        oauth_config(),
        oauth_credential(expires_at=1),
    )
    store.save(config, initial, {})

    async def refresh(value):
        # Deliberately bypass shared flock to test pre-save compare-and-set.
        (tmp_path / "config.json").write_text(
            "provider: {id: openai, auth: oauth}\nmodels: {text: changed}\n"
        )
        return oauth_credential()

    with pytest.raises(RuntimeConfigurationError):
        await store.resolve_oauth(config, initial.account_id, {}, refresh)
    assert store.read_stored_credential() == initial


async def test_async_lock_wait_is_cancelable_and_off_event_loop(tmp_path, monkeypatch):
    import threading

    store, initial, config = (
        RuntimeConfigStore(tmp_path),
        oauth_credential(expires_at=1),
        oauth_config(),
    )
    store.save(config, initial, {})
    event_thread = threading.get_ident()
    original_open, original_read = store._open_lock, store._read
    calls = []

    def checked_open():
        assert threading.get_ident() != event_thread
        calls.append("open")
        return original_open()

    def checked_read(name):
        assert threading.get_ident() != event_thread
        return original_read(name)

    monkeypatch.setattr(store, "_open_lock", checked_open)
    monkeypatch.setattr(store, "_read", checked_read)
    with RuntimeConfigStore(tmp_path)._locked():
        task = asyncio.create_task(
            store.resolve_oauth(config, initial.account_id, {}, AsyncMock())
        )
        await asyncio.sleep(0.03)
        assert calls and not task.done()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    updated = oauth_credential()
    assert (
        await store.resolve_oauth(
            config, initial.account_id, {}, AsyncMock(return_value=updated)
        )
        == updated
    )


@pytest.mark.parametrize("kind", ["oversize", "compressed", "malformed", "transport"])
async def test_token_boundary_limits_and_closes(kind):
    body = Body(b"x" * 65537 if kind == "oversize" else b"bad-json")

    def handle(request):
        if kind == "transport":
            raise httpx.ConnectError("fixture-secret-marker", request=request)
        return httpx.Response(
            200,
            stream=body,
            headers={
                "content-encoding": "gzip" if kind == "compressed" else "identity"
            },
        )

    with pytest.raises(RuntimeConfigurationError) as error:
        await oauth.refresh(oauth_credential(), transport=httpx.MockTransport(handle))
    assert "fixture-secret-marker" not in str(error.value)
    assert error.value.__context__ is None
    assert body.closed is (kind != "transport")


@pytest.mark.parametrize("mode", ["success", "failure", "missing", "cancel"])
async def test_os_browser_opener_is_mocked_bounded_and_cleaned(monkeypatch, mode):
    ready = asyncio.Event()

    class Process:
        returncode = None
        killed = False

        async def wait(self):
            if mode == "cancel" and not self.killed:
                ready.set()
                await asyncio.Event().wait()
            self.returncode = 1 if mode == "failure" else 0
            return self.returncode

        def kill(self):
            self.killed = True

    process = Process()

    async def create(*args, **kwargs):
        assert args == ("/fixture/xdg-open", "https://auth.openai.com/fixture")
        assert kwargs["stdout"] == asyncio.subprocess.DEVNULL
        return process

    monkeypatch.setattr(oauth.sys, "platform", "linux")
    monkeypatch.setattr(
        oauth.shutil,
        "which",
        lambda _: None if mode == "missing" else "/fixture/xdg-open",
    )
    monkeypatch.setattr(oauth.asyncio, "create_subprocess_exec", create)
    task = asyncio.create_task(oauth._open_browser("https://auth.openai.com/fixture"))
    if mode == "cancel":
        await ready.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert process.killed and process.returncode == 0
    else:
        assert await task is (mode == "success")


async def test_login_authorization_denial_is_immediate_and_closes(monkeypatch):
    server, writer, callbacks, calls = Server(), Writer(), [], []

    async def start(handle, *args, **kwargs):
        callbacks.append(handle)
        return server

    monkeypatch.setattr(oauth.asyncio, "start_server", start)
    monkeypatch.setattr(oauth, "_open_browser", AsyncMock(return_value=False))
    pending = []

    def report(value):
        if "https://auth.openai.com/oauth/authorize?" not in value:
            return
        state = parse_qs(urlsplit(value.split("\n")[-1]).query)["state"][0]
        reader = asyncio.StreamReader()
        reader.feed_data(
            f"GET /auth/callback?error=access_denied&state={state} HTTP/1.1\r\nHost: localhost\r\n\r\n".encode()
        )
        pending.append(asyncio.create_task(callbacks[0](reader, writer)))

    with pytest.raises(RuntimeConfigurationError, match="denied"):
        await oauth.login(report, transport=token_transport(calls))
    await asyncio.gather(*pending)
    assert server.closed and writer.closed and not calls


@pytest.mark.parametrize(
    ("result", "status", "title"),
    [
        ("success", b"200 OK", "Signed in with ChatGPT"),
        ("denied", b"400 Bad Request", "Sign-in cancelled"),
        ("invalid", b"400 Bad Request", "This sign-in link has expired"),
    ],
)
def test_callback_page_is_self_contained_html(result, status, title):
    head, page = oauth._callback_response(result).split(b"\r\n\r\n", 1)
    lines = head.split(b"\r\n")
    assert lines[0] == b"HTTP/1.1 " + status
    assert b"Content-Type: text/html; charset=utf-8" in lines
    assert b"Cache-Control: no-store" in lines
    assert f"Content-Length: {len(page)}".encode() in lines
    html = page.decode()
    assert html.startswith("<!doctype html>") and f"<h1>{title}</h1>" in html
    assert "· ChatGPT sign-in" in html
    # Inline only: no fonts, scripts, links, buttons or remote requests.
    for marker in ("http", "<script", "<link", "<button", "@import", "url("):
        assert marker not in html


async def test_listener_serves_html_for_each_callback_result():
    pages, codes, denied = [], [], []
    first = oauth.CallbackListener("s" * 32, codes.append, lambda: denied.append(1))
    second = oauth.CallbackListener("t" * 32, codes.append, lambda: denied.append(1))

    async def send(listener, target):
        reader, writer = asyncio.StreamReader(), Writer()
        reader.feed_data(f"GET {target} HTTP/1.1\r\nHost: localhost\r\n\r\n".encode())
        await listener._handle(reader, writer)
        pages.append(writer.data)

    # Wrong state, the valid callback, a replay, then a denial on a new login.
    await send(first, "/auth/callback?code=c&state=wrong")
    await send(first, f"/auth/callback?code=c&state={'s' * 32}")
    await send(first, f"/auth/callback?code=c&state={'s' * 32}")
    await send(second, f"/auth/callback?error=access_denied&state={'t' * 32}")
    assert codes == ["c"] and denied == [1]
    assert [page.split(b"\r\n", 1)[0] for page in pages] == [
        b"HTTP/1.1 400 Bad Request",
        b"HTTP/1.1 200 OK",
        b"HTTP/1.1 400 Bad Request",
        b"HTTP/1.1 400 Bad Request",
    ]
    assert [b"<h1>" + page.split(b"<h1>")[1].split(b"</h1>")[0] for page in pages] == [
        b"<h1>This sign-in link has expired",
        b"<h1>Signed in with ChatGPT",
        b"<h1>This sign-in link has expired",
        b"<h1>Sign-in cancelled",
    ]

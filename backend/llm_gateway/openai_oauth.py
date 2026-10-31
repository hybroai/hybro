"""Host PKCE login and backend refresh for ChatGPT/Codex subscriptions.

Translated from pi-ai (MIT); see oauth_notice.py. JWT decoding extracts routing
metadata only, never authenticates a Hybro user. All destinations are fixed.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import math
import secrets
import shutil
import sys
import time
from collections.abc import Callable, Mapping
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
from pydantic import SecretStr

from common.config.runtime_config import (
    OAuthCredential,
    RuntimeConfigurationError,
    account_id,
)
from llm_gateway._diagnostics import _Diagnostic, _http_diagnostic

CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
CALLBACK_PORT = 1455
REDIRECT_URI = f"http://localhost:{CALLBACK_PORT}/auth/callback"
TOKEN_URL = "https://auth.openai.com/oauth/token"
LOGIN_TIMEOUT = 300
REFRESH_MARGIN = 300


class _AuthorizationDenied(RuntimeConfigurationError):
    pass


AuthorizationDenied = _AuthorizationDenied


def authorization_flow() -> tuple[str, str, str]:
    verifier = secrets.token_urlsafe(32)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .decode()
        .rstrip("=")
    )
    state = secrets.token_hex(16)
    query = urlencode(
        {
            "response_type": "code",
            "client_id": CLIENT_ID,
            "redirect_uri": REDIRECT_URI,
            "scope": "openid profile email offline_access",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": state,
            "id_token_add_organizations": "true",
            "codex_cli_simplified_flow": "true",
            "originator": "hybro",
        }
    )
    return verifier, state, "https://auth.openai.com/oauth/authorize?" + query


def callback_code(target: str, state: str) -> str:
    """Strict one-time state check; no bare-code/manual state bypass."""
    try:
        url = urlsplit(target)
        if url.scheme or url.netloc or url.path != "/auth/callback" or url.fragment:
            raise ValueError
        query = parse_qs(
            url.query, strict_parsing=True, max_num_fields=16, errors="strict"
        )
        supplied = query.get("state", [])
        if (
            len(supplied) != 1
            or not supplied[0].isascii()
            or not secrets.compare_digest(supplied[0], state)
        ):
            raise ValueError
        if "error" in query:
            raise _AuthorizationDenied("OAuth authorization was denied; rerun setup.")
        codes = query.get("code", [])
        if len(codes) != 1 or not codes[0] or len(codes[0]) > 4096:
            raise ValueError
        return codes[0]
    except _AuthorizationDenied:
        raise
    except ValueError:
        raise RuntimeConfigurationError("Invalid OAuth callback or state.") from None


async def _token(
    fields: Mapping[str, str], *, transport: httpx.AsyncBaseTransport | None = None
) -> OAuthCredential:
    stage = (
        "refresh" if fields.get("grant_type") == "refresh_token" else "token_exchange"
    )
    diagnostic = _Diagnostic(stage, "local_validation")
    status = None
    try:
        async with asyncio.timeout(15):
            async with httpx.AsyncClient(
                transport=transport or httpx.AsyncHTTPTransport(retries=0),
                timeout=15,
                follow_redirects=False,
                trust_env=False,
            ) as client:
                async with client.stream(
                    "POST",
                    TOKEN_URL,
                    data={"client_id": CLIENT_ID, **fields},
                    headers={"user-agent": "hybro", "accept-encoding": "identity"},
                ) as response:
                    status = response.status_code
                    if status != 200:
                        diagnostic = _http_diagnostic(stage, status)
                        raise ValueError
                    if (
                        response.headers.get("content-encoding", "identity")
                        != "identity"
                    ):
                        raise ValueError
                    body = bytearray()
                    async for chunk in response.aiter_raw():
                        if len(body) + len(chunk) > 65536:
                            raise ValueError
                        body.extend(chunk)
                    raw = json.loads(body)
        access, refresh, expiry = (
            raw["access_token"],
            raw["refresh_token"],
            raw["expires_in"],
        )
        if (
            not isinstance(access, str)
            or not isinstance(refresh, str)
            or not refresh.strip()
            or len(refresh) > 16384
            or type(expiry) not in {int, float}
            or not math.isfinite(expiry)
            or expiry <= 0
        ):
            raise ValueError
        return OAuthCredential(
            access_token=SecretStr(access),
            refresh_token=SecretStr(refresh),
            expires_at=time.time() + expiry,
            account_id=account_id(access),
        )
    except json.JSONDecodeError:
        diagnostic = _Diagnostic(stage, "bad_json", status)
    except (httpx.TimeoutException, TimeoutError):
        diagnostic = _Diagnostic(stage, "timeout", status)
    except httpx.HTTPError:
        diagnostic = _Diagnostic(stage, "network", status)
    except (ValueError, KeyError, TypeError, RecursionError, OverflowError):
        pass
    # Raise outside the handler: no HTTP request, tokens or raw body in context.
    raise RuntimeConfigurationError(
        "OAuth token exchange/refresh failed; " + diagnostic.describe()
    )


async def refresh(
    credential: OAuthCredential, *, transport: httpx.AsyncBaseTransport | None = None
) -> OAuthCredential:
    refreshed = await _token(
        {
            "grant_type": "refresh_token",
            "refresh_token": credential.refresh_token.get_secret_value(),
        },
        transport=transport,
    )
    if refreshed.account_id != credential.account_id:
        raise RuntimeConfigurationError(
            "OAuth account changed during refresh; rerun setup."
        )
    return refreshed


async def _open_browser(url: str) -> bool:
    """Bound the OS URL opener itself; no uncancelable executor browser thread."""
    command = "/usr/bin/open" if sys.platform == "darwin" else shutil.which("xdg-open")
    if command is None:
        return False
    process = None
    try:
        process = await asyncio.create_subprocess_exec(
            command,
            url,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        async with asyncio.timeout(5):
            return await process.wait() == 0
    except (OSError, TimeoutError):
        return False
    finally:
        if process is not None and process.returncode is None:
            process.kill()
            await process.wait()


async def exchange_code(
    code: str, verifier: str, *, transport: httpx.AsyncBaseTransport | None = None
) -> OAuthCredential:
    """Exchange one authorization code from the fixed redirect for tokens."""
    return await _token(
        {
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": verifier,
            "redirect_uri": REDIRECT_URI,
        },
        transport=transport,
    )


_CALLBACK_PAGES = {
    "success": (
        "200 OK",
        "103,228,149",
        "0.35",
        '<path d="M20 6 9 17l-5-5"/>',
        "Signed in with ChatGPT",
        "Hybro now uses your ChatGPT account.",
        "You can close this tab. Your Hybro tab has already updated.",
    ),
    "denied": (
        "400 Bad Request",
        "240,176,66",
        "0.35",
        '<path d="M18 6 6 18"/><path d="m6 6 12 12"/>',
        "Sign-in cancelled",
        "Access was declined on chatgpt.com, so nothing was connected or saved.",
        "You can close this tab and try again from Settings, Model provider in Hybro.",
    ),
    "invalid": (
        "400 Bad Request",
        "242,121,107",
        "0.4",
        '<circle cx="12" cy="12" r="9"/><path d="M12 8v4"/><path d="M12 16h.01"/>',
        "This sign-in link has expired",
        "It was already used, or a newer sign-in was started. Nothing was changed.",
        "You can close this tab and start a new sign-in from Settings, Model provider"
        " in Hybro.",
    ),
}

_CALLBACK_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Hybro \u00b7 ChatGPT sign-in</title>
<style>
body{{margin:0;background:#0d0f12;color:#e4e7ec;font:14px ui-sans-serif,system-ui,\
-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif}}
.page{{min-height:100vh;box-sizing:border-box;padding:64px 16px;display:flex;\
flex-direction:column;align-items:center;justify-content:center;gap:32px}}
.logo{{display:inline-flex;gap:2px;font-weight:700;font-size:28px;line-height:1}}
main{{width:100%;max-width:480px;box-sizing:border-box;padding:32px;\
border:1px solid #24272e;border-radius:16px;background:#16181d;display:flex;\
flex-direction:column;align-items:center;gap:16px;text-align:center}}
.icon{{width:56px;height:56px;border-radius:28px;display:inline-flex;\
align-items:center;justify-content:center;background:rgba({rgb},0.1);\
border:1px solid rgba({rgb},{ring})}}
.text{{display:flex;flex-direction:column;gap:8px}}
h1{{margin:0;font-size:22px;font-weight:600;letter-spacing:-0.01em}}
p{{margin:0;line-height:21px;color:#c3c8d1}}
.note{{width:100%;box-sizing:border-box;margin-top:4px;padding-top:16px;\
border-top:1px solid #24272e;font-size:13px;line-height:19px;color:#a6adb9}}
</style></head>
<body><div class="page">
<span class="logo"><span style="color:#3ec3d3">HY</span><span style="color:#b380f0">BRO</span></span>
<main>
<span class="icon" aria-hidden="true"><svg width="26" height="26" viewBox="0 0 24 24" \
fill="none" stroke="rgb({rgb})" stroke-width="2.5" stroke-linecap="round" \
stroke-linejoin="round">{icon}</svg></span>
<div class="text"><h1>{title}</h1><p>{body}</p></div>
<p class="note">{note}</p>
</main></div></body></html>
"""


def _callback_response(result: str) -> bytes:
    """One self-contained page per callback result; no external requests."""
    status, rgb, ring, icon, title, body, note = _CALLBACK_PAGES[result]
    page = _CALLBACK_HTML.format(
        rgb=rgb, ring=ring, icon=icon, title=title, body=body, note=note
    ).encode()
    return (
        f"HTTP/1.1 {status}\r\n"
        "Content-Type: text/html; charset=utf-8\r\n"
        f"Content-Length: {len(page)}\r\n"
        "Connection: close\r\n"
        "Cache-Control: no-store\r\n\r\n"
    ).encode() + page


class CallbackListener:
    """One-shot HTTP listener for the fixed redirect on port 1455.

    Each request must be a single ``GET`` whose path and state pass
    ``callback_code``; anything else gets 400 and leaves the listener waiting.
    The first valid callback or authorization denial ends it. Raw requests,
    codes and states are never reported.
    """

    def __init__(
        self,
        state: str,
        on_code: Callable[[str], None],
        on_denied: Callable[[], None],
    ) -> None:
        self._state = state
        self._on_code = on_code
        self._on_denied = on_denied
        self._done = False
        self._server: asyncio.Server | None = None
        self._connections: set[asyncio.Task[None]] = set()

    async def start(self, host: str) -> None:
        """Bind ``host:1455``; ``OSError`` when the port is unavailable."""
        self._server = await asyncio.start_server(
            self._handle, host, CALLBACK_PORT, limit=8192
        )

    async def close(self, *, cancel_connections: bool = True) -> None:
        """Stop listening; ``cancel_connections=False`` lets a handler finish."""
        server, self._server = self._server, None
        if server is not None:
            server.close()
        if cancel_connections:
            for task in tuple(self._connections):
                task.cancel()
        await asyncio.gather(*self._connections, return_exceptions=True)
        if server is not None:
            await server.wait_closed()

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        task = asyncio.current_task()
        self._connections.add(task)
        try:
            async with asyncio.timeout(5):
                request = await reader.readuntil(b"\r\n\r\n")
                if len(request) > 8192:
                    raise ValueError
                method, target, version = (
                    request.split(b"\r\n", 1)[0].decode("ascii").split(" ")
                )
                if method != "GET" or version != "HTTP/1.1" or self._done:
                    raise ValueError
                result = callback_code(target, self._state)
                self._done = True
                self._on_code(result)
                writer.write(_callback_response("success"))
                await writer.drain()
        except _AuthorizationDenied:
            self._done = True
            self._on_denied()
            writer.write(_callback_response("denied"))
        except (
            ValueError,
            UnicodeError,
            asyncio.IncompleteReadError,
            asyncio.LimitOverrunError,
            TimeoutError,
            OSError,
        ):
            writer.write(_callback_response("invalid"))
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass
            self._connections.discard(task)


async def login(
    report: Callable[[str], None], *, transport: httpx.AsyncBaseTransport | None = None
) -> OAuthCredential:
    """Five-minute loopback login; Ctrl-C closes listener and pending connections.

    Run setup on the browser's host. If automatic opening fails, open the printed
    URL on that same host. No externally exposed callback or pasted bare codes.
    """
    verifier, state, url = authorization_flow()
    code: asyncio.Future[str] = asyncio.get_running_loop().create_future()

    def on_code(value: str) -> None:
        if not code.done():
            code.set_result(value)

    def on_denied() -> None:
        if not code.done():
            code.set_exception(
                _AuthorizationDenied("OAuth authorization was denied; rerun setup.")
            )

    listener = CallbackListener(state, on_code, on_denied)
    try:
        async with asyncio.timeout(LOGIN_TIMEOUT):
            await listener.start("127.0.0.1")
            try:
                report(
                    "Open this URL on this computer to authorize Hybro (expires in 5 minutes):\n"
                    + url
                )
                opened = await _open_browser(url)
                if not opened:
                    report(
                        "Browser could not open; open the URL above manually on this computer."
                    )
                return await exchange_code(await code, verifier, transport=transport)
            finally:
                await listener.close()
    except TimeoutError:
        raise RuntimeConfigurationError("OAuth login timed out; rerun setup.") from None
    except OSError:
        raise RuntimeConfigurationError(
            "Cannot listen on 127.0.0.1:1455; run setup on your browser's host with that port free."
        ) from None

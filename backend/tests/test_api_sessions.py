"""Session HTTP routes: request validation, error mapping, auth, and limits."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest

from api_gateway.dependencies import get_owner_session_access, get_session_access
from api_gateway.source_token import SourceCaller
from common import auth
from common.auth import ClerkUser, get_current_user
from common.errors import AppError
from main import SESSION_MESSAGE_MAX_BYTES, SESSION_MESSAGE_MAX_CONCURRENT, create_app
from models.session import (
    SessionAgent,
    SessionClosed,
    SessionHistoryEntry,
    SessionListResponse,
    SessionMessageRequest,
    SessionMessageResponse,
    SessionOwnerRecord,
    SessionSource,
    SessionStatus,
    SessionSummary,
)
from session.access import BoundOwnerSessionAccess, BoundSessionAccess
from session.protocols import OwnerSessionAccess, SessionAccess, SessionOwner
from source.protocols import NO_TOKEN

NOW = datetime(2026, 10, 14, tzinfo=UTC)
NO_TOKEN_SOURCE = SessionSource(source_id="no_token", name="No token", has_token=False)
SEND = "/api/v1/sessions/messages"


class AccessStub:
    def __init__(self) -> None:
        self.sent: list[SessionMessageRequest] = []
        self.error: AppError | None = None
        self.started: asyncio.Queue[str] = asyncio.Queue()
        self.release = asyncio.Event()
        self.release.set()

    def _raise(self) -> None:
        if self.error is not None:
            raise self.error

    async def send(self, request: SessionMessageRequest) -> SessionMessageResponse:
        self.started.put_nowait(request.client_request_id)
        await self.release.wait()
        self._raise()
        self.sent.append(request)
        return SessionMessageResponse(
            session_id="sess-1",
            client_request_id=request.client_request_id,
            agent_id=request.agent_id,
            status="created",
            state="working",
            message="Created. Use session_id to query this session.",
            created_at=NOW,
            updated_at=NOW,
        )

    def _status(self, session_id: str) -> SessionStatus:
        return SessionStatus(
            session_id=session_id,
            state="waiting_on_owner",
            agents=[SessionAgent(agent_id="agent-1", name="Travel")],
            created_at=NOW,
            updated_at=NOW,
            history=[
                SessionHistoryEntry(
                    client_request_id="cr-0",
                    text="Plan a trip",
                    sent_at=NOW,
                    agent_id="agent-1",
                    agent_name="Travel",
                    state="completed",
                    reply="Earlier answer",
                    replied_at=NOW,
                ),
                SessionHistoryEntry(
                    client_request_id="cr-1",
                    text="Make it longer",
                    sent_at=NOW,
                    agent_id="agent-1",
                    agent_name="Travel",
                    state="waiting_on_owner",
                ),
            ],
        )

    async def check(self, session_id: str) -> SessionStatus:
        self._raise()
        return self._status(session_id)

    async def close(self, session_id: str) -> SessionClosed:
        self._raise()
        return SessionClosed(session_id=session_id, status="closed")

    async def list_sessions(self) -> SessionListResponse:
        return SessionListResponse(
            sessions=[
                SessionSummary(
                    session_id="sess-1",
                    source=NO_TOKEN_SOURCE,
                    client_label="Buyer agent",
                    state="waiting_on_owner",
                    conversation_id="conv-1",
                    opened_at=NOW,
                    last_activity_at=NOW,
                )
            ]
        )

    async def get_record(self, session_id: str) -> SessionOwnerRecord:
        self._raise()
        return SessionOwnerRecord(
            session_id=session_id,
            source=NO_TOKEN_SOURCE,
            client_label="Buyer agent",
            state="waiting_on_owner",
            paused_agent_id="agent-1",
            agents=[SessionAgent(agent_id="agent-1", name="Travel")],
            status="open",
            conversation_id="conv-1",
            opened_at=NOW,
            last_activity_at=NOW,
        )


def body(**overrides) -> dict:
    data = {
        "agent_id": "agent-1",
        "text": "Plan a trip",
        "client_request_id": "cr-1",
        "client_label": "Buyer agent",
    }
    data.update(overrides)
    return {key: value for key, value in data.items() if value is not None}


def make_app():
    access = AccessStub()
    app = create_app()
    app.dependency_overrides[get_session_access] = lambda: access
    app.dependency_overrides[get_owner_session_access] = lambda: access
    app.dependency_overrides[get_current_user] = lambda: ClerkUser(
        user_id="owner", session_id="s", claims={}
    )
    return app, access


def client_for(app):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://hybro.test",
    )


def test_stub_satisfies_both_session_surfaces():
    assert isinstance(AccessStub(), SessionAccess)
    assert isinstance(AccessStub(), OwnerSessionAccess)


async def test_send_returns_a_receipt():
    app, access = make_app()
    async with client_for(app) as client:
        response = await client.post(SEND, json=body())

    assert response.status_code == 200
    assert response.json() == {
        "session_id": "sess-1",
        "client_request_id": "cr-1",
        "agent_id": "agent-1",
        "status": "created",
        "state": "working",
        "message": "Created. Use session_id to query this session.",
        "created_at": "2026-10-14T00:00:00Z",
        "updated_at": "2026-10-14T00:00:00Z",
    }
    assert access.sent[0].agent_id == "agent-1"


@pytest.mark.parametrize(
    "payload",
    [
        body(client_request_id=None),
        body(client_request_id="x" * 129),
        body(text="x" * 10_001),
        body(text="   "),
        body(data={"a": 1}),
        body(files=[]),
    ],
)
async def test_send_rejects_invalid_requests_with_422(payload):
    app, access = make_app()
    async with client_for(app) as client:
        response = await client.post(SEND, json=payload)

    assert response.status_code == 422
    assert access.sent == []


async def test_follow_up_send_needs_no_client_label():
    app, access = make_app()
    async with client_for(app) as client:
        response = await client.post(
            SEND, json=body(session_id="sess-1", client_label=None)
        )

    assert response.status_code == 200
    assert access.sent[0].session_id == "sess-1"


@pytest.mark.parametrize(
    ("code", "status"),
    [
        ("invalid_request", 422),
        ("session_not_found", 404),
        ("agent_not_found", 404),
        ("subnet_not_found", 404),
        ("not_found", 404),
        ("forbidden", 403),
        ("session_closed", 409),
        ("request_conflict", 409),
        ("agent_unavailable", 409),
        ("request_rejected", 400),
        ("execution_error", 500),
    ],
)
async def test_send_maps_session_errors(code, status):
    app, access = make_app()
    access.error = AppError("refused", code=code)
    async with client_for(app) as client:
        response = await client.post(SEND, json=body())

    assert response.status_code == status
    assert response.json()["detail"] == {
        "code": code,
        "message": "refused",
        "client_request_id": "cr-1",
    }


async def test_check_close_list_and_record():
    app, _ = make_app()
    async with client_for(app) as client:
        check = await client.get("/api/v1/sessions/sess-1")
        record = await client.get("/api/v1/sessions/sess-1/record")
        listing = await client.get("/api/v1/sessions")
        close = await client.post("/api/v1/sessions/sess-1/close")
        owner_close = await client.post("/api/v1/sessions/sess-1/record/close")

    assert check.status_code == 200
    client_view = check.json()
    assert client_view["state"] == "waiting_on_owner"
    assert set(client_view) == {
        "session_id",
        "state",
        "reason",
        "agents",
        "created_at",
        "updated_at",
        "history",
    }
    assert set(client_view["history"][0]) == {
        "client_request_id",
        "text",
        "sent_at",
        "agent_id",
        "agent_name",
        "state",
        "reason",
        "reply",
        "replied_at",
    }
    assert record.json()["conversation_id"] == "conv-1"
    assert listing.json()["sessions"][0]["conversation_id"] == "conv-1"
    assert close.json() == {"session_id": "sess-1", "status": "closed"}
    assert owner_close.json() == {"session_id": "sess-1", "status": "closed"}
    assert listing.json()["sessions"][0]["source"] == {
        "source_id": "no_token",
        "name": "No token",
        "has_token": False,
    }


async def test_opening_without_a_label_reaches_the_session_check():
    # Whether client_label is needed depends on the token, so the route passes
    # it on and the session decides.
    app, access = make_app()
    async with client_for(app) as client:
        response = await client.post(SEND, json=body(client_label=None))
    assert response.status_code == 200
    assert access.sent[0].client_label is None


async def test_unknown_session_is_404_on_read_routes():
    app, access = make_app()
    access.error = AppError("Session not found", code="session_not_found")
    async with client_for(app) as client:
        check = await client.get("/api/v1/sessions/missing")
        close = await client.post("/api/v1/sessions/missing/close")
        record = await client.get("/api/v1/sessions/missing/record")
        owner_close = await client.post("/api/v1/sessions/missing/record/close")

    assert check.status_code == close.status_code == record.status_code == 404
    assert owner_close.status_code == 404
    assert check.json()["detail"]["session_id"] == "missing"


async def test_clerk_anonymous_owner_requests_fail_with_401(monkeypatch):
    app, access = make_app()
    app.dependency_overrides.pop(get_current_user)
    app.dependency_overrides.pop(get_owner_session_access)
    monkeypatch.setattr(auth, "_require_clerk_secret_key", lambda: "sk_test")
    monkeypatch.setattr(
        auth,
        "authenticate_request",
        lambda request, options: SimpleNamespace(is_signed_in=False, payload=None),
    )
    async with client_for(app) as client:
        responses = [
            await client.get("/api/v1/sessions"),
            await client.get("/api/v1/sessions/sess-1/record"),
            await client.post("/api/v1/sessions/sess-1/record/close"),
        ]

    assert [response.status_code for response in responses] == [401] * 3
    assert access.sent == []


def test_get_session_access_binds_source_owner_and_resolver():
    facade = object()
    deps = SimpleNamespace(
        session_facade=facade,
        agent_service=object(),
        agent_group_store=object(),
        agent_matcher=object(),
    )
    caller = SourceCaller(
        source=NO_TOKEN, owner=SessionOwner(user_id="owner-1", name="estate")
    )

    access = get_session_access(caller, deps)

    assert isinstance(access, BoundSessionAccess)
    assert access._owner.user_id == "owner-1"
    assert access._source == NO_TOKEN
    assert access._resolver._requesting_user_id == "owner-1"


def test_get_owner_session_access_binds_the_signed_in_owner():
    deps = SimpleNamespace(session_facade=object())
    owner = SessionOwner(user_id="owner-1", name="Ada")

    access = get_owner_session_access(owner, deps)

    assert isinstance(access, BoundOwnerSessionAccess)
    assert access._owner == owner


# -- Request limits -----------------------------------------------------------


def sized_body(size: int) -> bytes:
    empty = json.dumps(body(text="")).encode()
    return json.dumps(body(text="x" * (size - len(empty)))).encode()


async def test_session_body_limit_is_64_kib():
    assert SESSION_MESSAGE_MAX_BYTES == 64 * 1024
    app, access = make_app()
    async with client_for(app) as client:
        response = await client.post(
            SEND,
            content=b"",
            headers={"Content-Length": str(SESSION_MESSAGE_MAX_BYTES + 1)},
        )
        streamed = await client.post(
            SEND,
            content=sized_body(SESSION_MESSAGE_MAX_BYTES + 1),
            headers={"Content-Type": "application/json"},
        )

    assert response.status_code == streamed.status_code == 413
    assert access.started.empty()


async def test_session_send_concurrency_is_bounded():
    assert SESSION_MESSAGE_MAX_CONCURRENT == 4
    app, access = make_app()
    async with client_for(app) as client:
        access.release.clear()
        tasks = [
            asyncio.create_task(client.post(SEND, json=body(client_request_id=str(i))))
            for i in range(4)
        ]
        try:
            async with asyncio.timeout(5):
                entered = {await access.started.get() for _ in tasks}
                assert entered == {"0", "1", "2", "3"}
                overflow = await client.post(SEND, json=body(client_request_id="x"))
            assert overflow.status_code == 429
            # Reads are not admission-limited.
            check = await client.get("/api/v1/sessions/sess-1")
            assert check.status_code == 200
        finally:
            access.release.set()
            await asyncio.gather(*tasks, return_exceptions=True)
        assert [task.result().status_code for task in tasks] == [200] * 4

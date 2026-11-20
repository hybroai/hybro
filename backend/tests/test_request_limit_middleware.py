"""Body-size and admission middleware regressions on a minimal application."""

import asyncio
import json
from contextlib import asynccontextmanager

import httpx
import pytest
from fastapi import FastAPI, UploadFile

from common.middleware.request_concurrency import RequestConcurrencyLimitMiddleware
from common.middleware.request_size import RequestBodyLimitMiddleware

PATH = "/api/v1/limited"
MAX_BYTES = 64 * 1024
MAX_CONCURRENT = 4


def message_body(request_id: str = "request", text: str = "Hello") -> dict:
    return {"client_request_id": request_id, "text": text}


def sized_body(size: int) -> bytes:
    empty = json.dumps(message_body(text="")).encode()
    return json.dumps(message_body(text="x" * (size - len(empty)))).encode()


async def chunks(body: bytes):
    middle = len(body) // 2
    yield body[:middle]
    yield body[middle:]


class ExecutionStub:
    def __init__(self) -> None:
        self.started: asyncio.Queue[str] = asyncio.Queue()
        self.release = asyncio.Event()
        self.release.set()
        self.error = False

    async def send(self, body: dict) -> dict:
        self.started.put_nowait(body["client_request_id"])
        if self.error:
            raise RuntimeError("execution failed")
        await self.release.wait()
        return {"client_request_id": body["client_request_id"]}


def make_app():
    """Mirror the production order: admission wraps the body limit."""
    execution = ExecutionStub()
    app = FastAPI()

    @app.post(PATH)
    async def limited(body: dict):
        return await execution.send(body)

    app.add_middleware(
        RequestBodyLimitMiddleware, path=PATH, method="POST", max_bytes=MAX_BYTES
    )
    app.add_middleware(
        RequestConcurrencyLimitMiddleware, path=PATH, max_concurrent=MAX_CONCURRENT
    )
    return app, execution


def client_for(app):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://hybro.test",
    )


@asynccontextmanager
async def four_blocked_requests(client, execution):
    execution.release.clear()
    tasks = [
        asyncio.create_task(client.post(PATH, json=message_body(str(index))))
        for index in range(4)
    ]
    try:
        async with asyncio.timeout(5):
            entered = {await execution.started.get() for _ in tasks}
        assert entered == {"0", "1", "2", "3"}
        yield tasks
    finally:
        execution.release.set()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.parametrize(
    ("mount", "suffix"), [("", ""), ("", "/"), ("/nested/backend", "//")]
)
async def test_declared_oversize_rejected_before_execution(mount, suffix):
    app, execution = make_app()
    if mount:
        parent = FastAPI()
        parent.mount(mount, app)
        app = parent
    async with client_for(app) as client:
        response = await client.post(
            mount + PATH + suffix,
            content=b"",
            headers={"Content-Length": str(MAX_BYTES + 1)},
        )
    assert response.status_code == 413
    assert execution.started.empty()


@pytest.mark.parametrize("declared_length", [None, "1"])
async def test_streamed_oversize_is_413_not_parser_error(declared_length):
    app, execution = make_app()
    headers = {"Content-Type": "application/json"}
    if declared_length is not None:
        headers["Content-Length"] = declared_length
    async with client_for(app) as client:
        response = await client.post(
            PATH,
            content=chunks(sized_body(MAX_BYTES + 1)),
            headers=headers,
        )
    assert response.status_code == 413
    assert execution.started.empty()


async def test_exact_body_limit_still_executes_and_preserves_request_id():
    app, execution = make_app()
    async with client_for(app) as client:
        response = await client.post(
            PATH,
            content=chunks(sized_body(MAX_BYTES)),
            headers={"Content-Type": "application/json"},
        )
    assert response.status_code == 200
    assert response.json()["client_request_id"] == "request"
    assert execution.started.get_nowait() == "request"


async def test_overload_is_immediate_and_success_restores_admission():
    app, execution = make_app()
    other_app, other_execution = make_app()
    parent = FastAPI()
    parent.mount("/nested", app)
    async with client_for(app) as client, client_for(parent) as nested_client:
        async with four_blocked_requests(client, execution) as admitted:
            # No admitted request is released until the overflow has responded.
            async with asyncio.timeout(5):
                response = await client.post(PATH, json=message_body("overflow"))
                nested = await nested_client.post(
                    "/nested" + PATH + "/", json=message_body("nested-overflow")
                )
                async with client_for(other_app) as independent:
                    isolated = await independent.post(PATH, json=message_body("other"))
            assert response.status_code == nested.status_code == 429
            assert isolated.status_code == 200
            assert other_execution.started.get_nowait() == "other"
            assert all(not task.done() for task in admitted)
            assert execution.started.empty()
        assert [task.result().status_code for task in admitted] == [200] * 4
        response = await client.post(PATH, json=message_body("after-success"))
        assert response.status_code == 200


async def test_cancellation_restores_exactly_one_slot():
    app, execution = make_app()
    async with client_for(app) as client:
        async with four_blocked_requests(client, execution) as admitted:
            admitted[0].cancel()
            with pytest.raises(asyncio.CancelledError):
                await admitted[0]
            replacement = asyncio.create_task(
                client.post(PATH, json=message_body("replacement"))
            )
            admitted.append(replacement)
            async with asyncio.timeout(5):
                assert await execution.started.get() == "replacement"
                overflow = await client.post(PATH, json=message_body("overflow"))
            assert overflow.status_code == 429
        assert replacement.result().status_code == 200


async def test_execution_errors_do_not_leak_slots():
    app, execution = make_app()
    execution.error = True
    async with client_for(app) as client:
        for index in range(4):
            response = await client.post(PATH, json=message_body(f"error-{index}"))
            assert response.status_code == 500
            assert execution.started.get_nowait() == f"error-{index}"
        execution.error = False
        async with four_blocked_requests(client, execution):
            async with asyncio.timeout(5):
                overflow = await client.post(PATH, json=message_body("overflow"))
            assert overflow.status_code == 429


async def test_disconnect_during_body_receive_restores_admission():
    app, execution = make_app()
    incoming = iter(
        [
            {"type": "http.request", "body": b'{"text":', "more_body": True},
            {"type": "http.disconnect"},
        ]
    )
    sent = []

    async def receive():
        return next(incoming)

    async def send(message):
        sent.append(message)

    await app(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": PATH,
            "raw_path": PATH.encode(),
            "query_string": b"",
            "root_path": "",
            "headers": [(b"content-type", b"application/json")],
            "server": ("hybro.test", 80),
            "client": ("127.0.0.1", 1234),
        },
        receive,
        send,
    )
    assert execution.started.empty()
    async with client_for(app) as client:
        async with four_blocked_requests(client, execution):
            async with asyncio.timeout(5):
                overflow = await client.post(PATH, json=message_body("overflow"))
            assert overflow.status_code == 429


async def test_admission_includes_incomplete_bodies_and_rejection_frees_slot():
    app, execution = make_app()
    receiving = asyncio.Queue()
    finish = asyncio.Event()

    async def incomplete_body():
        receiving.put_nowait(True)
        await finish.wait()
        yield sized_body(MAX_BYTES + 1)

    async with client_for(app) as client:
        requests = [
            asyncio.create_task(
                client.post(
                    PATH,
                    content=incomplete_body(),
                    headers={"Content-Type": "application/json"},
                )
            )
            for _ in range(4)
        ]
        try:
            async with asyncio.timeout(5):
                for _ in requests:
                    await receiving.get()
                overflow = await client.post(PATH, json=message_body("overflow"))
            assert overflow.status_code == 429
            assert execution.started.empty()
        finally:
            finish.set()
            responses = await asyncio.gather(*requests)
        assert [response.status_code for response in responses] == [413] * 4
        async with four_blocked_requests(client, execution):
            pass


async def test_limits_do_not_intercept_other_methods_or_neighbor_paths():
    app, execution = make_app()

    @app.get(PATH)
    @app.post(PATH + "/other")
    async def unaffected():
        return {"unaffected": True}

    async with client_for(app) as client:
        async with four_blocked_requests(client, execution):
            for method, path in [("GET", PATH), ("POST", PATH + "/other")]:
                response = await client.request(
                    method,
                    path,
                    content=b"",
                    headers={"Content-Length": str(MAX_BYTES + 1)},
                )
                assert response.status_code == 200
                assert response.json() == {"unaffected": True}


@pytest.mark.parametrize("multipart", [False, True])
async def test_existing_json_and_upload_parsers_keep_streamed_body_limits(multipart):
    app = FastAPI()
    calls = []
    path = "/files/upload" if multipart else "/internal/llm/chat/completions"
    app.add_middleware(RequestBodyLimitMiddleware, path=path, max_bytes=1024)

    if multipart:

        @app.post(path)
        async def upload(file: UploadFile):
            content = await file.read()
            calls.append(content)
            return {"bytes": len(content)}

        content_type = "multipart/form-data; boundary=fixture"
        prefix = (
            b'--fixture\r\nContent-Disposition: form-data; name="file"; '
            b'filename="image.png"\r\nContent-Type: image/png\r\n\r\n'
        )
        suffix = b"\r\n--fixture--\r\n"
    else:

        @app.post(path)
        async def chat(body: dict):
            calls.append(body)
            return {"bytes": len(body["text"])}

        content_type = "application/json"
        prefix, suffix = b'{"text":"', b'"}'

    async with client_for(app) as client:
        rejected = await client.post(
            path,
            content=chunks(prefix + b"x" * 1024 + suffix),
            headers={"Content-Type": content_type},
        )
        assert rejected.status_code == 413
        assert calls == []
        accepted = await client.post(
            path,
            content=chunks(prefix + b"hello" + suffix),
            headers={"Content-Type": content_type},
        )
        assert accepted.status_code == 200
        assert accepted.json() == {"bytes": 5}

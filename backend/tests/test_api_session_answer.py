"""``POST /sessions/{session_id}/questions/{question_id}/answer``.

The route is thin: a strict body, fixed error text, and the session's codes.
"""

from __future__ import annotations

import pytest

from common.errors import AppError
from tests.test_api_sessions import client_for, make_app

ANSWER = "/api/v1/sessions/sess-1/questions/int-1/answer"


@pytest.mark.parametrize("answer", ["approve", "refuse"])
async def test_an_answer_reaches_the_session(answer):
    app, access = make_app()
    async with client_for(app) as client:
        response = await client.post(ANSWER, json={"answer": answer})

    assert response.status_code == 200
    assert response.json() == {
        "session_id": "sess-1",
        "question_id": "int-1",
        "answer": answer,
        "status": "accepted",
    }
    assert access.answered == [("sess-1", "int-1", answer)]


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"answer": "Approve"},
        {"answer": "yes"},
        {"answer": True},
        {"answer": ["approve"]},
        {"answer": "approve", "reason": "secret-reason-text"},
        {"decision": "approve"},
    ],
)
async def test_a_bad_body_is_a_422_that_names_the_field(payload):
    app, access = make_app()
    async with client_for(app) as client:
        response = await client.post(ANSWER, json=payload)

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["code"] == "invalid_request"
    assert detail["fields"] == ["answer"]
    assert "secret-reason-text" not in response.text
    assert "yes" not in response.text.replace("only", "")
    assert access.answered == []


async def test_a_body_that_is_not_json_is_a_422():
    app, access = make_app()
    async with client_for(app) as client:
        response = await client.post(
            ANSWER,
            content=b"answer=approve",
            headers={"content-type": "application/json"},
        )

    assert response.status_code == 422
    assert response.json()["detail"]["fields"] == ["answer"]
    assert access.answered == []


@pytest.mark.parametrize(
    ("code", "status"),
    [
        ("answer_not_granted", 403),
        ("question_owner_only", 403),
        ("session_not_found", 404),
        ("session_closed", 409),
        ("question_not_pending", 409),
        ("answer_failed", 502),
        ("clients_unavailable", 503),
        ("answer_unconfirmed", 503),
    ],
)
async def test_session_errors_keep_their_code(code, status):
    app, access = make_app()
    access.error = AppError("Fixed text.", code=code)
    async with client_for(app) as client:
        response = await client.post(ANSWER, json={"answer": "approve"})

    assert response.status_code == status
    assert response.json()["detail"] == {
        "code": code,
        "message": "Fixed text.",
        "session_id": "sess-1",
        "question_id": "int-1",
    }


async def test_the_route_is_in_the_schema_with_its_body():
    app, _ = make_app()
    schema = app.openapi()

    operation = schema["paths"][
        "/api/v1/sessions/{session_id}/questions/{question_id}/answer"
    ]["post"]
    assert set(operation["responses"]) >= {"200", "403", "404", "409", "422", "503"}
    body = schema["components"]["schemas"]["SessionQuestionAnswer"]
    assert body["properties"]["answer"]["enum"] == ["approve", "refuse"]
    assert body["additionalProperties"] is False
    status = schema["components"]["schemas"]["SessionStatus"]
    assert "question" in status["properties"]


@pytest.mark.parametrize(
    ("path", "field"),
    [
        (f"/api/v1/sessions/sess-1/questions/{'q' * 300}/answer", "question_id"),
        (f"/api/v1/sessions/{'s' * 300}/questions/int-1/answer", "session_id"),
    ],
)
async def test_a_bad_path_parameter_names_that_parameter(path, field):
    app, access = make_app()
    async with client_for(app) as client:
        response = await client.post(path, json={"answer": "approve"})

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["fields"] == [field]
    assert field in detail["message"]
    assert "q" * 300 not in response.text and "s" * 300 not in response.text
    assert access.answered == []

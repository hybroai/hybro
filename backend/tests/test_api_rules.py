"""The owner's /rules routes: the check rules, in order. Not enforced."""

from __future__ import annotations

import httpx
import pytest

from account.protocols import OWNER_USER_ID
from api_gateway.owner_session import get_current_user
from common.auth import ClerkUser
from main import create_app
from policy import PolicyService
from tests.fakes.account import build_service, sign_out, store_with_owner
from tests.fakes.policy import MemoryRuleStore

API = "/api/v1"
RULES = f"{API}/rules"


def make_app():
    app = create_app()
    app.state.policy_rules = PolicyService(MemoryRuleStore())
    app.state.owner_account = build_service(
        store=store_with_owner(OWNER_USER_ID), auth_mode="mock"
    )
    app.dependency_overrides[get_current_user] = lambda: ClerkUser(
        user_id=OWNER_USER_ID, session_id="s", claims={"name": "Ada"}
    )
    return app


def client_for(app):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://hybro.test",
    )


async def test_the_owner_creates_lists_updates_reorders_and_deletes_rules():
    app = make_app()
    async with client_for(app) as client:
        empty = await client.get(RULES)
        first = await client.post(RULES, json={"agent_id": "coder", "answer": "deny"})
        second = await client.post(RULES, json={"answer": "allow", "enabled": False})
        a, b = first.json()["rule_id"], second.json()["rule_id"]
        updated = await client.put(
            f"{RULES}/{a}",
            json={"agent_id": "reviewer", "answer": "allow", "enabled": True},
        )
        reordered = await client.put(f"{RULES}/order", json={"rule_ids": [b, a]})
        listing = await client.get(RULES)
        deleted = await client.delete(f"{RULES}/{b}")
        after = await client.get(RULES)

    assert empty.status_code == 200 and empty.json() == {"rules": []}
    assert first.status_code == 201
    assert first.json()["agent_id"] == "coder"
    assert first.json()["answer"] == "deny"
    assert first.json()["enabled"] is True
    assert first.json()["kind"] == "check"
    assert second.json()["agent_id"] is None
    assert second.json()["enabled"] is False
    assert updated.status_code == 200
    assert (updated.json()["agent_id"], updated.json()["answer"]) == (
        "reviewer",
        "allow",
    )
    assert reordered.status_code == 200
    assert [r["rule_id"] for r in reordered.json()["rules"]] == [b, a]
    assert [r["rule_id"] for r in listing.json()["rules"]] == [b, a]
    assert deleted.status_code == 204
    assert [r["rule_id"] for r in after.json()["rules"]] == [a]


async def test_unknown_rules_are_refused():
    app = make_app()
    async with client_for(app) as client:
        updated = await client.put(
            f"{RULES}/rule_nope",
            json={"agent_id": None, "answer": "deny", "enabled": True},
        )
        deleted = await client.delete(f"{RULES}/rule_nope")

    for response in (updated, deleted):
        assert response.status_code == 404
        assert response.json()["detail"]["code"] == "rule_not_found"


async def test_an_order_that_does_not_name_every_rule_once_is_refused():
    app = make_app()
    async with client_for(app) as client:
        created = await client.post(RULES, json={"answer": "deny"})
        await client.post(RULES, json={"answer": "allow"})
        response = await client.put(
            f"{RULES}/order", json={"rule_ids": [created.json()["rule_id"]]}
        )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "rule_order_mismatch"


async def test_create_is_refused_at_the_limit(monkeypatch):
    import policy.service

    monkeypatch.setattr(policy.service, "MAX_RULES", 1)
    app = make_app()
    async with client_for(app) as client:
        await client.post(RULES, json={"answer": "deny"})
        response = await client.post(RULES, json={"answer": "deny"})

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "rule_limit_reached"


@pytest.mark.parametrize(
    ("body", "field"),
    [
        ({"answer": "ask"}, "answer"),
        ({"answer": "secret-answer-value"}, "answer"),
        ({}, "answer"),
        ({"answer": "deny", "agent_id": "   "}, "agent_id"),
        ({"answer": "deny", "secret-key": "secret-extra-value"}, "unknown field"),
        ({"answer": "deny", "enabled": "secret-yes"}, "enabled"),
        ({"answer": "deny", "rule_ids": ["secret-id"]}, "unknown field"),
    ],
)
async def test_an_invalid_rule_names_the_field_without_its_value(body, field):
    app = make_app()
    async with client_for(app) as client:
        response = await client.post(RULES, json=body)

    assert response.status_code == 422
    assert response.json()["detail"]["fields"] == [field]
    assert "secret-" not in response.text


async def test_the_rules_need_the_owners_sign_in(monkeypatch):
    app = make_app()
    sign_out(app, monkeypatch)
    async with client_for(app) as client:
        response = await client.get(RULES)

    assert response.status_code == 401


async def test_the_rules_are_unavailable_without_the_service():
    app = make_app()
    del app.state.policy_rules
    async with client_for(app) as client:
        response = await client.get(RULES)

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "rules_unavailable"


async def test_a_body_that_is_not_json_is_reported_as_the_body():
    app = make_app()
    async with client_for(app) as client:
        response = await client.post(
            RULES,
            content=b'{"answer":',
            headers={"Content-Type": "application/json"},
        )

    assert response.status_code == 422
    assert response.json()["detail"]["fields"] == ["body"]


async def test_an_order_may_name_more_rules_than_create_allows():
    # Concurrent creates can pass the cap; the order must still name them all.
    from policy.models import MAX_RULES

    app = make_app()
    async with client_for(app) as client:
        response = await client.put(
            f"{RULES}/order",
            json={"rule_ids": [f"rule_{i}" for i in range(MAX_RULES + 1)]},
        )

    # Validation accepts it; the service refuses it only because no such rules
    # exist.
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "rule_order_mismatch"


@pytest.mark.parametrize("missing", ["agent_id", "answer", "enabled"])
async def test_a_replace_that_leaves_a_field_out_is_refused(missing):
    app = make_app()
    async with client_for(app) as client:
        created = await client.post(
            RULES, json={"agent_id": "coder", "answer": "deny", "enabled": False}
        )
        rule_id = created.json()["rule_id"]
        body = {"agent_id": "coder", "answer": "allow", "enabled": False}
        del body[missing]
        response = await client.put(f"{RULES}/{rule_id}", json=body)
        listing = await client.get(RULES)

    assert response.status_code == 422
    assert response.json()["detail"]["fields"] == [missing]
    # The stored rule is unchanged: still off, still for one agent.
    assert listing.json()["rules"][0]["enabled"] is False
    assert listing.json()["rules"][0]["agent_id"] == "coder"
    assert listing.json()["rules"][0]["answer"] == "deny"

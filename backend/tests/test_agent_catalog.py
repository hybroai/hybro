"""AgentCatalogService serves the /agent/* routes over the agent facade."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from a2a_adapter.card_data import build_agent_card
from a2a_adapter.translators import a2a_card_to_snapshot
from agent.catalog import AgentCatalogService
from agent.facade import AgentRegistrationConflict
from agent.liveness import AgentLivenessService
from common.dto import AgentCardSnapshot, AgentInfo
from models.agent import AgentStatus
from tests.fakes.a2a_v1 import make_v03_card_json


def _info(**overrides) -> AgentInfo:
    values = {
        "agent_id": "a1",
        "name": "Weather",
        "url": "https://private.example/a2a",
        "provider_id": "owner",
        "raw_card": {
            "name": "Weather",
            "url": "https://private.example/a2a",
            "provider": {"organization": "Acme"},
        },
    }
    values.update(overrides)
    return AgentInfo(**values)


async def _unchanged(info: AgentInfo) -> AgentInfo:
    return info


def _catalog(facade, liveness=_unchanged) -> AgentCatalogService:
    return AgentCatalogService(facade, liveness_checker=liveness)


async def test_register_returns_view_without_transport_url():
    facade = MagicMock(register_agent=AsyncMock(return_value=_info()))

    response = await _catalog(facade).register(
        agent_url="https://private.example", provider_id="owner"
    )

    assert response.success is True
    assert response.agent.agent_id == "a1"
    assert "private.example" not in response.model_dump_json()
    facade.register_agent.assert_awaited_once_with("https://private.example", "owner")


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (AgentRegistrationConflict("owned_by_another_provider", "taken"), 409),
        (ValueError("agent card could not resolve"), 500),
        (RuntimeError("database down"), 500),
    ],
)
async def test_register_maps_failures_to_status(error, status):
    facade = MagicMock(register_agent=AsyncMock(side_effect=error))

    response = await _catalog(facade).register(agent_url="u", provider_id="owner")

    assert (response.success, response.status_code) == (False, status)
    assert response.error == str(error)
    assert response.error_code == getattr(error, "code", None)


async def test_provider_name_is_resolved_only_without_card_organization(
    monkeypatch,
):
    monkeypatch.setattr("agent.catalog.resolve_provider_name", lambda _id: "Owner")
    facade = MagicMock(
        list_agents=AsyncMock(
            return_value=[_info(), _info(agent_id="a2", raw_card={"name": "Bare"})]
        )
    )

    response = await _catalog(facade).list_for_provider(provider_id="owner")

    assert [view.provider_name for view in response.agents] == [None, "Owner"]
    facade.list_agents.assert_awaited_once_with("owner")


@pytest.mark.parametrize(
    ("info", "user_id"),
    [
        (None, "owner"),
        (_info(is_public=False), None),
        (_info(is_public=False), "someone-else"),
    ],
)
async def test_get_visible_hides_missing_and_private_agents(info, user_id):
    facade = MagicMock(get_agent=AsyncMock(return_value=info))
    liveness = AsyncMock()

    response = await _catalog(facade, liveness).get_visible(
        agent_id="a1", user_id=user_id
    )

    assert (response.success, response.status_code) == (False, 404)
    liveness.assert_not_awaited()


async def test_get_visible_returns_private_agent_to_owner_after_liveness():
    facade = MagicMock(get_agent=AsyncMock(return_value=_info(is_public=False)))

    async def mark_inactive(info: AgentInfo) -> AgentInfo:
        return info.model_copy(update={"status": "inactive"})

    response = await _catalog(facade, mark_inactive).get_visible(
        agent_id="a1", user_id="owner"
    )

    assert response.success is True
    assert response.agent.status == "inactive"


async def test_list_visible_passes_scope_and_reports_errors():
    facade = MagicMock(list_visible_agents=AsyncMock(return_value=[_info()]))

    response = await _catalog(facade).list_visible(user_id="owner", active_only=True)

    assert [view.agent_id for view in response.agents] == ["a1"]
    facade.list_visible_agents.assert_awaited_once_with(
        user_id="owner", active_only=True
    )

    facade.list_visible_agents.side_effect = RuntimeError("down")
    failed = await _catalog(facade).list_visible(user_id=None)
    assert (failed.success, failed.status_code) == (False, 500)


@pytest.mark.parametrize(
    ("existing", "deleted", "status"),
    [
        (None, False, 404),
        (_info(provider_id="other"), False, 403),
        (_info(), False, 404),
        (_info(), True, 200),
    ],
)
async def test_delete_checks_existence_and_ownership(existing, deleted, status):
    facade = MagicMock(
        get_agent=AsyncMock(return_value=existing),
        delete_agent=AsyncMock(return_value=deleted),
    )

    response = await _catalog(facade).delete(agent_id="a1", provider_id="owner")

    assert response.status_code == status
    assert response.success is (status == 200)


async def test_resolve_card_returns_card_view_or_404():
    snapshot = AgentCardSnapshot(
        agent_id="", url="https://private.example", raw_card={"name": "Weather"}
    )
    facade = MagicMock(resolve_agent_card_from_url=AsyncMock(return_value=snapshot))

    found = await _catalog(facade).resolve_card(agent_url="https://private.example")
    facade.resolve_agent_card_from_url.return_value = None
    missing = await _catalog(facade).resolve_card(agent_url="https://private.example")

    assert found.card.name == "Weather"
    assert "private.example" not in found.model_dump_json()
    assert (missing.success, missing.status_code) == (False, 404)


def _health(is_healthy: bool, card=None) -> MagicMock:
    return MagicMock(
        check_agent_health=AsyncMock(return_value=(is_healthy, card)),
        sync_agent_card=AsyncMock(),
        update_agent_status=AsyncMock(return_value=True),
        reactivate_agent=AsyncMock(return_value=True),
    )


async def test_liveness_marks_unreachable_cloud_agent_inactive():
    health = _health(False)

    result = await AgentLivenessService(health_service=health)(_info())

    assert result.status == "inactive"
    health.check_agent_health.assert_awaited_once()
    assert health.check_agent_health.await_args.args[:2] == (
        "a1",
        "https://private.example/a2a",
    )
    health.update_agent_status.assert_awaited_once_with("a1", AgentStatus.inactive)


async def test_liveness_recovers_and_syncs_card_for_reachable_agent():
    fetched = object()
    health = _health(True, fetched)

    result = await AgentLivenessService(health_service=health)(_info(status="inactive"))

    assert result.status == "active"
    health.sync_agent_card.assert_awaited_once_with("a1", _info().raw_card, fetched)
    health.reactivate_agent.assert_awaited_once_with("a1")


async def test_liveness_skips_local_agents():
    health = _health(False)
    info = _info(source="local")

    assert await AgentLivenessService(health_service=health)(info) is info
    health.check_agent_health.assert_not_awaited()


@pytest.mark.parametrize(
    "card_json",
    [
        make_v03_card_json(url="https://legacy.example/a2a"),
        make_v03_card_json(
            url="https://legacy.example/a2a",
            supportedInterfaces=[
                {
                    "url": "https://legacy.example/v03",
                    "protocolBinding": "JSONRPC",
                    "protocolVersion": "0.3.0",
                },
                {
                    "url": "https://legacy.example/v1",
                    "protocolBinding": "JSONRPC",
                    "protocolVersion": "1.0",
                },
            ],
        ),
    ],
    ids=["v03", "mixed"],
)
async def test_v03_and_mixed_cards_register_and_preview(card_json):
    snapshot = a2a_card_to_snapshot(
        build_agent_card(card_json), "https://legacy.example"
    )
    info = _info(url=snapshot.url, raw_card=snapshot.raw_card)
    facade = MagicMock(
        register_agent=AsyncMock(return_value=info),
        resolve_agent_card_from_url=AsyncMock(return_value=snapshot),
    )

    registered = await _catalog(facade).register(
        agent_url="https://legacy.example", provider_id="owner"
    )
    previewed = await _catalog(facade).resolve_card(agent_url="https://legacy.example")

    assert registered.success is True
    assert registered.agent.name == "Legacy Agent"
    assert previewed.success is True
    assert previewed.card.name == "Legacy Agent"
    assert [skill.id for skill in previewed.card.skills] == ["echo"]
    assert previewed.card.capabilities.streaming is True

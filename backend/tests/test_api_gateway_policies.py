import json
from pathlib import Path

FIXTURE = Path(__file__).parent / "fixtures" / "api_gateway_policy_matrix.json"


def _fixture_matrix():
    return json.loads(FIXTURE.read_text())


def test_route_policy_matrix_matches_fixture():
    from api_gateway.policies import ROUTE_POLICIES

    current = {
        name: {
            "auth": policy.auth,
            "cors": policy.cors,
            "api_key": policy.api_key,
            "tags": list(policy.tags),
            **({"deprecated": policy.deprecated} if policy.deprecated else {}),
        }
        for name, policy in ROUTE_POLICIES.items()
    }

    assert current == _fixture_matrix()


def test_open_cors_groups_are_explicitly_limited():
    from api_gateway.policies import open_cors_groups

    assert open_cors_groups() == frozenset({"discovery", "platform_gateway"})


def test_every_public_route_group_has_policy():
    from api_gateway.policies import ROUTE_POLICIES
    from api_gateway.registry import route_group_for_path
    from main import app

    missing = set()
    for route in app.routes:
        path = getattr(route, "path", "")
        if not path.startswith("/api/"):
            continue
        group = route_group_for_path(path)
        if group not in ROUTE_POLICIES:
            missing.add((path, group))

    assert missing == set()


def test_route_group_matching_is_segment_bounded():
    from api_gateway.registry import route_group_for_path

    assert route_group_for_path("/api/v1/local-agents/discovery") == "agent"
    assert route_group_for_path("/api/v1/agent/getAgent/abc") == "agent"
    assert route_group_for_path("/api/v1/agentGroups") == "agent_group"
    assert route_group_for_path("/api/v1/rooms/abc/a2a-tasks") == "a2a_task"

    assert route_group_for_path("/api/v1/local-agents-something") == "unknown"
    assert route_group_for_path("/api/v1/agentGroups-v2") == "unknown"
    assert route_group_for_path("/api/v1/agentish") == "unknown"


def test_route_tags_follow_policy_matrix():
    from api_gateway.policies import ROUTE_POLICIES
    from api_gateway.registry import route_group_for_path
    from main import app

    bad = []
    for route in app.routes:
        path = getattr(route, "path", "")
        if not path.startswith("/api/"):
            continue
        group = route_group_for_path(path)
        policy = ROUTE_POLICIES[group]
        tags = set(getattr(route, "tags", []) or [])
        if not set(policy.tags).issubset(tags):
            bad.append((path, group, sorted(tags), list(policy.tags)))

    assert bad == []

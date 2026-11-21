"""The container binds the owner's check rules and hands them to execution."""

from __future__ import annotations

import inspect
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import container
from policy.models import RuleCreate
from policy.protocols import RuleRegistry
from tests.test_module_boundaries import imported_modules


async def test_create_policy_rules_binds_the_rules_collection():
    collection = MagicMock()
    collection.find = AsyncMock(return_value=[])
    collection.insert_one = AsyncMock(return_value="id")
    names: list[str] = []
    mongo = SimpleNamespace(collection=lambda name: names.append(name) or collection)

    rules = container.create_policy_rules(mongo=mongo)

    assert isinstance(rules, RuleRegistry)
    assert names == ["rules"]
    created = await rules.create(RuleCreate(answer="deny"))
    assert collection.insert_one.await_args.args[0]["rule_id"] == created.rule_id


def test_lifespan_binds_and_resets_the_rules():
    source = inspect.getsource(container._runtime_lifespan)

    assert source.count("app.state.policy_rules = create_policy_rules(") == 1
    assert source.count("app.state.policy_rules = None") == 2


def test_the_rule_repository_creates_its_indexes_with_the_other_owners():
    source = inspect.getsource(container.ensure_runtime_indexes)

    assert "await MongoRuleRepository(mongo).ensure_indexes()" in source


def test_only_the_container_and_the_rule_routes_import_policy():
    # Execution reads the rules through its own port; only the container
    # wires the two together.
    backend = Path(container.__file__).parent
    importers = set()
    for path in backend.rglob("*.py"):
        relative = path.relative_to(backend)
        if relative.parts[0] in {"policy", "tests", ".venv"}:
            continue
        for module in imported_modules(path, root=backend):
            if module.split(".", 1)[0] == "policy":
                importers.add((relative.as_posix(), module))
    assert importers == {
        ("container.py", "policy"),
        ("api_gateway/routes/rule_routes.py", "policy.protocols"),
    }


def test_policy_imports_only_common_and_libraries():
    import sys

    backend = Path(container.__file__).parent
    allowed = {"policy", "common", "pydantic", "pymongo", "__future__"}
    found = set()
    for path in (backend / "policy").rglob("*.py"):
        for module in imported_modules(path, root=backend):
            root = module.split(".", 1)[0]
            if root not in allowed and root not in sys.stdlib_module_names:
                found.add((path.relative_to(backend).as_posix(), module))
    assert found == set()


def test_the_runtime_checks_new_agent_calls_against_the_rules():
    import orchestrator_composition

    lifespan = inspect.getsource(container._runtime_lifespan)
    compose = inspect.getsource(orchestrator_composition.create_orchestrator_runtime)

    assert lifespan.count("agent_call_check=app.state.policy_rules,") == 1
    assert lifespan.index("app.state.policy_rules = create_policy_rules(") < (
        lifespan.index("agent_call_check=app.state.policy_rules,")
    )
    assert compose.count("call_check=agent_call_check,") == 1


def test_the_rules_implement_the_execution_call_check_port():
    from execution.orchestrator.a2a_runtime.ports import AgentCallCheckPort

    rules = container.create_policy_rules(
        mongo=SimpleNamespace(collection=lambda _name: MagicMock())
    )
    assert isinstance(rules, AgentCallCheckPort)

"""Plan 4 pre-cutover contracts pinned before any production wiring lands.

These invariants stay true while the orchestrator runtime is production-unbound
and are the successor spine of the architecture gate in
``test_orchestrator_architecture.py``. That gate will be replaced by
coexistence/routing invariants (persisted ownership routing, flag-off zero
traffic, no mid-run owner switching) in the same change that wires
``container.py``.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest
from pydantic import ValidationError

from execution.orchestrator.models import OrchestratorRunState
from execution.orchestrator.profiles import ProfileConfiguration
from execution.repository.orchestrator.artifacts import (
    RoomFilesEpochFencedArtifactOwner,
)
from execution.repository.orchestrator.event_store import MongoOrchestratorEventStore
from tests.orchestrator_harness.in_memory import InMemoryOrchestratorEventStore

from ._orchestrator_helpers import make_run

ROOT = Path(__file__).parents[1]
ORCHESTRATOR = ROOT / "execution" / "orchestrator"
MODEL_LOOP = ROOT / "execution" / "model_loop"


def test_run_runtime_generation_is_persisted_and_immutable_by_schema():
    run = make_run()
    assert run.schema_version == 6
    assert run.lifecycle_family == "canonical"
    assert run.runtime_generation == "orchestrator"

    payload = run.model_dump(mode="json")
    payload["runtime_generation"] = "legacy"
    with pytest.raises(ValidationError):
        OrchestratorRunState.model_validate(payload)

    payload = run.model_dump(mode="json")
    payload["schema_version"] = 4
    with pytest.raises(ValidationError):
        OrchestratorRunState.model_validate(payload)


def test_initial_routing_and_finalization_are_reserved_not_consumed():
    # Frozen per Run but not yet consumed by any production code path. The
    # production composition pins `explicit_agent_first` and `pass_through`;
    # `model_select` and `synthesize` are deferred product capabilities. When
    # either field becomes consumed, update this test deliberately.
    assert set(
        ProfileConfiguration.model_fields["initial_routing"].annotation.__args__
    ) == {  # type: ignore[union-attr]
        "explicit_agent_first",
        "model_select",
    }
    assert set(
        ProfileConfiguration.model_fields["finalization"].annotation.__args__
    ) == {  # type: ignore[union-attr]
        "pass_through",
        "light",
        "synthesize",
    }
    consumers = [
        ORCHESTRATOR / "kernel.py",
        ORCHESTRATOR / "run_state.py",
        ORCHESTRATOR / "session.py",
        MODEL_LOOP / "context.py",
        ORCHESTRATOR / "a2a_runtime" / "catalog_assembler.py",
    ]
    for path in consumers:
        source = path.read_text()
        assert "profile.initial_routing" not in source, path
        assert "profile.finalization" not in source, path
        assert "initial_routing=" not in source, path
        assert "finalization=" not in source, path


def test_artifact_durable_identity_strings_are_pinned():
    # Both strings are durable operational/data identity, not architecture
    # branding: the write-lease owner must stay stable so old and new replicas
    # contend on the same Room lease, and the origin-key namespace participates
    # in the artifact idempotency preimage. The Plan 4 naming cleanup must not
    # rename them; this test fails loudly if it does.
    source = inspect.getsource(RoomFilesEpochFencedArtifactOwner)
    assert "orchestrator-v3-a2a-artifact" in source

    artifacts = (
        ROOT / "execution" / "repository" / "orchestrator" / "artifacts.py"
    ).read_text()
    assert '"orchestrator-v3-a2a"' in artifacts


def test_event_store_port_has_memory_and_mongo_implementations():
    implementations = [
        InMemoryOrchestratorEventStore,
        MongoOrchestratorEventStore,
    ]
    for implementation in implementations:
        assert {"append", "read"} <= {
            name
            for name, member in implementation.__dict__.items()
            if inspect.isfunction(member) and not name.startswith("_")
        }

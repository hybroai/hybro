from __future__ import annotations

from execution.orchestrator.a2a_runtime.catalog import FrozenToolCatalog
from execution.orchestrator.a2a_runtime.catalog_assembler import (
    AgentToolCatalogAssembler,
    agent_tool_input_schema,
    deterministic_tool_name,
)
from execution.orchestrator.a2a_runtime.models import AgentToolCandidate
from execution.orchestrator.a2a_runtime.preparation import (
    RunPreparedInvocationSnapshotReader,
)
from execution.orchestrator.models import (
    AuthorizationBasis,
    CandidateScopeSnapshot,
    PreparedResourceRef,
    RunResourceManifestSnapshot,
)
from tests.orchestrator_harness.a2a_in_memory import (
    InMemoryAgentToolBindingStore,
    InMemoryRoomEpochStore,
)
from tests.orchestrator_harness.in_memory import InMemoryOrchestratorRunStore

from ._orchestrator_a2a_helpers import invocation
from ._orchestrator_helpers import NOW, make_run


class Candidates:
    def __init__(self, values):
        self.values = values
        self.calls = 0

    async def list_candidates(self, **kwargs):
        self.calls += 1
        assert kwargs["requesting_subject_id"] == "user-1"
        return list(self.values)


def candidate(agent_id="agent-1", **updates):
    values = {
        "agent_id": agent_id,
        "agent_display_name": "Broker Agent",
        "display_name": "Broker Agent - Place Policy",
        "description": "Places insurance",
        "card_digest": "card-1",
        "endpoint_scope": "https://agent.example/a2a",
        "endpoint_scope_digest": "endpoint-1",
        "transport_kind": "direct",
        "input_modes": ["text", "application/pdf"],
    }
    values.update(updates)
    return AgentToolCandidate(**values)


async def prepare(values, *, resource_manifest=None):
    epochs = InMemoryRoomEpochStore()
    assert (await epochs.activate("room-1", "create-1", activated_at=NOW))[
        0
    ] == "accepted"
    bindings = InMemoryAgentToolBindingStore()
    source = Candidates(values)
    assembler = AgentToolCatalogAssembler(
        candidate_source=source,
        binding_store=bindings,
        room_epoch_store=epochs,
    )
    manifest = resource_manifest or RunResourceManifestSnapshot(
        manifest_id="resources",
        refs=[
            PreparedResourceRef(
                ref_id="artifact-1",
                kind="artifact",
                source_message_id="message-1",
                mime_type="application/pdf",
                size_bytes=10,
                content_digest="digest-1",
            )
        ],
        content_digest="manifest-digest",
    )
    prepared = await assembler.prepare(
        run_id="run-1",
        conversation_id="room-1",
        room_epoch=1,
        requesting_subject_id="user-1",
        candidate_scope=CandidateScopeSnapshot(
            snapshot_id="scope-1",
            revision=2,
            source="test",
            conversation_id="room-1",
            agent_ids=[item.agent_id for item in values],
            authorization_basis=AuthorizationBasis(kind="room_member"),
        ),
        resource_manifest=manifest,
        authorization_basis_digest="auth-basis",
        created_at=NOW,
    )
    return prepared, source, bindings


def test_tool_name_is_provider_safe_stable_and_private():
    first = deterministic_tool_name("private-owner/agent", "skill with spaces")
    second = deterministic_tool_name("private-owner/agent", "skill with spaces")
    assert first == second
    assert first.startswith("agent_")
    assert len(first) <= 64
    assert "private" not in first
    assert set(first) <= set("abcdefghijklmnopqrstuvwxyz0123456789_-")


async def test_async_assembler_filters_candidates_and_persists_private_bindings():
    prepared, source, store = await prepare(
        [
            candidate(),
            candidate("agent-2", authorized=False),
            candidate("agent-3", excluded=True),
        ]
    )
    assert source.calls == 1
    assert len(prepared.snapshot.entries) == 1
    assert len(prepared.bindings) == 1
    persisted = await store.load(prepared.bindings[0].binding_id)
    assert persisted == prepared.bindings[0]
    assert persisted is not None
    assert persisted.agent_display_name == "Broker Agent"
    assert persisted.definition.label == "Broker Agent - Place Policy"
    schema = prepared.snapshot.entries[0].definition.input_schema
    # The tool description carries the agent's I/O contract so the kernel can
    # match capabilities instead of relying on a one-line blurb.
    description = prepared.snapshot.entries[0].definition.description
    assert "Places insurance" in description
    assert "Input: text, application/pdf" in description
    assert "agent_id" not in schema["properties"]
    assert schema["properties"]["artifact_refs"]["items"]["enum"] == ["artifact-1"]


def test_input_schema_omits_ref_fields_when_no_resources_available():
    """A free-form ref field invites the model to invent reference ids, which
    authorization then rejects. With no resources the schema exposes only
    ``task`` so the model inlines facts instead."""
    schema = agent_tool_input_schema([], [], [])
    assert set(schema["properties"]) == {"task"}
    assert "context_refs" not in schema["properties"]
    assert "artifact_refs" not in schema["properties"]
    assert "attachment_refs" not in schema["properties"]


def test_input_schema_bounds_ref_fields_to_available_resources():
    schema = agent_tool_input_schema([], ["artifact-1", "artifact-2"], ["file:att-1"])
    assert schema["properties"]["artifact_refs"]["items"]["enum"] == [
        "artifact-1",
        "artifact-2",
    ]
    assert schema["properties"]["attachment_refs"]["items"]["enum"] == ["file:att-1"]
    # A family with no refs stays omitted so the model cannot invent ids.
    assert "context_refs" not in schema["properties"]


async def test_frozen_catalog_is_synchronous_and_run_bound():
    prepared, _, _ = await prepare([candidate()])
    run = make_run().model_copy(update={"tool_catalog": prepared.snapshot})
    catalog = FrozenToolCatalog(prepared.snapshot)
    assert catalog.list_tools(run) == [prepared.snapshot.entries[0].definition]
    resolved = catalog.resolve(run, prepared.snapshot.entries[0].definition.name)
    assert resolved.binding == prepared.snapshot.entries[0].binding


async def test_live_artifact_schema_is_identical_for_listing_and_resolution():
    prepared, _, _ = await prepare(
        [candidate(input_modes=["text", "application/json"])]
    )
    run = make_run().model_copy(
        update={
            "tool_catalog": prepared.snapshot,
            "resource_manifest": RunResourceManifestSnapshot(
                manifest_id="live-resources",
                refs=[
                    PreparedResourceRef(
                        ref_id="art_inline",
                        kind="artifact",
                        source_message_id="observation-1",
                        mime_type="application/json",
                        size_bytes=42,
                        content_digest="inline-digest",
                    )
                ],
                content_digest="live-manifest-digest",
            ),
        }
    )
    catalog = FrozenToolCatalog(prepared.snapshot)
    listed = catalog.list_tools(run)[0]
    resolved = catalog.resolve(run, listed.name)

    assert listed.input_schema == resolved.definition.input_schema
    assert listed.input_schema["properties"]["artifact_refs"]["items"]["enum"] == [
        "art_inline"
    ]


async def test_card_or_endpoint_change_changes_binding_digest_only_for_new_run():
    first, _, _ = await prepare([candidate()])
    second, _, _ = await prepare([candidate(card_digest="card-2")])
    assert (
        first.snapshot.entries[0].definition.name
        == second.snapshot.entries[0].definition.name
    )
    assert first.bindings[0].binding_digest != second.bindings[0].binding_digest


async def _durable_reader_and_call(profile_id: str = "fast"):
    root_manifest = RunResourceManifestSnapshot(
        manifest_id="root-context",
        refs=[
            PreparedResourceRef(
                ref_id="ctx:message:user-1",
                kind="context",
                source_message_id="user-1",
                mime_type="text/plain",
                size_bytes=5,
                content_digest="root-context-digest",
            ),
            PreparedResourceRef(
                ref_id="attachment-1",
                kind="attachment",
                source_message_id="user-1",
                mime_type="application/pdf",
                size_bytes=10,
                content_digest="attachment-digest",
            ),
        ],
        content_digest="run-resource-digest",
    )
    prepared_catalog, _, binding_store = await prepare(
        [candidate()], resource_manifest=root_manifest
    )
    run = make_run().model_copy(
        update={
            "run_id": "run-1",
            "tool_catalog": prepared_catalog.snapshot,
            "resource_manifest": root_manifest,
            "profile": make_run().profile.model_copy(update={"profile_id": profile_id}),
            "request": make_run().request.model_copy(
                update={"requesting_subject_id": "user-1", "room_epoch": 1}
            ),
        }
    )
    store = InMemoryOrchestratorRunStore()
    await store.create(run, command_id="create")
    reader = RunPreparedInvocationSnapshotReader(
        run_store=store, binding_store=binding_store
    )
    call = invocation().model_copy(
        update={
            "tool": invocation().tool.model_copy(
                update={
                    "definition": prepared_catalog.snapshot.entries[0].definition,
                    "binding": prepared_catalog.snapshot.entries[0].binding,
                }
            )
        }
    )
    return reader, call, prepared_catalog


async def test_prepared_invocation_reconstructs_from_durable_run_and_binding():
    reader, call, prepared_catalog = await _durable_reader_and_call()
    snapshot = await reader.read_prepared(call)
    assert snapshot is not None
    assert snapshot.requesting_subject_id == "user-1"
    assert snapshot.binding == prepared_catalog.bindings[0]
    assert [ref.ref_id for ref in snapshot.resource_manifest.refs] == [
        "ctx:message:user-1"
    ]

    recovered = await reader.read_prepared(call)
    assert recovered is not None
    assert recovered.resource_manifest == snapshot.resource_manifest
    assert recovered.resource_manifest.manifest_id == (
        f"call-resources-{recovered.resource_manifest.content_digest}"
    )


async def test_passthrough_invocation_does_not_resend_user_text_as_context():
    # Passthrough forwards the user's text verbatim as ``task``; also
    # selecting the root user-text context would deliver it to the Agent twice.
    reader, call, _ = await _durable_reader_and_call("passthrough")

    snapshot = await reader.read_prepared(call)

    assert snapshot is not None
    assert snapshot.resource_manifest.refs == []


async def test_preparation_fails_closed_for_inactive_epoch():
    epochs = InMemoryRoomEpochStore()
    assembler = AgentToolCatalogAssembler(
        candidate_source=Candidates([candidate()]),
        binding_store=InMemoryAgentToolBindingStore(),
        room_epoch_store=epochs,
    )
    try:
        await assembler.prepare(
            run_id="run-1",
            conversation_id="room-1",
            room_epoch=1,
            requesting_subject_id="user-1",
            candidate_scope=CandidateScopeSnapshot(
                snapshot_id="scope",
                source="test",
                conversation_id="room-1",
                agent_ids=["agent-1"],
                authorization_basis=AuthorizationBasis(kind="room_member"),
            ),
            resource_manifest=RunResourceManifestSnapshot(
                manifest_id="empty", refs=[], content_digest="empty"
            ),
            authorization_basis_digest="auth",
            created_at=NOW,
        )
    except PermissionError:
        pass
    else:
        raise AssertionError("inactive Room epoch was accepted")


async def test_catalog_keeps_each_agents_skills_in_card_order():
    # Card order: agent-1 lists "beta" before "alpha". Passthrough calls an
    # Agent's first catalog tool, which must be its first card skill.
    prepared, _source, _store = await prepare(
        [
            candidate("agent-2", skill_id="zeta"),
            candidate("agent-1", skill_id="beta"),
            candidate("agent-1", skill_id="alpha"),
        ]
    )

    assert [entry.definition.name for entry in prepared.snapshot.entries] == [
        deterministic_tool_name("agent-1", "beta"),
        deterministic_tool_name("agent-1", "alpha"),
        deterministic_tool_name("agent-2", "zeta"),
    ]

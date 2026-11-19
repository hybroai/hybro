from __future__ import annotations

from dataclasses import replace

from execution.model_loop.context import ContextCompiler
from execution.orchestrator.models import ModelMessage, ModelTextPart
from execution.orchestrator.session import DefaultRunFactory
from execution.orchestrator_routing import DualRuntimeRouter

from ._orchestrator_helpers import FixedClock, FixedIDs, session_config, user_message


async def test_room_history_is_typed_and_excludes_current_and_subagent_turns():
    async def read_memory(conversation_id: str):
        assert conversation_id == "room-1"
        return {
            "room_id": conversation_id,
            "memory_id": "memory-1",
            "conversation_history": [
                {
                    "turn_id": "message:user-previous",
                    "role": "user",
                    "content": "Remember ORCHID-171.",
                    "timestamp": "2031-01-01T00:00:00Z",
                },
                {
                    "turn_id": "message:agent-detail",
                    "role": "agent",
                    "agent_id": "weather-agent",
                    "content": "internal agent detail",
                    "timestamp": "2031-01-01T00:00:01Z",
                },
                {
                    "turn_id": "message:assistant-previous",
                    "role": "agent",
                    "agent_id": "system:hybro",
                    "content": "OK",
                    "timestamp": "2031-01-01T00:00:02Z",
                },
                {
                    "turn_id": "message:user-current",
                    "role": "user",
                    "content": "What was the code?",
                    "timestamp": "2031-01-01T00:00:03Z",
                },
            ],
        }

    router = DualRuntimeRouter(room_memory_reader=read_memory)
    history = (
        await router._load_room_context(  # noqa: SLF001
            "room-1", current_message_id="user-current"
        )
    ).history

    assert [message.role for message in history] == ["user", "assistant"]
    assert [message.content[0].text for message in history] == [
        "Remember ORCHID-171.",
        "OK",
    ]


def test_new_run_freezes_room_history_before_current_message():
    prior_user = ModelMessage(
        role="user", content=[ModelTextPart(text="Remember ORCHID-171.")]
    )
    prior_assistant = ModelMessage(role="assistant", content=[ModelTextPart(text="OK")])
    config = replace(
        session_config(), conversation_history=(prior_user, prior_assistant)
    )
    current = user_message("What was the code?").model_copy(
        update={"message_id": "user-current"}
    )

    run = DefaultRunFactory(clock=FixedClock(), id_factory=FixedIDs()).create_run(
        config=config,
        message=current,
        client_request_id="request-1",
    )
    compiled = ContextCompiler().compile(
        run, tools=[], background=run.background_context
    )

    assert run.transcript == [current]
    assert run.background_context == [prior_user, prior_assistant]
    assert compiled.kind == "ready"
    assert [message.role for message in compiled.messages] == [
        "user",
        "assistant",
        "user",
    ]
    assert [message.content[0].text for message in compiled.messages] == [
        "Remember ORCHID-171.",
        "OK",
        "What was the code?",
    ]


async def test_room_history_carries_answers_the_user_gave_in_an_earlier_turn():
    from types import SimpleNamespace

    from execution.orchestrator.models import (
        ToolInteractionAnswer,
        ToolInteractionAnswers,
        ToolInteractionMessage,
        ToolInteractionQuestion,
    )

    from ._orchestrator_helpers import NOW

    async def read_memory(conversation_id: str):
        return {
            "room_id": conversation_id,
            "memory_id": "memory-1",
            "conversation_history": [
                {
                    "turn_id": "message:user-previous",
                    "role": "user",
                    "content": "Draft the emails.",
                    "timestamp": "2031-01-01T00:00:00Z",
                },
                {
                    "turn_id": "message:assistant-previous",
                    "role": "agent",
                    "agent_id": "system:hybro",
                    "content": "Drafts are ready.",
                    "timestamp": "2031-01-01T00:00:02Z",
                },
                {
                    "turn_id": "message:user-current",
                    "role": "user",
                    "content": "Revise them.",
                    "timestamp": "2031-01-01T00:00:03Z",
                },
            ],
        }

    interaction = ToolInteractionMessage(
        message_id="interaction:call-1:fp",
        call_id="call-1",
        tool_name="writer",
        interaction_id="interaction-1",
        interaction_fingerprint="fp",
        questions=[
            ToolInteractionQuestion(
                question_id="sender", prompt="Who signs the emails?", answer_kind="text"
            )
        ],
        interaction_answers=[
            ToolInteractionAnswers(
                interaction_id="interaction-1",
                answers=[
                    ToolInteractionAnswer(
                        question_id="sender", answer="Morgan Reyes, CEO"
                    )
                ],
            )
        ],
        created_at=NOW,
    )
    loaded: list[str] = []

    class RunStore:
        async def load_by_user_message_id(self, message_id: str):
            loaded.append(message_id)
            if message_id != "user-previous":
                return None
            return SimpleNamespace(transcript=[interaction])

    router = DualRuntimeRouter(
        runtime=SimpleNamespace(run_store=RunStore()),
        room_memory_reader=read_memory,
    )
    history = (
        await router._load_room_context(  # noqa: SLF001
            "room-1", current_message_id="user-current"
        )
    ).history

    assert loaded == ["user-previous"]
    assert [message.role for message in history] == ["user", "assistant"]
    assert history[0].content[0].text == "Draft the emails."
    answer, record = history[1].content
    assert answer.text == "Drafts are ready."
    assert "treat them as user-supplied facts" in record.text
    assert "Question: Who signs the emails?" in record.text
    assert "Answer: Morgan Reyes, CEO" in record.text


async def test_follow_up_can_reference_earlier_attachments_and_artifacts():
    from types import SimpleNamespace

    from execution.orchestrator.models import (
        PreparedResourceRef,
        RunResourceManifestSnapshot,
        ToolResultMessage,
    )
    from execution.orchestrator_routing import _build_resource_manifest

    from ._orchestrator_helpers import NOW

    def ref(ref_id: str, kind: str, mime: str) -> PreparedResourceRef:
        return PreparedResourceRef(
            ref_id=ref_id,
            kind=kind,
            source_message_id="source",
            mime_type=mime,
            size_bytes=10,
            content_digest=f"digest-{ref_id}",
        )

    def run(epoch: int, refs: list[PreparedResourceRef], transcript=()):
        return SimpleNamespace(
            request=SimpleNamespace(room_epoch=epoch),
            resource_manifest=RunResourceManifestSnapshot(
                manifest_id="m", refs=refs, content_digest="d"
            ),
            tool_catalog=SimpleNamespace(
                entries=[
                    SimpleNamespace(
                        definition=SimpleNamespace(name="research", label="R - R"),
                        agent_display_name="Investor Research",
                    )
                ]
            ),
            transcript=list(transcript),
        )

    produced = ToolResultMessage(
        message_id="result-1",
        call_id="call-1",
        tool_name="research",
        status="completed",
        content=[],
        artifact_refs=["art_research"],
        is_error=False,
        created_at=NOW,
    )
    runs = {
        # A turn from before the Room was reset does not contribute files.
        "old-epoch": run(1, [ref("file-stale", "attachment", "text/csv")]),
        "first": run(
            2,
            [
                ref("ctx:message:first", "context", "text/plain"),
                ref("file-list", "attachment", "application/xlsx"),
                ref("art_research", "artifact", "application/json"),
            ],
            [produced],
        ),
        "second": run(
            2, [ref(f"art_{index}", "artifact", "text/markdown") for index in range(19)]
        ),
    }

    class RunStore:
        async def load_by_user_message_id(self, message_id: str):
            return runs.get(message_id)

    def user(turn: str) -> dict:
        return {
            "turn_id": f"message:{turn}",
            "role": "user",
            "content": f"turn {turn}",
            "timestamp": "2031-01-01T00:00:00Z",
        }

    async def read_memory(conversation_id: str):
        return {
            "room_id": conversation_id,
            "memory_id": "memory-1",
            "conversation_history": [
                user("old-epoch"),
                user("first"),
                user("second"),
                user("current"),
            ],
        }

    router = DualRuntimeRouter(
        runtime=SimpleNamespace(run_store=RunStore()),
        room_memory_reader=read_memory,
    )
    context = await router._load_room_context(  # noqa: SLF001
        "room-1", current_message_id="current", room_epoch=2
    )

    # The 20 most recent files, oldest first: the cap drops file-list.
    assert [item.ref_id for item in context.earlier_refs] == [
        "art_research",
        *[f"art_{index}" for index in range(19)],
    ]
    first_turn = context.history[1].content[-1].text
    assert "- art_research: Artifact (application/json) produced by Investor" in (
        first_turn
    )
    assert "file-list" not in first_turn
    assert "file-stale" not in "".join(
        part.text for message in context.history for part in message.content
    )

    manifest = _build_resource_manifest(
        source_message_id="current",
        user_text="Revise it.",
        attachments=None,
        earlier_refs=context.earlier_refs,
    )
    assert [item.ref_id for item in manifest.refs] == [
        "ctx:message:current",
        *[item.ref_id for item in context.earlier_refs],
    ]

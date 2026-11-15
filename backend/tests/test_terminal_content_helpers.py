"""Tests for terminal agent content helpers in a2a_helpers."""

from common.utils.a2a_helpers import (
    _part_dict_is_text,
    extract_text_from_artifact_dicts,
    prepare_terminal_agent_content,
    resolve_terminal_sse_content,
    sync_artifact_dicts_to_canonical_text,
)


class TestPrepareTerminalAgentContent:
    def test_syncs_artifacts_to_resolved_message_text(self) -> None:
        raw = '1. "One" — ago 2. "Two" — ago'
        text, artifacts, _ = prepare_terminal_agent_content(
            message_text=None,
            artifacts=[{"artifactId": "a1", "parts": [{"kind": "text", "text": raw}]}],
        )
        assert text == raw
        assert artifacts is not None
        assert extract_text_from_artifact_dicts(artifacts) == raw

    def test_passthrough_when_only_message_text(self) -> None:
        text, artifacts, task = prepare_terminal_agent_content(
            message_text="Hello",
            artifacts=None,
            task_data=None,
        )
        assert text == "Hello"
        assert artifacts is None
        assert task is None


class TestTerminalContentHelpers:
    def test_sync_artifact_dicts_to_canonical_text(self) -> None:
        artifacts = [
            {
                "artifactId": "stream-1",
                "parts": [
                    {"kind": "text", "text": "part one"},
                    {"kind": "text", "text": "stale tail"},
                ],
            }
        ]
        canonical = "canonical body"
        synced = sync_artifact_dicts_to_canonical_text(artifacts, canonical)
        assert extract_text_from_artifact_dicts(synced) == canonical
        text_parts = [part for part in synced[0]["parts"] if _part_dict_is_text(part)]
        assert len(text_parts) == 1
        assert text_parts[0]["text"] == canonical

    def test_resolve_terminal_sse_content_prefers_message_text_on_completed(
        self,
    ) -> None:
        from common.types import TaskState

        stored = "1. First\n2. Second"
        raw_artifact = "artifact fallback"
        resolved = resolve_terminal_sse_content(
            TaskState.completed,
            message_text=stored,
            artifact_text=raw_artifact,
        )
        assert resolved == stored

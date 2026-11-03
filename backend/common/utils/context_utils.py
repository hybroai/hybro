"""
Context utilities for room conversation memory management.

See docs/System-Architecture.md for design details.
"""

from __future__ import annotations

import re
from typing import Any, Protocol

from common.observability import events as E
from common.utils.logger import get_logger

logger = get_logger(__name__)


class MemoryContentLike(Protocol):
    summary: str | None
    conversation_history: list[Any]


class RoomMemoryLike(Protocol):
    memory_content: MemoryContentLike | None
    conversation_history: list[Any]


# Configuration
MAX_HISTORY_TURNS = 20  # Keep last N turns in full detail
MAX_SUMMARY_CHARS = 4000  # Cap for MemoryContent.summary to prevent unbounded growth

# Token estimation constants
CHARS_PER_TOKEN_ESTIMATE = 4  # Approximate chars per token for English text


def estimate_tokens(text: str | None, model: str = "gpt-4") -> int:
    """
    Estimate token count for text.

    Uses tiktoken for accuracy if available. Falls back to char/4 heuristic.

    See docs/System-Architecture.md for the current architecture.

    Args:
        text: The text to estimate tokens for
        model: The model to use for tokenization (default: gpt-4)

    Returns:
        Estimated token count
    """
    if not text:
        return 0

    try:
        import tiktoken

        encoding = tiktoken.encoding_for_model(model)
        return len(encoding.encode(text))
    except ImportError:
        logger.emit(E.TOKEN_ESTIMATION_FALLBACK_SELECTED, reason="tiktoken_unavailable")
    except Exception as exc:
        logger.emit(
            E.TOKEN_ESTIMATION_FALLBACK_SELECTED,
            reason="encoding_failed",
            error_type=type(exc).__name__,
        )

    # Fallback: ~4 chars per token for English
    return len(text) // CHARS_PER_TOKEN_ESTIMATE


def clean_mention_format(
    text: str, room_agent_set: dict[str, str] | None = None
) -> str:
    """
    Convert <@uuid|name> mentions to clean @AgentName format for storage.

    Args:
        text: Raw message text with mentions like "<@d7d6dbb1-...|N8n Agent>"
        room_agent_set: Dict of {agent_id: agent_name} from room

    Returns:
        Clean text like "@N8n Agent check my email"

    Example:
        Input:  "<@d7d6dbb1-e1ba-48f3-946b-48c6adb98f4d|N8n Personal AI Assistant Agent> can you check me email today?"
        Output: "@N8n Personal AI Assistant Agent can you check me email today?"
    """
    room_agent_set = room_agent_set or {}

    def replace_mention(match: re.Match) -> str:
        agent_id = match.group(1)
        agent_name = match.group(2)
        # Prefer name from room_agent_set if available for consistency
        display_name = room_agent_set.get(agent_id, agent_name)
        return f"@{display_name}"

    cleaned = re.sub(r"<@([^|]+)\|([^>]+)>", replace_mention, text)
    # Clean up any extra whitespace
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


_AGENT_MENTION_PATTERN = re.compile(r"<@([^|]+)\|([^>]+)>")


def extract_mentioned_agent_ids(text: str) -> list[str]:
    """
    Extract agent IDs from mention format in text.

    Args:
        text: Message text with mentions like "<@uuid|name>"

    Returns:
        List of agent IDs found in mentions
    """
    pattern = r"<@([^|]+)\|[^>]+>"
    return re.findall(pattern, text)


def split_agent_mentions(text: str) -> tuple[str, list[tuple[str, str]]]:
    """Split a user message into clean text and its explicit Agent mentions.

    The stored message keeps the editor's ``<@agent-id|AgentName>`` token so the
    UI can render it as a chip. A consumer presenting the message to a model
    should not receive that token: it carries an internal id and an editor
    syntax the model cannot interpret.

    Returns the text with those tokens removed, plus the ``(agent_id,
    agent_name)`` pairs in the order they appeared. Line breaks are preserved;
    only the gap the removed token left behind is collapsed.
    """
    source = text or ""
    mentions = [
        (match.group(1), match.group(2))
        for match in _AGENT_MENTION_PATTERN.finditer(source)
    ]
    if not mentions:
        return source.strip(), []

    cleaned = _AGENT_MENTION_PATTERN.sub("", source)
    cleaned = re.sub(r"[^\S\n]{2,}", " ", cleaned)
    cleaned = "\n".join(line.strip() for line in cleaned.split("\n")).strip()
    if not cleaned:
        # A message that is nothing but mentions still needs a turn body.
        cleaned = " ".join(f"@{name}" for _, name in mentions)
    return cleaned, mentions


def _value(value: Any) -> Any:
    return getattr(value, "value", value)


def _representation_value(value: Any) -> str:
    return str(_value(value))


def get_context_stats(room_memory: RoomMemoryLike) -> dict:
    """Get monitoring statistics from canonical room-memory fields."""
    total_chars = 0
    total_tokens = 0
    full_turns = 0
    compact_turns = 0
    memory_content = room_memory.memory_content
    summary = memory_content.summary if memory_content else None

    if summary:
        total_chars += len(summary)

    for turn in room_memory.conversation_history:
        # Check representation if available (new ConversationTurn)
        if hasattr(turn, "representation"):
            if _representation_value(turn.representation) == "full":
                full_turns += 1
                if turn.content:
                    total_chars += len(turn.content)
            else:
                compact_turns += 1
        else:
            # Turn without a representation counts as full.
            full_turns += 1
            if turn.content:
                total_chars += len(turn.content)

        # Sum token estimates if available
        if hasattr(turn, "estimated_tokens_full"):
            total_tokens += turn.estimated_tokens_full

    return {
        "history_turns": len(room_memory.conversation_history),
        "full_turns": full_turns,
        "compact_turns": compact_turns,
        "has_summary": bool(summary),
        "summary_length": len(summary) if summary else 0,
        "total_chars": total_chars,
        "total_tokens": total_tokens,
    }

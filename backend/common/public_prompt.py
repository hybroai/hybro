"""Public projection helpers for agent-originated HITL prompts."""

from __future__ import annotations

import re

GENERIC_AGENT_INPUT_PROMPT = "The agent needs additional information."
_MAX_PUBLIC_AGENT_PROMPT_CHARS = 1200
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_PRIVATE_MARKERS = (
    "PRIVATE_SENTINEL",
    "[SYSTEM]",
    "<system>",
    "BEGIN SYSTEM PROMPT",
    "END SYSTEM PROMPT",
)
_INTERNAL_CONTRACT_FORMAT = re.compile(
    r"\b(?:json(?:\s+object)?|application/json|data\s*part|"
    r"structured(?:\s+data)?\s+payload|schema(?:-conformant)?\s+payload|"
    r"machine-readable\s+payload)\b",
    re.I,
)
_INTERNAL_CONTRACT_FIELD = re.compile(
    r"\b[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+\b",
    re.I,
)
_INTERNAL_CONTRACT_IMPERATIVE = re.compile(
    r"\b(?:send|provide|include|containing|return|supply|pass)\b",
    re.I,
)


def safe_agent_input_prompt(prompt: str | None) -> str | None:
    """Return a bounded user-facing question, or None when it is not public."""
    if not isinstance(prompt, str):
        return None
    normalized = " ".join(_CONTROL_CHARS.sub("", prompt).split()).strip()
    if not normalized or len(normalized) > _MAX_PUBLIC_AGENT_PROMPT_CHARS:
        return None
    upper = normalized.upper()
    if any(marker.upper() in upper for marker in _PRIVATE_MARKERS):
        return None
    return normalized


def is_internal_agent_contract_prompt(prompt: str | None) -> bool:
    """Return whether an Agent is requesting a private transport/schema payload."""
    safe_prompt = safe_agent_input_prompt(prompt)
    if safe_prompt is None:
        return False
    if _INTERNAL_CONTRACT_FORMAT.search(safe_prompt):
        return True
    dotted_fields = _INTERNAL_CONTRACT_FIELD.findall(safe_prompt)
    return bool(dotted_fields and _INTERNAL_CONTRACT_IMPERATIVE.search(safe_prompt))


def concrete_agent_input_prompt(prompt: str | None) -> str | None:
    """Return a concrete public question, rejecting private contract requests."""
    safe_prompt = safe_agent_input_prompt(prompt)
    if safe_prompt is None or is_internal_agent_contract_prompt(safe_prompt):
        return None
    if safe_prompt.casefold() == GENERIC_AGENT_INPUT_PROMPT.casefold():
        return None
    return safe_prompt

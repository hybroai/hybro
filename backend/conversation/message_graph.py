from __future__ import annotations

from typing import Any

from conversation.timeline import SOURCE_RANK, timeline_sort_us_from_value


def normalize_history_rows(
    user_rows: list[dict[str, Any]],
    agent_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for row in user_rows:
        item = dict(row)
        item.setdefault("message_type", "user")
        rows.append(item)
    for row in agent_rows:
        item = dict(row)
        item.setdefault("message_type", "agent")
        rows.append(item)
    return sort_messages(rows)


def sort_messages(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(rows, key=_message_sort_key)


def status_update_payload(status: str, fields: dict[str, Any]) -> dict[str, Any]:
    return {
        "message_content.message_task.status.state": status,
        **fields,
    }


def _message_sort_key(row: dict[str, Any]) -> tuple[int, int, int, str]:
    sort_us = row.get("timeline_sort_us")
    missing = 0
    if isinstance(sort_us, bool) or not isinstance(sort_us, int):
        try:
            sort_us = timeline_sort_us_from_value(row.get("message_created_at"))
        except ValueError:
            sort_us = 0
            missing = 1
    source = str(row.get("message_type") or "agent")
    return (
        missing,
        sort_us,
        SOURCE_RANK.get(source, SOURCE_RANK["agent"]),
        str(row.get("message_id") or ""),
    )

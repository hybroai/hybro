"""Adapter for fetching task state from remote A2A agents."""

from __future__ import annotations

from typing import Any

from a2a.client import ClientConfig, ClientFactory
from a2a.types import GetTaskRequest

from common.observability import events as E
from common.observability import get_logger
from common.types import Task

from .bounded_http import bounded_client
from .card_data import build_agent_card
from .constants import SUPPORTED_BINDINGS
from .docker_host_fallback import with_docker_host_fallback
from .message_factory import to_internal_task

logger = get_logger(__name__)


async def fetch_remote_task(
    agent_card_data: Any,
    task_id: str,
    *,
    timeout: float = 30.0,
) -> Task | None:
    """Fetch a task from a remote A2A agent and return an internal task.

    Uses the same client construction as outbound sends, so a query lands on
    the interface bound to the accepted task instead of a re-derived endpoint.
    """
    try:
        card = build_agent_card(agent_card_data)
        async with bounded_client(timeout=timeout) as client:
            config = ClientConfig(
                streaming=False,
                httpx_client=client,
                supported_protocol_bindings=list(SUPPORTED_BINDINGS),
                use_client_preference=True,
            )
            task = await with_docker_host_fallback(
                card,
                lambda candidate: (
                    ClientFactory(config)
                    .create(candidate)
                    .get_task(GetTaskRequest(id=task_id))
                ),
            )
        return to_internal_task(task)
    except Exception as exc:
        logger.emit(E.REMOTE_TASK_FETCH_FAILED, exc=exc, task_id=task_id)
        return None


__all__ = ["fetch_remote_task"]

from typing import override

from a2a.helpers import new_task_from_user_message, new_text_status_update_event
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.types import TaskState, UnsupportedOperationError

SIGNATURE = "Created by Hybro"


def echo_reply(text: str) -> str:
    """The received text followed by the signature line."""
    return f"{text}\n{SIGNATURE}" if text else SIGNATURE


class EchoAgentExecutor(AgentExecutor):
    """Completes every task with the received text; no model, no network."""

    @override
    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        if not context.message:
            raise ValueError("No message provided")
        task = new_task_from_user_message(context.message)
        # The task must be published before any task-scoped update event.
        await event_queue.enqueue_event(task)
        await event_queue.enqueue_event(
            new_text_status_update_event(
                task_id=task.id,
                context_id=task.context_id,
                state=TaskState.TASK_STATE_COMPLETED,
                text=echo_reply(context.get_user_input()),
            )
        )

    @override
    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        raise UnsupportedOperationError()

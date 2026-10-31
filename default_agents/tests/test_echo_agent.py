"""The Echo agent answers without a model provider or network access."""

import asyncio
import importlib.util
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from a2a.helpers import new_text_message
from a2a.server.agent_execution import RequestContext
from a2a.server.context import ServerCallContext
from a2a.server.events import EventQueue
from a2a.types import Role, SendMessageRequest, Task, TaskState, TaskStatusUpdateEvent

ECHO_DIR = Path(__file__).resolve().parents[1] / "echo_agent"

# The agent imports its executor as a top-level module, as it runs in its image.
with patch.object(sys, "path", [str(ECHO_DIR), *sys.path]):
    from agent_executor import SIGNATURE, EchoAgentExecutor, echo_reply


class RecordingQueue(EventQueue):
    def __init__(self):
        self.events = []

    async def enqueue_event(self, event):
        self.events.append(event)


def request_context(text: str) -> RequestContext:
    return RequestContext(
        call_context=ServerCallContext(),
        request=SendMessageRequest(message=new_text_message(text, role=Role.ROLE_USER)),
    )


class EchoAgentTests(unittest.TestCase):
    def test_reply_appends_signature(self):
        self.assertEqual(echo_reply("Hello"), "Hello\nCreated by Hybro")
        self.assertEqual(echo_reply(""), SIGNATURE)

    def test_execute_completes_task_with_echo(self):
        queue = RecordingQueue()
        asyncio.run(EchoAgentExecutor().execute(request_context("Hello"), queue))

        self.assertEqual(
            [type(event) for event in queue.events], [Task, TaskStatusUpdateEvent]
        )
        task, terminal = queue.events
        self.assertEqual(terminal.task_id, task.id)
        self.assertEqual(terminal.status.state, TaskState.TASK_STATE_COMPLETED)
        text = "".join(part.text for part in terminal.status.message.parts)
        self.assertEqual(text, "Hello\nCreated by Hybro")

    def test_agent_card_route_serves_card(self):
        from starlette.testclient import TestClient

        spec = importlib.util.spec_from_file_location(
            "echo_agent_main", ECHO_DIR / "__main__.py"
        )
        module = importlib.util.module_from_spec(spec)
        with patch.object(sys, "path", [str(ECHO_DIR), *sys.path]):
            spec.loader.exec_module(module)
        with patch.dict(os.environ, {"SERVER_PORT": "7004"}):
            card = TestClient(module.build_app()).get("/.well-known/agent-card.json")
        self.assertEqual(card.json()["name"], "Echo Agent")
        self.assertEqual(card.json()["skills"][0]["id"], "echo")


if __name__ == "__main__":
    unittest.main()

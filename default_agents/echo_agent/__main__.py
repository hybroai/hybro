"""Echo agent: an A2A server that returns the received text.

It needs no model provider and no network, so it works right after install.
"""

import logging
import os

from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore
from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentSkill
from a2a.utils.constants import PROTOCOL_VERSION_1_0, TransportProtocol
from agent_executor import EchoAgentExecutor
from starlette.applications import Starlette

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def build_app() -> Starlette:
    """Builds the A2A application: agent card + JSON-RPC endpoint."""
    port = int(os.getenv("SERVER_PORT", "7004"))
    server_domain = os.getenv("SERVER_DOMAIN", "localhost")

    agent_card = AgentCard(
        name="Echo Agent",
        description=(
            "Repeats your message back. Use it to check that Hybro can reach"
            " an agent; it needs no model provider."
        ),
        supported_interfaces=[
            AgentInterface(
                protocol_binding=TransportProtocol.JSONRPC,
                protocol_version=PROTOCOL_VERSION_1_0,
                url=f"http://{server_domain}:{port}/",
            )
        ],
        version="1.0.0",
        default_input_modes=["text"],
        default_output_modes=["text"],
        capabilities=AgentCapabilities(streaming=False),
        skills=[
            AgentSkill(
                id="echo",
                name="Echo",
                description="Returns the received text.",
                tags=["echo", "test"],
                examples=["Hello, Hybro"],
            )
        ],
    )
    request_handler = DefaultRequestHandler(
        agent_executor=EchoAgentExecutor(),
        task_store=InMemoryTaskStore(),
        agent_card=agent_card,
    )
    return Starlette(
        routes=create_agent_card_routes(agent_card)
        + create_jsonrpc_routes(request_handler, rpc_url="/")
    )


def main() -> None:
    import uvicorn

    host = os.getenv("SERVER_HOST", "0.0.0.0")
    port = int(os.getenv("SERVER_PORT", "7004"))
    uvicorn.run(build_app(), host=host, port=port)


if __name__ == "__main__":
    main()

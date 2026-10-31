#!/usr/bin/env python3
"""Lightweight mock OpenAI-compatible server for CI and automated testing.

Requires 0 third-party packages (uses Python standard library http.server).
Simulates responses for:
- Supervisor multi-agent delegation & synthesis
- Agent HITL questionnaires (AskUserForClarification)
- Agent completion itineraries
- Streaming (SSE) and non-streaming responses
- Embeddings and model listing
- Image generation and edits (one 1x1 PNG)
"""

from __future__ import annotations

import json
import logging
import sys
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

logging.basicConfig(level=logging.INFO, format="[mock-llm] %(asctime)s - %(message)s")
logger = logging.getLogger("mock_llm")

# A valid 1x1 transparent PNG, returned for every image request.
MOCK_PNG_BASE64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA"
    "60e6kgAAAABJRU5ErkJggg=="
)


def _message_content_text(content) -> str:
    if isinstance(content, list):
        text_parts = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                text_parts.append(part.get("text", ""))
            elif isinstance(part, str):
                text_parts.append(part)
        return " ".join(text_parts)
    if isinstance(content, str):
        return content
    return str(content or "")


def extract_last_user_and_tool_info(messages: list[dict]) -> tuple[str, list[dict], bool]:
    """Inspects messages for the latest user prompt, tool responses, and whether tools are present."""
    last_user_text = ""
    tool_results = []
    has_tool_results = False

    for msg in messages:
        role = msg.get("role")
        content_str = _message_content_text(msg.get("content"))
        tool_call_id = msg.get("tool_call_id")

        if role == "user":
            last_user_text = content_str
        elif role == "tool" or tool_call_id:
            has_tool_results = True
            tool_results.append(content_str)

    return last_user_text, tool_results, has_tool_results


def collect_user_text(messages: list[dict]) -> str:
    """Aggregate user-role text for travel planner routing."""
    chunks: list[str] = []
    for msg in messages:
        if msg.get("role") != "user":
            continue
        content_str = _message_content_text(msg.get("content"))
        if content_str.strip():
            chunks.append(content_str)
    return " ".join(chunks).lower()


def collect_conversation_text(messages: list[dict]) -> str:
    """Aggregate user and tool text so resumed HITL answers affect routing."""
    chunks: list[str] = []
    for msg in messages:
        role = msg.get("role")
        content_str = _message_content_text(msg.get("content"))
        if not content_str.strip():
            continue
        if role in {"user", "tool"} or msg.get("tool_call_id"):
            chunks.append(content_str)
    return " ".join(chunks).lower()


def _supervisor_observations(
    messages: list[dict], agent_tools: list[str]
) -> tuple[list[str], bool]:
    """Read canonical private observations, not user text or surface results.

    Transcript conversion folds all interaction rounds and the eventual terminal
    result into one tool message. Only the latest request per call is pending;
    a terminal observation supersedes every historical request for that call.
    """
    call_names = {
        call["id"]: call.get("function", {}).get("name")
        for message in messages
        if message.get("role") == "assistant"
        for call in message.get("tool_calls", [])
    }
    pending: dict[str, str] = {}
    completed = False
    marker = "[agent input request; answer it from existing evidence, or ask the user]"
    for message in messages:
        call_id = message.get("tool_call_id")
        if message.get("role") != "tool" or call_names.get(call_id) not in agent_tools:
            continue
        lines = _message_content_text(message.get("content")).splitlines()
        for index, line in enumerate(lines):
            if line.startswith("[agent observation:"):
                pending.pop(call_id, None)
                completed |= line == (
                    "[agent observation: verified completed result; usable as evidence]"
                )
            elif line == marker and index + 1 < len(lines):
                try:
                    observation = json.loads(lines[index + 1])
                except json.JSONDecodeError:
                    continue
                if not isinstance(observation, dict):
                    continue
                presentation_id = observation.get("presentation_id")
                if isinstance(presentation_id, str) and presentation_id:
                    pending[call_id] = presentation_id
    return list(pending.values()), completed


def _surface_call(pending: list[str], tools: list[dict]) -> dict | None:
    surface_tool = next(
        (
            tool["function"]
            for tool in tools
            if tool.get("function", {}).get("name") == "surface_agent_questions"
        ),
        None,
    )
    if surface_tool is None:
        return None
    target_schema = (
        surface_tool["parameters"].get("properties", {}).get("presentation_id")
    )
    for presentation_id in pending:
        if target_schema is not None and presentation_id not in target_schema["enum"]:
            continue
        # The singleton schema forbids arguments; multiple targets require
        # the exact private presentation ID from the tool observation.
        arguments = (
            {} if target_schema is None else {"presentation_id": presentation_id}
        )
        return {
            "id": f"call_surface_{uuid.uuid4().hex[:8]}",
            "type": "function",
            "function": {
                "name": "surface_agent_questions",
                "arguments": json.dumps(arguments),
            },
        }
    return None


class MockLLMHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        if self.path in {"/health", "/healthz"}:
            self._send_json({"status": "ok", "service": "mock-llm-server"})
            return
        if self.path.endswith("/models") or self.path.endswith("/v1/models"):
            self._send_json({
                "object": "list",
                "data": [
                    {"id": "gpt-4o", "object": "model", "owned_by": "mock"},
                    {"id": "gpt-4o-mini", "object": "model", "owned_by": "mock"},
                    {"id": "gpt-5-mini", "object": "model", "owned_by": "mock"},
                    {"id": "text-embedding-3-small", "object": "model", "owned_by": "mock"},
                ],
            })
            return
        self.send_error(404, "Endpoint not found")

    def do_POST(self):
        normalized_path = self.path.split("?")[0].rstrip("/")
        if normalized_path.endswith("/embeddings"):
            self._handle_embeddings()
            return
        if normalized_path.endswith("/chat/completions"):
            self._handle_chat_completions()
            return
        if normalized_path.endswith(("/images/generations", "/images/edits")):
            self._handle_images()
            return
        self.send_error(404, f"Unknown POST endpoint: {self.path}")

    def _send_json(self, data: dict, status: int = 200):
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_embeddings(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length).decode("utf-8")) if length > 0 else {}
            input_val = payload.get("input", "")
            items = [input_val] if isinstance(input_val, str) else list(input_val)
            data = [
                {"object": "embedding", "embedding": [0.01 * (i % 10)] * 1536, "index": i}
                for i in range(len(items))
            ]
            self._send_json({
                "object": "list",
                "data": data,
                "model": payload.get("model", "text-embedding-3-small"),
                "usage": {"prompt_tokens": 10, "total_tokens": 10},
            })
        except Exception as exc:
            logger.exception("Error handling embeddings: %s", exc)
            self.send_error(500, str(exc))

    def _handle_images(self):
        # Generations send JSON and edits send multipart; neither body matters.
        length = int(self.headers.get("Content-Length", 0))
        if length > 0:
            self.rfile.read(length)
        logger.info("Image: %s", self.path)
        self._send_json({
            "created": int(time.time()),
            "data": [{"b64_json": MOCK_PNG_BASE64}],
            "output_format": "png",
        })

    def _handle_chat_completions(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            body_bytes = self.rfile.read(length) if length > 0 else b"{}"
            payload = json.loads(body_bytes.decode("utf-8"))
            stream = payload.get("stream", False)
            messages = payload.get("messages", [])
            tools = payload.get("tools", [])
            model = payload.get("model", "gpt-4o")

            tool_calls, text_content, finish_reason = self._decide_response(messages, tools, payload)
            logger.info("Chat: model=%s, stream=%s, tools=%d, msgs=%d => finish=%s, has_tools=%s, tool_calls=%s",
                        model, stream, len(tools), len(messages), finish_reason, bool(tool_calls),
                        [tc.get("function", {}).get("name") for tc in (tool_calls or [])])
            logger.info("Chat messages roles: %s", [m.get("role") for m in messages])

            if stream:
                self._stream_response(model, tool_calls, text_content, finish_reason)
            else:
                self._send_non_streaming_response(model, tool_calls, text_content, finish_reason)
        except Exception as exc:
            logger.exception("Error handling chat completions: %s", exc)
            self.send_error(500, str(exc))

    def _decide_response(self, messages: list[dict], tools: list[dict], payload: dict) -> tuple[list[dict] | None, str, str]:
        last_user_text, tool_results, has_tool_results = extract_last_user_and_tool_info(messages)
        tool_names = [t.get("function", {}).get("name") or t.get("name") for t in tools]

        # Case 0: Structured JSON request (response_format provided)
        response_format = payload.get("response_format")
        if response_format:
            data = {
                "message_type": "text",
                "original_text": last_user_text,
                "needs_decomposition": False,
                "decomposition_reason": "Single cohesive task",
                "task_steps": [{
                    "step_id": "step_1",
                    "agent_id": None,
                    "agent_name": None,
                    "task_content": last_user_text,
                    "dependencies": [],
                }],
                "selected_agents": [],
                "summary": last_user_text,
                "key_points": [last_user_text],
            }
            return None, json.dumps(data), "stop"

        # Case 1: Supervisor continuation (private questions precede synthesis).
        agent_tools = [name for name in tool_names if name and name.startswith("agent_")]
        if has_tool_results and agent_tools:
            pending, completed = _supervisor_observations(messages, agent_tools)
            surface_call = _surface_call(pending, tools)
            if surface_call is not None:
                return [surface_call], "", "tool_calls"
            if pending or not completed:
                return None, "No completed agent result is available yet.", "stop"
            synthesis_text = (
                "### Final Synthesized Trip Plan & Itinerary\n\n"
                "Here is the complete trip plan compiled from our specialist agents:\n\n"
                "**1. Destination & Overview:** Comprehensive itinerary prepared.\n"
                "**2. Day-by-Day Schedule:**\n"
                "- **Day 1:** Arrival, check-in, orientation, and evening local exploration.\n"
                "- **Day 2:** Major landmarks, cultural attractions, and dining highlights.\n"
                "- **Day 3:** Scenic excursions, shopping, and smooth departure.\n\n"
                "**3. Weather & Tips:** Clear skies and pleasant conditions expected. Safe travels!"
            )
            return None, synthesis_text, "stop"

        # Case 2: Supervisor Turn 1 (Has agent delegation tools like agent_*)
        if agent_tools and not has_tool_results:
            selected_tool = agent_tools[0]
            call_id = f"call_{uuid.uuid4().hex[:12]}"
            arguments = json.dumps({"task": last_user_text or "Plan trip based on user preferences"})
            tool_calls = [{
                "id": call_id,
                "type": "function",
                "function": {"name": selected_tool, "arguments": arguments},
            }]
            return tool_calls, "", "tool_calls"

        # Case 4: Weather Agent (get_weather / get_forecast)
        weather_tools = [name for name in tool_names if name in {"get_weather", "get_forecast"}]
        if weather_tools and not has_tool_results:
            call_id = f"call_weather_{uuid.uuid4().hex[:8]}"
            args = json.dumps({"city": "San Francisco", "days": 3})
            tool_calls = [{
                "id": call_id,
                "type": "function",
                "function": {"name": weather_tools[0], "arguments": args},
            }]
            return tool_calls, "", "tool_calls"
        elif weather_tools and has_tool_results:
            return None, "The weather is currently clear and sunny with highs around 22°C (72°F).", "stop"

        # Case 3: Travel Planner Agent (LangChain tool AskUserForClarification or direct)
        if "AskUserForClarification" in tool_names:
            conversation_text = collect_conversation_text(messages)
            has_details = any(
                kw in conversation_text
                for kw in [
                    "kyoto",
                    "san francisco",
                    "tokyo",
                    "japan",
                    "3 days",
                    "3-day",
                    "sept",
                    "budget",
                ]
            )
            if not has_details and len(messages) <= 2:
                call_id = f"call_clarify_{uuid.uuid4().hex[:8]}"
                args = json.dumps(
                    {"question": "Where would you like to travel, and for how many days?"}
                )
                tool_calls = [{
                    "id": call_id,
                    "type": "function",
                    "function": {"name": "AskUserForClarification", "arguments": args},
                }]
                return tool_calls, "", "tool_calls"

            itinerary_text = (
                "### Custom Travel Plan\n\n"
                "**Day 1:** Arrival, city center exploration, and local dining.\n"
                "**Day 2:** Iconic historical sights and scenic viewpoints.\n"
                "**Day 3:** Local markets, cultural activities, and departure.\n\n"
                "Have a wonderful trip!"
            )
            return None, itinerary_text, "stop"

        # Default fallback
        fallback_text = "I have processed your request and completed the task successfully."
        return None, fallback_text, "stop"

    def _stream_response(self, model: str, tool_calls: list[dict] | None, text_content: str, finish_reason: str):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

        req_id = f"chatcmpl-mock-{uuid.uuid4().hex[:8]}"
        created = int(time.time())

        if tool_calls:
            for idx, tc in enumerate(tool_calls):
                # Chunk 1: ID and Name
                chunk_meta = {
                    "id": req_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model,
                    "choices": [{
                        "index": 0,
                        "delta": {
                            "role": "assistant",
                            "tool_calls": [{
                                "index": idx,
                                "id": tc["id"],
                                "type": "function",
                                "function": {
                                    "name": tc["function"]["name"],
                                    "arguments": "",
                                },
                            }],
                        },
                        "finish_reason": None,
                    }],
                }
                self.wfile.write(f"data: {json.dumps(chunk_meta)}\n\n".encode("utf-8"))
                self.wfile.flush()

                # Chunk 2: Arguments
                chunk_args = {
                    "id": req_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model,
                    "choices": [{
                        "index": 0,
                        "delta": {
                            "tool_calls": [{
                                "index": idx,
                                "function": {
                                    "arguments": tc["function"]["arguments"],
                                },
                            }],
                        },
                        "finish_reason": None,
                    }],
                }
                self.wfile.write(f"data: {json.dumps(chunk_args)}\n\n".encode("utf-8"))
                self.wfile.flush()

            terminal_chunk = {
                "id": req_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [{
                    "index": 0,
                    "delta": {},
                    "finish_reason": "tool_calls",
                }],
            }
            self.wfile.write(f"data: {json.dumps(terminal_chunk)}\n\n".encode("utf-8"))
            self.wfile.flush()
        else:
            words = text_content.split(" ")
            for i in range(0, len(words), 4):
                chunk_text = " ".join(words[i:i + 4]) + (" " if i + 4 < len(words) else "")
                chunk = {
                    "id": req_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model,
                    "choices": [{
                        "index": 0,
                        "delta": {"role": "assistant", "content": chunk_text},
                        "finish_reason": None,
                    }],
                }
                self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode("utf-8"))
                self.wfile.flush()

            terminal_chunk = {
                "id": req_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [{
                    "index": 0,
                    "delta": {},
                    "finish_reason": finish_reason or "stop",
                }],
            }
            self.wfile.write(f"data: {json.dumps(terminal_chunk)}\n\n".encode("utf-8"))
            self.wfile.flush()

        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def _send_non_streaming_response(self, model: str, tool_calls: list[dict] | None, text_content: str, finish_reason: str):
        message: dict = {"role": "assistant"}
        if tool_calls:
            message["tool_calls"] = tool_calls
            message["content"] = None
        else:
            message["content"] = text_content

        response = {
            "id": f"chatcmpl-mock-{uuid.uuid4().hex[:8]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [{
                "index": 0,
                "message": message,
                "finish_reason": finish_reason or ("tool_calls" if tool_calls else "stop"),
            }],
            "usage": {
                "prompt_tokens": 15,
                "completion_tokens": 25,
                "total_tokens": 40,
            },
        }
        self._send_json(response)

    def log_message(self, format, *args):
        logger.info("%s - %s", self.address_string(), format % args)


def run_server(port: int = 8080):
    server = ThreadingHTTPServer(("0.0.0.0", port), MockLLMHandler)
    logger.info("Mock LLM Server running on port %d ...", port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down mock server.")
        server.shutdown()


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
    run_server(port)

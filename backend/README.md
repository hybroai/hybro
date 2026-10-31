# Hybro Backend

This directory is the canonical backend for the current Hybro repository and is
the backend used by the application, the single image, and CI.

## Run from the monorepo

### Single image

Configuration lives in `~/.hybro/config.json` (secrets in `~/.hybro/auth.json`).
The checkout's CLI builds the image from the working tree and runs it; the
entrypoint gives each service its validated JSON-derived projection:

```sh
./scripts/hybro start --build
```

The API is then at <http://localhost:3100/api/v1> and its health endpoint at
<http://localhost:3100/health>, on the same port as the app and MCP. A released
install uses port 3000; `--port` changes either.

### Run the backend directly

Python 3.12+ and MongoDB 4.2+ are required. Terminal projection scheduling uses
MongoDB aggregation update pipelines; the image embeds MongoDB 7.0.

```sh
cd backend
uv sync --extra dev
# HYBRO_HOME may point at another configuration directory (absolute path).
uv run uvicorn main:app --reload
```

Set `backend.auth_mode` to `mock` in `config.json` for local development without
Clerk credentials. Redis is optional for a single-process local server;
cross-process delivery and locking require it.

### Production upgrade note

Releases that introduce terminal task writer fencing must not be deployed with a
rolling mixed-version writer fleet. Coordinately drain or stop all old backend
writers, deploy the new version to every replica, and only then resume traffic.
An older writer does not apply the MongoDB terminal winner fences and can undo the
new version's guarantees.

## Project layout

- `main.py`: FastAPI application and lifespan entry point.
- `container.py`: runtime composition root.
- `api_gateway/`: the only HTTP route package.
- `agent/`, `room/`, `execution/`, `context_memory/`, `delivery/`: domain modules.
- `a2a_adapter/`: A2A SDK boundary.
- `llm_gateway/`: LLM provider boundary.
- `local_agents/`: Docker-host A2A Agent discovery and lifecycle coordination.
- `dal/`: MongoDB and Redis adapters.
- `room_files/`: room-owned file metadata and local content storage.
- `tests/`: unit, boundary, and workflow tests.

The former `api/` compatibility package has been removed. New routes belong in
`api_gateway/routes/` and must use injected owner protocols rather than concrete
repositories.

See [`docs/System-Architecture.md`](docs/System-Architecture.md) for the current
runtime architecture.

## Agent-network API

These endpoints let an authenticated client discover visible registered agents
and send one A2A message without creating a room. The frontend's **Connect > API**
(`/connect/api`) page provides copyable request examples; existing room chat continues to use its
authenticated REST/SSE workflow.

**Security boundary:** both routes use the existing `get_current_user` dependency.
With `backend.auth_mode="clerk"`, callers need a valid Clerk session; add
`-H "Authorization: Bearer <Clerk session JWT>"` to the curl examples below.
With local `backend.auth_mode="mock"`, the existing override supplies
`user_local_developer`; this mode must remain on a trusted network.
Discovery and sending are limited to public agents and the caller's own private
agents. Saved groups must belong to that caller. Discovery returns only the
Card fields a client needs to choose an agent (name, description, version,
protocol version, provider, documentation and icon URLs, capabilities, default
modes, and skills without per-skill `security`). Endpoint URLs, interfaces, and
security data stay inside Hybro; clients reach agents by `agent_id`. Secrets
embedded in those kept fields are not detected.

The `curl` examples below use a backend run directly on port 8000. Against the
image, use the app's address instead: `http://localhost:3000` for a released
install, `http://localhost:3100` from a checkout.

### Discover stored active agents

```sh
curl --fail-with-body --silent --show-error \
  'http://localhost:8000/api/v1/agents/discovery'

# Optional scope: use an existing saved group/Team ID from the frontend.
curl --fail-with-body --silent --show-error --get \
  --data-urlencode 'group_id=my-saved-team-id' \
  'http://localhost:8000/api/v1/agents/discovery'
```

The response shape is `{"agents":[{"agent_id":"...","agent_card":{...}}]}`.
Each `agent_card` is the stored raw Card, except that legacy
`authentication.credentials` is stripped. There is no live scan, Card refresh,
health probe, search ranking, or fixed inventory cap.

- Omit `group_id`, or use `all_agents`, for all visible stored active agents:
  public agents plus the caller's own private agents.
- A saved group/Team must belong to the caller and returns only its currently
  active, visible registered members. An empty group or one with no visible
  active members returns `{"agents":[]}`.
- Another owner's group returns HTTP `403`.
- An unknown group returns HTTP `404`.
- `room_team` returns HTTP `422`: this built-in selection requires room context
  and is not supported here.

### Choose an agent

Three read-only routes help a caller pick an agent without loading every Card.
They use the same authentication, visibility, and saved-group ownership rules
as discovery.

```sh
# Saved groups (subnets) owned by the caller, with active visible member counts.
curl --fail-with-body --silent --show-error \
  'http://localhost:8000/api/v1/agents/groups'

# Rank agents for a capability query, optionally within one saved group.
curl --fail-with-body --silent --show-error --get \
  --data-urlencode 'query=weather forecast' \
  --data-urlencode 'group_id=my-saved-team-id' \
  --data-urlencode 'limit=5' \
  'http://localhost:8000/api/v1/agents/search'

# One agent's consumer profile.
curl --fail-with-body --silent --show-error \
  'http://localhost:8000/api/v1/agents/registered-agent-id/profile'
```

- `GET /agents/groups` returns `{"groups":[{"group_id","name","description","agent_count"}]}`.
  Built-in `all_agents` and `room_team` selections are not listed.
- `GET /agents/search` requires `query` (1-500 characters) and accepts
  `limit` (1-20, default 5) and `group_id` with the same scope rules as
  discovery. Ranking is lexical over Card names, descriptions, skill names,
  tags, and descriptions, so short capability keywords work best. Each match
  has `agent_id`, `name`, `description`, compact `skills`, `input_modes`,
  `output_modes`, the caller's `groups` containing the agent, and `score`.
  An empty list means no agent matched the query terms.
- `GET /agents/{agent_id}/profile` returns `status`, `name`, `description`,
  `version`, `provider`, `streaming`, modes, skills with examples and modes,
  and `groups`. It leaves out URLs, transport interfaces, and security
  details because Hybro makes the call. Missing or invisible agents return `404`.

### Send one message

Replace `registered-agent-id` with an `agent_id` from discovery; the ID belongs
in the request body, not in the URL. This example assumes an agent that can
multiply two numbers:

```sh
curl --fail-with-body --silent --show-error \
  'http://localhost:8000/api/v1/agents/messages' \
  -H 'Content-Type: application/json' \
  --data '{
    "agent_id": "registered-agent-id",
    "client_request_id": "multiply-17-23-1",
    "message": {
      "role": "user",
      "messageId": "multiply-message-1",
      "parts": [{"kind": "text", "text": "Multiply 17 by 23."}]
    }
  }'
```

Add `"group_id":"my-saved-team-id"` to the outer object to enforce saved-group
membership and ownership at send time. Without it (or with `all_agents`), any
visible registered active agent may be targeted. The service reloads visibility,
status, and group ownership rather than trusting an earlier discovery response.

`client_request_id` and `message.messageId` are generated when omitted; the
message role defaults to `user`, the only accepted caller role. `parts` must be
nonempty. Request text parts use the existing common A2A shape with `kind:"text"`;
supported data and file parts use their corresponding common-model shapes.
Room file IDs alone are not accepted as file content.

An illustrative Task response is:

```json
{
  "agent_id": "registered-agent-id",
  "client_request_id": "multiply-17-23-1",
  "result": {
    "kind": "task",
    "id": "remote-task-1",
    "contextId": "remote-context-1",
    "status": {
      "state": "TASK_STATE_COMPLETED",
      "message": {
        "messageId": "remote-reply-1",
        "role": "ROLE_AGENT",
        "parts": [{"text": "PRODUCT=391"}]
      }
    }
  }
}
```

The remote agent can instead return a Message, for example:

```json
{
  "agent_id": "registered-agent-id",
  "client_request_id": "multiply-17-23-1",
  "result": {
    "kind": "message",
    "messageId": "remote-reply-1",
    "role": "ROLE_AGENT",
    "parts": [{"text": "PRODUCT=391"}]
  }
}
```

These are shape examples, not a promise about any agent's output. `result`
preserves the SDK's A2A 1.0 ProtoJSON fields plus the adapter's top-level `kind`
discriminator: enums are `ROLE_AGENT` / `TASK_STATE_COMPLETED`, and result text
parts have `text` **without** legacy `kind:"text"`. Do not deserialize results
as the legacy request model or expect a flattened text answer. Task history,
artifacts, metadata, status messages, and continuation IDs remain in the result.
A nonterminal or failed Task also returns HTTP `200`; inspect its state.

### Continuation and failure semantics

To continue an agent conversation, keep its returned `contextId`. To continue a
specific Task, also put its returned `id` into the next `message.taskId`. Send a
new user message to the same agent with a new `messageId`, for example:

```json
{
  "agent_id": "registered-agent-id",
  "client_request_id": "follow-up-2",
  "message": {
    "messageId": "follow-up-message-2",
    "contextId": "remote-context-1",
    "taskId": "remote-task-awaiting-input",
    "parts": [{"kind": "text", "text": "Use whole numbers."}]
  }
}
```

Use IDs actually returned by that agent. Omit `taskId` for a new task in a reusable
context; do not assume a completed Task can be reopened. The remote agent owns
continuation validity. This API does not bind task/context IDs to a
caller, store conversation history, or provide room/HITL/LLM orchestration.

The call has a **600-second deadline** and performs one blocking send, without
automatic retries or Docker-host fallback. A timeout stops the local wait,
**not the remote task**: no A2A cancellation is sent, and the agent may continue
working after HTTP `504` or client disconnection. `client_request_id` is only a
correlation ID, not an idempotency key; resubmitting the same request can repeat
remote effects. There are no polling, cancellation, or SSE endpoints for this
network API.

`POST /agents/messages` accepts at most **6 MiB** of total request-body bytes,
including JSON and base64 file content. The limit also applies without
`Content-Length`. Each backend worker admits at most **four concurrent requests**,
including requests still receiving their body. Additional requests receive HTTP
`429` immediately; there is no waiting queue. Slots are released when the request
exits, including errors and cancellation. These limits are per worker, not a
distributed quota; remote work may still outlive a timed-out request.

Errors use HTTP `401` for missing/invalid authentication, `403` for another owner's
group, `404` for a missing group or missing/invisible agent, `409` for an inactive
or out-of-group visible agent, `413` for an oversized body, `422` for invalid input
or `room_team`, `429` for exhausted concurrent admission, `502` for upstream
transport/protocol or invalid-result failures, and `504` for timeout.
Domain errors have `detail.code` and `detail.message`; domain send errors also
include `detail.agent_id` and `detail.client_request_id`. Authentication and
middleware errors use a string `detail`; schema validation errors use FastAPI's
validation-detail array.

Implementation ownership and protocol inventory are documented in
[`System Architecture`](docs/System-Architecture.md#agent-network-api).

## Sessions API

A session is a conversation a client of the instance owner opens with the
owner's agents. Each message names one agent; every turn is recorded in Hybro;
when an agent needs the owner's permission, the owner approves or rejects in the
app (**Sessions** in the sidebar). Under local `backend.auth_mode="mock"` the
client and the owner share one identity, so the separation is a presentation
rule.

```sh
# Open a session (client_label is required without session_id).
curl --request POST http://localhost:8000/api/v1/sessions/messages \
  --header 'Content-Type: application/json' \
  --data '{"agent_id": "<agent_id>", "text": "Generate a travel plan for Kyoto.",
           "client_request_id": "turn-1", "client_label": "Buyer agent"}'

# Continue it, possibly with another agent.
curl --request POST http://localhost:8000/api/v1/sessions/messages \
  --header 'Content-Type: application/json' \
  --data '{"session_id": "<session_id>", "agent_id": "<agent_id>",
           "text": "Now list the hotels.", "client_request_id": "turn-2"}'

# Check, close; the owner list and record include the conversation id.
curl http://localhost:8000/api/v1/sessions/<session_id>
curl --request POST http://localhost:8000/api/v1/sessions/<session_id>/close
curl http://localhost:8000/api/v1/sessions
curl http://localhost:8000/api/v1/sessions/<session_id>/record
```

A send returns a receipt as soon as Hybro has the message: `session_id`,
`client_request_id`, `agent_id`, `status` (`created` when it opened the
session, `accepted` for a later message, `busy` when it was not recorded
because an earlier message is still active), `state`, `message` (a short
confirmation with the tip to query the session), and the session's
`created_at` and `updated_at` (UTC). The agents' responses come from
`GET /sessions/{session_id}`: `state`, `reason`, `agents`, `created_at`,
`updated_at`, and `history`, which lists each client message oldest first with
its `text`, `sent_at`, `agent_id`, `agent_name`, `state`, `reason`, and the
agent's final `reply` and `replied_at`. Agents' questions to the owner and the
owner's decisions are never included; a waiting request shows only
`waiting_on_owner`. A request with no run event within
`backend.session_run_start_grace_seconds` (10) of its send reports `failed`
with `run_not_started`. Text is limited to 10,000 characters; `data` and files
are not accepted. Retrying with the same `client_request_id`, including an
earlier request's, returns that request's receipt without sending it again.

Under mock auth anyone who can call the HTTP API holds the owner's identity,
so the owner-only routes (`GET /sessions`, `GET /sessions/{session_id}/record`)
and the room routes behind `conversation_id` are reachable too. The MCP adapter
calls only the client routes. Agents run in passthrough mode:
no orchestrator model turns, one call to the named agent, and a fresh A2A
context per message (the agent does not receive the session history).

An agent that takes part in sessions must publish a standard Agent Card and be
registered; return `input-required` with the typed `hybro.ai/a2a/interaction`
metadata to ask the owner (an untyped `input-required` completes as an ordinary
reply); support `tasks/get`, since late results arrive by polling; and accept a
best-effort remote cancel meaning "stop; this request will not proceed", sent
on owner reject and on client close.

## Local MCP access

The repository-root [`MCP/`](../MCP/) directory contains a separate Python
Streamable HTTP adapter for the agent-network and session APIs. It does not
import the backend runtime, access the database, or change Agent visibility
rules.

The single image runs MCP as one of its programs and serves it at `/mcp` on the
app's port, only on a loopback listener: `http://localhost:3000/mcp` for a
released install, `http://localhost:3100/mcp` from a checkout. Use a trusted
backend in `backend.auth_mode="mock"`.

Add a **Streamable HTTP** server manually to your MCP client using:

```text
http://localhost:3000/mcp
```

From a checkout, use `http://localhost:3100/mcp`.

This URL is for clients on the machine running Hybro, not a remote browser's
machine. **Connect** (`/connect/mcp`) in the app copies the URL and per-assistant
setup snippets, such as the Claude Code `mcpServers` configuration
(`type: "http"`); the snippets can also be selected and copied manually. It does
not check status, control services, relay tool calls, or automatically
discover/register clients. The Overview **Gateway** tile shows MCP status.

Native manual startup remains supported from the repository root, with a backend
already running at `http://127.0.0.1:8000/api/v1/`:

```sh
uv run --project backend --frozen --extra mcp --no-env-file python MCP/server.py
```

The adapter runs from the backend environment with its `mcp` extra (one lock,
`backend/uv.lock`) and exposes seven tools. Backend
saved groups appear as **subnets** (`subnet_id`) in tool arguments and results.

- `list_subnets()`: the caller's subnets with active member counts, from
  `GET /agents/groups`. The app's internal preset tag
  (`[hybro-use-case:<id>]`) is removed from descriptions.
- `find_agents(query, subnet_id?, limit?)`: ranked compact matches from
  `GET /agents/search`, each listing its subnets.
- `get_agent(agent_id)`: one agent's consumer profile from
  `GET /agents/{agent_id}/profile`.
- `discover_agents(subnet_id?)`: every active agent with its public Card
  fields, from `GET /agents/discovery`. Output is large; prefer `find_agents` to choose.
- `send_message(agent_id, text, session_id?, subnet_id?, client_request_id?,
  client_label?, include_raw?)`: sends one message in a session through
  `POST /sessions/messages` and returns the receipt. Without `session_id` it
  opens a session; the receipt carries the `session_id` to pass back.
- `check_session(session_id)`: the session state and its history of client
  messages and agents' replies, from `GET /sessions/{session_id}`.
- `close_session(session_id)`: `POST /sessions/{session_id}/close`.

For example, after `find_agents`, call `send_message` with:

```json
{
  "agent_id": "<an ID returned by find_agents>",
  "text": "Generate a travel plan for Kyoto."
}
```

The result is:

```json
{
  "session_id": "b-S7K-y3wXKVefny4gvETXhcKJGj80Wo",
  "agent_id": "...",
  "client_request_id": "...",
  "status": "created",
  "state": "working",
  "message": "Created. Use session_id to query this session (MCP check_session, or GET /sessions/{session_id}) for the agents' responses.",
  "created_at": "2026-10-15T22:13:51.445000Z",
  "updated_at": "2026-10-15T22:13:52.674000Z",
  "next_action": "Hybro has the message. Call check_session with this session_id for the agents' responses; do not resend."
}
```

`status: busy` means the message was not recorded because an earlier message
in the session is still active; send it again once `check_session` shows that
request finished. `check_session` returns `state` (`completed`, `working`,
`waiting_on_owner` while the owner decides in the Hybro app, `rejected`,
`failed`, or `closed`) and `history`; the latest reply is the last entry's
`reply`. A `failed` receipt sets `isError`. The client view never contains the
owner's question or answer. `include_raw: true` adds the backend response as
`raw`. A missing `client_request_id` is generated; a retried call must pass the
same value to get the same receipt instead of a new send.
A missing `client_label` defaults to "MCP client" when a session is opened.

Every tool publishes an output schema. Results are sent as both MCP structured
content and JSON text. HTTP/transport errors set `isError`, keep the backend
`detail`, and add a `next_action` hint; a `409` hint depends on the backend code
(`session_closed`: start a new session; `request_conflict`: use a new
`client_request_id`; otherwise the agent is inactive or outside the subnet).

The adapter allows 610 seconds per backend call; a session send returns its
receipt at once. There are no automatic retries.

Native mode uses fixed loopback endpoints and the `/api/v1` prefix. Image mode
(`--image`) requires the entrypoint's validated `HYBRO_MCP_CONFIG` projection,
containing only `api_prefix`; it reaches the backend on loopback and listens on
`127.0.0.1:8001`, behind Caddy's `/mcp` route. Explicit Host/Origin
DNS-rebinding checks remain active in both modes. MCP receives no auth store or
Provider credentials.

`GET http://127.0.0.1:8001/health` makes a read-only backend `agents/discovery`
request with a two-second deadline and returns only `service: "hybro-mcp"` and
`status: "ready" | "unavailable" | "unsupported_auth"`. It never executes
agents or exposes Cards. A rejected authentication probe (`401`/`403`) reports
`unsupported_auth`, including with Clerk. The public frontend `GET /hybro-mcp` route exposes
only this sanitized status, not backend data or credentials.

The adapter has **no MCP authentication or user isolation** and supplies no
Clerk credentials: Clerk-mode tool requests fail rather than bypassing auth.
Use an authenticated REST client for Clerk-mode access; do not disable production
authentication to use this adapter. Do not expose the adapter publicly. The
adapter only calls the public agent-network API; Execution ownership is unchanged.

To run the isolated regression tests from the repository root:

```sh
uv run --project backend --frozen --extra mcp --extra dev --no-env-file pytest MCP/tests
```

The `MCP CI (local adapter)` job runs this same frozen, dotenv-disabled test
command in the backend environment.

## Validation

Run from `backend/`:

```sh
uv run ruff format --check .
uv run ruff check .
uv run pytest -m core
uv run pytest
```

Real Redis integration tests are explicit:

```sh
HYBRO_TEST_REDIS_URL=redis://localhost:6379/0 uv run pytest -m integration
```

See [`tests/README.md`](tests/README.md) for test lanes and cleanup conventions.

## Cleanup reports

Both scripts are read-only. Run from `backend/`:

```sh
uv run python -m scripts.report_orphan_modules
uv run python -m scripts.check_legacy_lifecycle_readiness
```

`report_orphan_modules` lists production modules that no entry point imports.
`check_legacy_lifecycle_readiness` counts the legacy lifecycle and data-shape
records in the configured Mongo database (or `--mongo-url`/`--database`) that
must reach zero before each legacy-removal step; each line names the step it
gates.

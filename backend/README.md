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

Every owner route needs the owner's sign-in cookie (`hybro_session`), on every
install; Clerk sign-in and the `?token=` query parameter are gone, and
`backend.auth_mode` is deprecated (see
[configuration](../docs/configuration.md#deprecated-settings)). The owner
account is created through the setup link or `hybro owner create`, which use
the machine-only routes of the image; see CONTRIBUTING.md for a checkout.
Redis is optional for a single-process local server; cross-process delivery
and locking require it.

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

These read-only endpoints let an authenticated client discover visible
registered agents and choose one. Work is sent to an agent through a session
(see [Sessions API](#sessions-api)). The frontend's **Access > API**
(`/connect/api`) page provides copyable request examples; existing room chat continues to use its
authenticated REST/SSE workflow.

**Security boundary:** these routes are part of the Source API and act for
the instance owner on behalf of a source (see [Sources and
tokens](#sources-and-tokens)). Until the owner account exists every Source
API request is refused with `503 no_account`. A fresh install requires a
source token: add `-H "Authorization: Bearer <source token>"` to the curl
examples below, with a token from **Access > Sources** or
`hybro source create --name <name>`. While the owner has turned the
requirement off (an install upgraded with data starts with it off), a request
without a token is accepted as the built-in "No token" source, so Hybro must
stay on a trusted network. Discovery is limited to public agents and the owner's own
private agents. Saved groups must belong to that caller. Discovery returns only the
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

Errors use HTTP `401` for missing/invalid authentication, `403` for another
owner's group, `404` for a missing group or missing/invisible agent, and `422`
for invalid input or `room_team`. Domain errors have `detail.code` and
`detail.message`. Authentication errors use a string `detail`; schema
validation errors use FastAPI's validation-detail array.

Implementation ownership and protocol inventory are documented in
[`System Architecture`](docs/System-Architecture.md#agent-network-api).

## Sources and tokens

A source is a program that sends work to Hybro, such as a CI job or an MCP
client. The owner creates one and gets its token once:

```sh
hybro source create --name "CI bot"
```

or, signed in as the owner, through the owner routes: `GET /sources`,
`POST /sources` (returns the token once), `PATCH /sources/{source_id}`
(rename), `POST /sources/{source_id}/rotate` (a new token; the old one stops
working) and `POST /sources/{source_id}/revoke`. Hybro stores only a SHA-256
hash of a token and its last four characters.

The Source API is the seven routes a source calls: `GET /agents/discovery`,
`GET /agents/groups`, `GET /agents/search`, `GET /agents/{agent_id}/profile`,
`POST /sessions/messages`, `GET /sessions/{session_id}` and
`POST /sessions/{session_id}/close`. Each takes an optional
`Authorization: Bearer <source token>` header:

- A current token names the source on its sessions.
- An Authorization header that is not a current source token of this Hybro
  (unknown, revoked, rotated, or a leftover header of another kind) is refused
  with `401 unknown_source_token`.
- No header: the request is the built-in "No token" source. It is refused with
  `401 source_token_required` when a token is required (the owner's switch
  under **Access > Sources**, on for a fresh install and always with
  `backend.auth_mode="clerk"`), and with `403 cross_site_request` when it
  comes from a browser page on another site.

A session belongs to the source that opened it: another source, or a request
without a token, cannot read, continue or close it (`404 session_not_found`).


A session is a conversation a client of the instance owner opens with the
owner's agents. Each message names one agent; every turn is recorded in Hybro;
when an agent needs the owner's permission, the owner approves or rejects in the
app (**Inbox** in the sidebar). Sessions are stored under the owner's
identity; a source reaches only the sessions it opened, and the owner's routes
need the owner's sign-in.

```sh
# Open a session. Without a token, client_label is required when session_id
# is omitted; with one it is optional and the source's name is used.
curl --request POST http://localhost:8000/api/v1/sessions/messages \
  --header 'Content-Type: application/json' \
  --data '{"agent_id": "<agent_id>", "text": "Generate a travel plan for Kyoto.",
           "client_request_id": "turn-1", "client_label": "Buyer agent"}'

# Continue it, possibly with another agent.
curl --request POST http://localhost:8000/api/v1/sessions/messages \
  --header 'Content-Type: application/json' \
  --data '{"session_id": "<session_id>", "agent_id": "<agent_id>",
           "text": "Now list the hotels.", "client_request_id": "turn-2"}'

# Check and close, as the source that opened the session.
curl http://localhost:8000/api/v1/sessions/<session_id>
curl --request POST http://localhost:8000/api/v1/sessions/<session_id>/close

# The owner's list, record and close (signed in as the owner); they include
# the conversation id and the source (source_id, name, has_token).
curl http://localhost:8000/api/v1/sessions
curl http://localhost:8000/api/v1/sessions/<session_id>/record
curl --request POST http://localhost:8000/api/v1/sessions/<session_id>/record/close
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
with `run_not_started`, unless the backend is still running the task that starts
the run. Text is limited to 10,000 characters; `data` and files
are not accepted. Retrying with the same `client_request_id`, including an
earlier request's, returns that request's receipt without sending it again.

The owner-only routes (`GET /sessions`, `GET /sessions/{session_id}/record`,
`POST /sessions/{session_id}/record/close`) and the room routes behind
`conversation_id` need the owner's sign-in. The MCP adapter
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
Streamable HTTP adapter for the agent-network and session APIs. It imports
only the backend's `common.observability` logging modules, not the runtime, and
does not access the database or change Agent visibility rules.

The single image runs MCP as one of its programs and serves it at `/mcp` on the
app's port, on every listener: `http://localhost:3000/mcp` for a released
install, `http://localhost:3100/mcp` from a checkout. An MCP client on another
machine connects with a source token to the address it reaches Hybro by, once
Hybro listens there (for now with `--insecure-no-auth`).

Each tool passes the `Authorization` header of the MCP request on to the
backend. Set `Authorization: Bearer <source token>` in your MCP client's
server configuration to name it as a source; it is required while the token
requirement is on, as on a fresh install. Without the header, and with the
requirement off, the calls are the "No token" source. Each call also carries
`X-Hybro-Via: mcp`, so the backend records that the source last connected
through MCP; in the image, Caddy's backend route removes that header from
requests that arrive on the app's port (a checkout's Next dev proxy does not,
so there a client can claim it; the mark decides nothing). Until the owner account exists the tools fail with
`no_account`, and their `next_action` points the user to the setup link.

Add a **Streamable HTTP** server manually to your MCP client using:

```text
http://localhost:3000/mcp
```

From a checkout, use `http://localhost:3100/mcp`.

The `localhost` URL is for clients on the machine running Hybro, not a remote
browser's machine. **Access** (`/connect/mcp`) in the app copies the URL and per-assistant
setup snippets, such as the Claude Code `mcpServers` configuration
(`type: "http"`); the snippets can also be selected and copied manually. It does
not check status, control services, relay tool calls, or automatically
discover/register clients. The Overview **Gateway** tile shows MCP status.

Native manual startup remains supported from the repository root, with a backend
already running at `http://127.0.0.1:8000/api/v1/`:

```sh
PYTHONPATH=backend uv run --project backend --frozen --extra mcp --no-env-file python MCP/server.py
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
A missing `client_label` defaults to "MCP client" when a session is opened
without a token; with a token the backend uses the source's name.

Every tool publishes an output schema. Results are sent as both MCP structured
content and JSON text. HTTP/transport errors set `isError`, keep the backend
`detail`, and add a `next_action` hint; a `401` hint asks for a current
source token, and a `409` hint depends on the backend code
(`session_closed`: start a new session; `request_conflict`: use a new
`client_request_id`; otherwise the agent is inactive or outside the subnet).

The adapter allows 610 seconds per backend call; a session send returns its
receipt at once. There are no automatic retries.

Native mode uses fixed loopback endpoints and the `/api/v1` prefix. Image mode
(`--image`) requires the entrypoint's validated `HYBRO_MCP_CONFIG` projection,
containing `api_prefix` and the listen mode (`listen`: `loopback` or
`public`); it reaches the backend on loopback and listens on `127.0.0.1:8001`,
behind Caddy's `/mcp` route. Native mode and a loopback listener keep the
explicit Host/Origin DNS-rebinding check (only `localhost` and `127.0.0.1`, any
port), on `/mcp` and `/health`. On a public listener that check is off, because
other machines reach Hybro by names the server cannot know. Instead each
backend call carries the caller's address: the `X-Forwarded-Host` Caddy set (it
drops a client's own), or the request's `Host` without one, and the caller's
`Origin` when it sent one; a call with neither header fails with
`CALLER_ADDRESS_UNKNOWN` and sends nothing. The backend's trusted-host check then refuses a name
it does not answer to (421 `unknown_host`, whose `next_action` names
`frontend.server_url`), and its cross-site rule refuses a web page without a
token, as for a direct API call; the source token is the control. Calls that
never reach the backend, `initialize`, `tools/list` and `/health`, answer any
name and return no data: the tool list and a ready flag. A checkout runs the
server natively on loopback, so it keeps the host check and passes no address
on. MCP receives no auth store or
Provider credentials.

`GET http://127.0.0.1:8001/health` calls the backend's root `/health`, which
needs no credential, with a two-second deadline, and returns only
`service: "hybro-mcp"` and `status: "ready" | "unavailable"`. It is `ready`
only when the backend answers `{"status": "ok"}`; a degraded backend (for
example while the agent search index is not ready) reports `unavailable`. It
sends no `Authorization`, never executes agents and exposes no Cards. The
public frontend `GET /hybro-mcp` route exposes only this sanitized status, not
backend data or credentials.

The adapter has **no MCP authentication of its own**: it forwards the caller's
`Authorization` header, and the backend's source token rules apply. It never
holds the owner's sign-in. Do not expose the adapter publicly. The
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

# Configuration

Hybro reads one JSON configuration for the backend, the frontend, and the
bundled agents. This page covers the files, how to edit them, and the backend
fields most deployments change. Start options such as the port and listener are
`hybro` flags; see the [README](../README.md#running).

## Configuration contract

Backend, frontend and bundled-agent user settings belong to one local JSON
configuration, not repository environment files:

```text
~/.hybro/
├── config.json   # User settings; the Settings page or a text editor
└── auth.json     # Credentials; the Settings page and the container manage it
```

`HYBRO_HOME` is the sole directory override and must be an absolute path.
Defaults, types and validation remain in a thin code loader; JSON only needs
user overrides. `backend/common/config/loader.py` replaces the old `settings.py`
environment-loading path. Startup validates syntax, known fields, types, ranges and
required combinations. A missing model provider is allowed: Room Chat and the
LLM-backed agents stay inactive and the app points to Settings.

Settings page edits use the same validation before saving. Manual JSON edits are
validated at the next startup. Each service reads its settings when it starts.
The exceptions are the model provider and models, which reload live, and
`backend.log_level` saved through the [Settings API](#settings-api), which
applies to the running backend at once. Everything else applies after a
restart: the Settings API restarts the services in place in the image, and
`hybro stop` and `hybro` always work.

`.env`, `.env.example` and `frontend/.env.local` are not required by this contract.
The container's entrypoint supplies only scoped values to each service; the
CLI's transport variables (`HYBRO_LISTEN`, `HYBRO_PUBLIC_PORT`,
`HYBRO_INSECURE_NO_AUTH`) do not become another user configuration source. Secrets
must never enter the browser bundle or be forwarded wholesale to Agents.

On every start the container also creates a diagnostics token in
`/tmp/hybro/diagnostics.json` (mode 0600, inside the container, outside
`auth.json`), readable only by the container's service user; `hybro doctor`
reads it through `docker exec`. It is replaced on the next start and is not a
configuration field.

The entrypoint also writes `/tmp/hybro/runtime.json` (mode 0600): this start's
listener mode, public port, whether MongoDB is embedded, and the revision of the
configuration the services were started with. The restart helper writes its
progress to `/tmp/hybro/restart.json`. Both are runtime records inside the
container, rewritten on each start or restart; neither is a configuration
source.

## Edit configuration

Edit `~/.hybro/config.json` by hand, on the Settings page under
**Configuration**, or through the [Settings API](#settings-api), then restart:

```json
{
  "version": 1,
  "backend": { "log_level": "DEBUG" },
  "frontend": { "max_message_length": 8000 }
}
```

```bash
hybro stop && hybro
```

Use the Settings page for Provider and model changes. Startup generates missing
internal tokens in `auth.json` without rotating existing values. Backend overrides live under `backend`, public
frontend overrides under `frontend`, and image output size is `image_size`.

Startup rules added with the Settings API (all under `backend` unless named
`frontend`):

- Greater than 0: the Fast and Ultimate `max_agent_calls`, `max_parallel_calls`
  and `deadline_seconds`, `agent_health_check_interval`,
  `llm_gateway_request_timeout_seconds` and `llm_gateway_stream_timeout_seconds`.
- `local_agent_discovery_port_start` must not exceed the port end, including the
  default end.
- At most 87600 (ten years): `approval_deadline_hours` and
  `orchestrator_call_wait_ceiling_hours`.
- At most 315360000 (ten years): the Fast, Ultimate and passthrough
  `deadline_seconds`.
- At most 10000: the Fast and Ultimate `max_model_turns` and `max_agent_calls`.
- At most 100: `orchestrator_passthrough_max_transport_retries_per_call`.
- At most 36500: `compaction_content_ttl_days`.
- At most 9007199254740991 (2**53 - 1): any integer backend setting.
- `frontend.max_message_length` at most 10000 (the backend refuses longer
  messages) and `frontend.inspection_timeout_ms` at most 2147483647 (what the
  browser can use).

Before, such values passed startup and then failed every Run, stopped the
backend from booting, or made the health check loop spin; now startup rejects
them and names the field. Fix the value in `config.json` if Hybro no longer
starts after an upgrade.

A profile's `max_parallel_calls` above its `max_agent_calls` (including the
default parallel value when only `max_agent_calls` is lowered) is capped to
`max_agent_calls` instead of failing. `config.json` keeps the entered value;
the Settings API shows the capped value and a `capped_fields` notice.

## Settings API

The backend serves the instance settings under `{api_prefix}/settings`
(`/api/v1/settings` by default). The Settings page's Configuration and System
sections are its client.

| Route | Behavior |
|---|---|
| `GET /settings` | The editable fields with their value, default, bounds, group, whether each applies `live` or after a `restart`, and per-field `restart_pending`; a top-level `revision`, `restart_pending` and restart status; notices for retired, ignored, unlisted, or capped keys (names only); and read-only system information. |
| `PUT /settings` | `{"revision": "<from GET>", "values": {"backend.log_level": "DEBUG", ...}}`. Saves the given fields; `null` removes a key, so it returns to its default. Returns the keys that changed, `restart_required`, and the new view. |
| `POST /settings/restart` | 202. Body `{}` with `Content-Type: application/json` (otherwise 415 `json_required`, so another site cannot send it as a simple cross-site POST). Checks the saved configuration and restarts the services in place (image only). |

Every route is admin-only, including `GET`: anyone in `mock` auth mode, and in
`clerk` mode only users in `backend.admin_user_ids`; others get 403
`forbidden`.

Only the curated registry fields can be read or written: run limits, the
agent health check, local agent discovery, message length, compaction, log
level and format, and the advanced orchestrator, passthrough, session and LLM
gateway limits. Other keys, such as infrastructure URLs, internal tuning, and
credentials, are never returned, and a write to one fails with
`unknown_field`. `provider`, `models`, `image_size` and `auth.json` are never
touched; the model provider keeps its own setup routes.

A write uses the startup validation rules. Values must have the field's JSON
type (`"12"` is not an integer), and the normalized value is stored (for
example `"debug"` becomes `"DEBUG"`). Errors return 422 `invalid_settings` with
per-field `errors` (`field`, `code`, `message`) that never echo the submitted
value. A save also drops retired keys and the 0.3 `instances` block, as setup
saves do.

The `revision` token covers `backend`, `frontend` and `image_size`, not the
model provider. When `config.json` changed since the editor read it, `PUT`
returns 409 `revision_conflict`; reload and retry. An invalid `config.json`
returns 409 `config_invalid`; `GET` then shows `config_error` and the running
values.

`backend.log_level` applies live. Every other field applies after a restart,
and `restart_pending` reports a saved change the running services do not use
yet, including `image_size` and hand edits. The Settings page offers **Save**
(`PUT` only) and **Save & restart** (`PUT`, then `POST /settings/restart`).

The restart re-runs the start's configuration check and, when it passes,
restarts the backend, frontend, MCP and bundled agents through supervisord;
Caddy and the embedded MongoDB keep running, so the port stays open and data
stays up. The restart refuses a change it cannot apply in place, such as a
hand-edited `api_prefix` or switching between embedded and external MongoDB,
with "This change needs a full restart: run `hybro stop` and `hybro`." An
invalid configuration returns 409 `config_invalid` and nothing restarts; a
restart already running returns 409 `restart_in_progress`. From a checkout it
returns 409 `restart_unavailable`: restart the processes yourself. Like a start,
a restart may create a missing internal token in `auth.json`. The helper writes
one logfmt summary line to `hybro logs` (`restart | level=info ...`, or
`level=error` for a failure, which the foreground `hybro` view also shows).

The port, listener mode, and auth mode are shown read-only. The port and
listener are `hybro` flags (`--port`, `--listen`, `--insecure-no-auth`), and
`backend.auth_mode` is not a registry field: change it in `config.json` and
run `hybro stop` and `hybro`.

## Model access for Agents

The already-implemented Agent proxy shares backend's setup-selected gateway.
Agents retain SDKs, tools, HITL and A2A execution. Agents receive a scoped
`HYBRO_AGENT_CONFIG` projection containing the backend proxy URL, a separate
inference token and image size. They pass these values explicitly to their SDKs.
The token cannot authenticate as a user; it is required even under
mock auth. Agents never receive Provider credentials or the setup directory.
See [gateway architecture](../backend/docs/System-Architecture.md#llm_gateway) for
the supported proxy subset and limits.

## Backend configuration fields

Waits for the owner or for a remote Agent:

| Field | Default | Meaning |
|---|---|---|
| `backend.approval_deadline_hours` | `null` (no deadline) | Hours before an unanswered supervisor question expires and fails its Run. Must be greater than 0 when set. |
| `backend.orchestrator_call_wait_ceiling_hours` | `168` (seven days) | Hours an acknowledged remote Agent task is polled with `tasks/get` before the call expires. Must be greater than 0. |
| `backend.a2a_push_callback_base_url` | `""` (push disabled) | Base URL that remote Agents can reach. Must be `https`; plain `http` is accepted only for loopback (`localhost`, `127.0.0.0/8`, `::1`), `host.docker.internal`, or a private RFC 1918 address, because the per-call push token travels in a request header. When set, Hybro registers a per-call push notification URL with Agents whose card advertises push support, so late results arrive without waiting for the next poll. Polling stays the fallback. |

```json
{
  "backend": {
    "approval_deadline_hours": 48,
    "orchestrator_call_wait_ceiling_hours": 72.5,
    "a2a_push_callback_base_url": "https://hybro.example.com"
  }
}
```

A Run waiting for an answer or a remote Agent does not count that time against
its profile deadline, so it can wait as long as these limits allow. While it
waits it keeps the room's single active-Run slot.

Passthrough Runs (session sends, no orchestrator model turns):

| Field | Default | Meaning |
|---|---|---|
| `backend.orchestrator_passthrough_deadline_seconds` | `300` | Active seconds a passthrough Run may take; time spent waiting for the owner or a remote Agent does not count. Must be greater than 0. |
| `backend.orchestrator_passthrough_max_transport_retries_per_call` | `2` | Transport retries for the single Agent call. Must be greater than 0. |

Sessions:

| Field | Default | Meaning |
|---|---|---|
| `backend.session_run_start_grace_seconds` | `10` | Seconds after a send without any lifecycle event for the request before the session reports `failed` with `run_not_started`, unless this process is still running the task that starts the run. Must be greater than 0. |

Local agent discovery (the image scans the Docker host for A2A Agents):

| Field | Default | Meaning |
|---|---|---|
| `backend.local_agent_discovery_port_start` | `9000` | First `host.docker.internal` port scanned for an Agent Card. From 1 to 65535. |
| `backend.local_agent_discovery_port_end` | `9999` | Last port scanned. From 1 to 65535 and not below `local_agent_discovery_port_start`. An Agent on a port outside the range is not discovered, and one discovered earlier is marked inactive after three scans without it; widen the range or register the Agent manually. |

Logging:

| Field | Default | Meaning |
|---|---|---|
| `backend.log_level` | `INFO` | Lowest level the backend writes: `DEBUG`, `INFO`, `WARNING`, `ERROR`, or `CRITICAL`. |
| `backend.log_format` | `auto` in a checkout, `json` in the image | `json`, `logfmt`, or `auto` (logfmt when `backend.app_env` is `development`, JSON otherwise). The image defaults to `json` so backend lines in `hybro logs` are machine-readable; a value in `config.json` wins. |
| MCP server (not configurable) | `INFO`; JSON in the image | The MCP server logs JSON at level `info` in the image (logfmt from a checkout) and does not read `backend.log_level` or `backend.log_format`. |

`hybro logs --service <name> --level <level> --trace <id>` filters the
container's log stream on the host; these are command options, not
configuration fields.

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
validated at the next startup. Settings other than the model provider are a
startup snapshot: run `hybro stop` and `hybro` after changes.

`.env`, `.env.example` and `frontend/.env.local` are not required by this contract.
The container's entrypoint supplies only scoped values to each service; the
CLI's transport variables (`HYBRO_LISTEN`, `HYBRO_PUBLIC_PORT`,
`HYBRO_INSECURE_NO_AUTH`) do not become another user configuration source. Secrets
must never enter the browser bundle or be forwarded wholesale to Agents.

## Edit configuration

Edit `~/.hybro/config.json`, then restart:

```json
{
  "version": 1,
  "backend": { "log_level": "DEBUG" },
  "frontend": { "max_message_length": 12000 }
}
```

```bash
hybro stop && hybro
```

Use the Settings page for Provider and model changes. Startup generates missing
internal tokens in `auth.json` without rotating existing values. Backend overrides live under `backend`, public
frontend overrides under `frontend`, and image output size is `image_size`.

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
| `backend.session_run_start_grace_seconds` | `10` | Seconds after a send without any lifecycle event for the request before the session reports `failed` with `run_not_started`. Must be greater than 0. |

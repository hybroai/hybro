<div align="center">
  <a href="https://hybro.ai">
    <picture align="center">
      <source media="(prefers-color-scheme: dark)" srcset="assets/logo-dark.svg">
      <source media="(prefers-color-scheme: light)" srcset="assets/logo-light.svg">
      <img src="assets/logo-dark.svg" alt="Hybro AI" width="500">
    </picture>
  </a>

  <p>
    The open-source agent interoperability platform.<br />
  </p>

  <p>
    <a href="https://opensource.org/licenses/Apache-2.0"><img src="https://img.shields.io/badge/License-Apache%202.0-orange.svg" alt="License"></a>
    <a href="https://x.com/HybroAI"><img src="https://img.shields.io/badge/Follow%20on%20X-000000?logo=x&logoColor=white&style=for-the-badge" alt="Follow on X"></a>
    <a href="https://www.linkedin.com/company/hybroai"><img src="https://img.shields.io/badge/Follow%20on%20LinkedIn-0A66C2?logo=linkedin&logoColor=white&style=for-the-badge" alt="Follow on LinkedIn"></a>
    <a href="https://discord.gg/2S5pCKzUmJ"><img src="https://img.shields.io/badge/Join%20our%20Discord-5865F2?logo=discord&logoColor=white&style=for-the-badge" alt="Join our Discord"></a>
  </p>
</div>

<p align="center">
 ⭐ <em>Star this repo to support the growing Hybro open-source community!</em>
</p>

Hybro AI is an open-source, hybrid multi-agent platform built for seamless agent interoperability. It serves as the core orchestration engine powering the Hybro Agent Network—enabling local and remote AI agents to communicate, collaborate, and execute complex workflows.

## Overview
Hybro AI allows developers and teams to deploy, coordinate, and inspect clusters of autonomous AI agents. Powered by an async FastAPI backend and an interactive Next.js dashboard, Hybro provides real-time agent visualization, execution room management, and protocol-agnostic message routing via the Agent2Agent (A2A) standard.

### Key Features
- **Hybrid Agent Execution**: Seamlessly connect and orchestrate local on-device agents and remote cloud-hosted services.
- **Native Agent Interoperability**: Built around the open Agent2Agent (A2A) protocol for standardized inter-agent communication.
- **Multi-Agent Execution Rooms**: Group specialized agents in dedicated execution rooms to solve multi-step tasks collaboratively.
- **Real-Time Streaming & Inspection**: Live SSE message streaming, multi-agent turn timelines, and an interactive A2A Agent Inspector for testing agent capabilities.
- **CLI-Managed Configuration**: Configure models and authentication once, then start backend, frontend and Agents without maintaining environment files.


## Getting Started

### Prerequisites
- Docker with Compose v2.24+ (`docker compose`; the v1 `docker-compose` binary is not supported)
- Node.js 18+ to install the CLI from npm (not needed for release-asset installs)
- [uv](https://docs.astral.sh/uv/) for host-side `hybro setup` from a checkout
- Node.js 20.19+ (if running the frontend outside of Docker)
- Python 3.12+ and MongoDB 4.2+ (if running the backend outside of Docker; Docker Compose uses MongoDB 7.0)

### Quick Start (Docker)

Install the CLI, configure it, and start the released stack:

```bash
npm install -g @hybroai/cli
hybro setup          # provider, authentication, models -> ~/.hybro/{config,auth}.json
hybro start          # pulls the images published for this CLI's version
```

- **Hybro App**: http://localhost:3000
- **API Server**: http://localhost:8000
- **MCP Server**: http://127.0.0.1:8001/mcp (Streamable HTTP, same-host clients)

Running `hybro` with no arguments opens the same settings/services interface in
a terminal. `hybro --version` prints the CLI and stack version.

Alternatively, run the CLI without installing it by unpacking a release asset
(`hybro-<version>-<platform>.tar.gz` from the GitHub Release) and executing
`hybro/hybro`.

### Quick Start (from a checkout)

The development channel builds images from source instead of pulling them:

```bash
git clone https://github.com/hybroai/hybro.git
cd hybro
./scripts/hybro setup
./scripts/hybro start --build
```

Use this channel to change Hybro itself or to use settings the published images
cannot carry (see [Development and released stacks](#development-and-released-stacks)).

Existing installations should perform the one-time JSON migration below before
removing old environment files.

## Configuration

### Configuration contract

Backend, frontend and bundled-agent user settings belong to one local JSON
configuration, not repository environment files:

```text
~/.hybro/
├── config.json   # User settings; edit through the CLI or directly
└── auth.json     # Credentials; managed through the CLI
```

`HYBRO_HOME` is the sole directory override and must be an absolute path.
Defaults, types and validation remain in a thin code loader; JSON only needs
user overrides. `backend/common/config/loader.py` replaces the old `settings.py`
environment-loading path. Startup validates syntax, known fields, types, ranges and
required combinations. Missing model setup must direct the user to `hybro setup`;
invalid configuration must not silently select a legacy Provider.

CLI edits use the same validation before saving. Manual JSON edits are validated
at the next startup. Settings are a startup snapshot: restart services after
changes with `hybro start --recreate`. Only `api_prefix` needs a rebuild, because
the frontend image compiles its server-side API rewrite from it; the browser
reads every other public setting from the running container.

`.env`, `.env.example` and `frontend/.env.local` are not required by this contract.
The CLI supplies only scoped values to containers and frontend builds; transport
environment variables do not become another user configuration source. Secrets
must never enter the browser bundle or be forwarded wholesale to Agents.

### Edit configuration

```bash
./scripts/hybro config show
./scripts/hybro config check
./scripts/hybro config set backend.log_level '"DEBUG"'
./scripts/hybro config set frontend.max_message_length 12000
./scripts/hybro start --recreate
```

Use `config secret <name>` for hidden service-credential input and `setup` for
Provider/model changes. Startup generates missing internal tokens in `auth.json`
without rotating existing values. Backend overrides live under `backend`, public
frontend overrides under `frontend`, and image output size is `image_size`.

### Existing installations

If you previously ran `hybro setup` and have a `~/.hybro/config.yaml`, convert
it once (optionally importing known deployment values from the old root `.env`):

```bash
./scripts/hybro config migrate --from-env .env
./scripts/hybro config check
./scripts/hybro start --recreate
```

Omit `--from-env` if no deployment values need importing. Migration preserves
Provider credentials and imports known deployment settings and service secrets;
it refuses to overwrite an existing `config.json`. Original environment files,
`.env.example` and `config.yaml` are not deleted.

Installations that only used the old root `.env` (no YAML setup) must instead
re-run the interactive setup, then start:

```bash
./scripts/hybro setup
./scripts/hybro start --recreate
```

After either path, normal setup/start uses JSON only, even if the old files
remain. `migrate` warns when it skips legacy route/model fields it cannot map.

The already-implemented Agent proxy shares backend's setup-selected gateway.
Agents retain SDKs, tools, HITL and A2A execution. Image generation requires an
eligible image model; ChatGPT OAuth alone does not enable it.



## Running

`./scripts/hybro` is the day-2 lifecycle CLI. Common commands:

```bash
./scripts/hybro start                    # up -d, no rebuild (fast daily loop)
./scripts/hybro start --build            # rebuild images (after code/deps change)
./scripts/hybro start --recreate         # recreate containers (runtime configuration changes)
./scripts/hybro start --build --recreate # rebuild and recreate (after code/deps change)
./scripts/hybro logs backend             # stream one service (or all if no arg)
./scripts/hybro status                   # docker compose ps --all
./scripts/hybro stop                     # stop but keep containers
./scripts/hybro down                     # remove containers + default network
```

Only a source checkout can rebuild; a released install changes versions with
`hybro upgrade`. Configuration never needs a rebuild, except `api_prefix`.

Run `./scripts/hybro --help` for the full subcommand reference. Start through the
CLI so Compose receives validated scoped projections, not ambient dotenv values.

### Connect an MCP client

`hybro start` and the TUI's **Start services** include MCP by default. In the
app, open **Networks > Connect MCP** to check status and copy the URL or Claude
Code configuration. Add the server manually to your client; Hybro does not
automatically discover or register MCP clients.

```json
{
  "mcpServers": {
    "hybro": { "type": "http", "url": "http://127.0.0.1:8001/mcp" }
  }
}
```

The **Streamable HTTP** URL is for clients on the machine running Hybro. If you
open the app remotely, loopback in your client still refers to that client's
machine, not the Hybro host. MCP has no authentication or user isolation and
supports only a trusted local mock-auth backend, not Clerk. Do not expose it
publicly.

Use **Services > MCP connection** in the TUI for status, URL, transport, start,
stop, logs, and refresh, or run:

```bash
hybro mcp status
hybro mcp start    # requires setup and a running backend; starts only MCP
hybro mcp logs
hybro mcp stop
```

Exiting the TUI leaves services running. Stopping MCP is not a persistent opt-out:
the next global start includes it again. The app checks status only when the
dialog opens or you refresh; it cannot start/stop services or relay tool calls.
See [MCP tools, health, and native startup](backend/README.md#local-mcp-access).

### Development and released stacks

The CLI runs one of two Compose files, both pinned to the `hybro` project name so
containers keep their identity across install locations:

| | Source checkout | Released install |
|---|---|---|
| Stack file | `docker-compose.yml` | `docker-compose.release.yml` |
| Services come from | local `build:` | published `image:` |
| Version | the working tree | the CLI's own `VERSION` |
| Rebuild | `start --build` | not possible; install a newer CLI |

`docker-compose.release.yml` is generated from `docker-compose.yml` by
`default_agents/render_release.py`, which swaps every `build:` block for an
`image:` reference. It is checked in and verified by CI, so the two stacks cannot
drift. The CLI supplies `HYBRO_STACK_TAG` (its own version) and optionally
`HYBRO_IMAGE_REGISTRY` when it starts a released stack, which means a released
install always runs the images published for the CLI that started it.

Because a released install has no sources, `--build` is refused with an explicit
message. Upgrading is how you change versions:

```bash
hybro upgrade --check       # report; exits non-zero when a newer release exists
hybro upgrade               # install it
hybro start --recreate      # move the containers onto its images
```

The container images are pinned to the CLI version, so upgrading the CLI is what
moves the stack. `hybro upgrade` replaces an npm install in place; an install
from a release archive prints the download, checksum, and unpack commands
instead, because hybro does not overwrite its own directory. A source checkout is
updated with `git pull` and `hybro start --build`.

Everything else, including frontend settings, applies at runtime:

```bash
hybro config set frontend.max_message_length 12000
hybro start --recreate      # no rebuild needed
```

The one exception is `backend.api_prefix`: the published frontend image compiles
its server-side API rewrite from the prefix at build time, so changing it needs
the checkout channel. The browser reads the rest of the projection from a
per-request route (`/hybro-runtime-config`) that
`packaging/frontend/public-config.json` supplies at build time only for that
routing.

### Releasing

`release-please` owns version numbers; everything else is produced from them.

1. Merge the release-please PR. It bumps `VERSION`, `backend/pyproject.toml`,
   `frontend/package.json`, and the `packaging/npm/*/package.json` manifests
   together, and creates the `v<version>` GitHub Release.
2. That same workflow then calls the Release workflow as a reusable workflow,
   which publishes for that one version: the container images, the four platform
   CLI bundles plus tarballs and checksums on the Release, and the
   `@hybroai/cli*` npm packages.
3. The final job resolves `docker-compose.release.yml` and confirms every image
   name it references exists, so a stack that points at an unpublished image
   fails the release rather than a user's `hybro start`. It then resolves the
   published `@hybroai/cli` and confirms every platform package its
   `optionalDependencies` name is on the registry. npm skips a missing optional
   dependency silently, so nothing else would catch it: the launcher installs,
   and the wrapper only fails at run time with an "is not installed" message.

The Release workflow is called rather than triggered by `release: published`
because release-please acts with `GITHUB_TOKEN`, and GitHub does not start
workflows for events that token triggers — a release event would never arrive.
`workflow_dispatch` remains for re-publishing after a failure; give it the
version so a retry cannot silently publish whatever `VERSION` happens to say.
The same rule explains why release-please PRs get no automatic CI: GitHub does
start those runs, but holds them until someone with write access approves them.

Requirements for the workflow: an `NPM_TOKEN` repository secret with publish
rights for the `@hybroai` scope, and the scope must already exist on npm.
Publishing the images also needs the `ghcr.io/hybroai` packages to be public;
GitHub creates them private, and a private image makes `hybro start` fail with
an authorization error.

`backend/uv.lock` records the project's own version and release-please does not
update it, so a release leaves it one version behind. `uv sync --frozen`
tolerates that, but `uv lock --check` does not.

Build the CLI locally with `packaging/cli/build.sh`, then stage it for npm with
`packaging/npm/build.sh <darwin-arm64|darwin-x64|linux-arm64|linux-x64>`. Releases
ship a PyInstaller **onefile** executable: a single binary with no symbolic
links, because npm's packer drops links and a macOS onedir bundle depends on
four of them. The bundle extracts to a new temporary directory per run, which is
why both Compose files pin `name: hybro`.

## Architecture
This repository is the source of truth for the product. Its frontend and backend
are the in-repository `frontend/` and `backend/` directories used by Docker
Compose and CI.

The repository is split into these primary components:
- `backend/`: A FastAPI orchestration engine using MongoDB for persistence and optional Redis services for cross-process coordination.
- `frontend/`: A Next.js 16 (Turbopack) application for chat, local agent discovery, agent management, and inspection.
- `default_agents/`: A collection of ready-to-use A2A agents, each running as its own container, plus a one-shot `registrar` that registers them with the backend on startup.
- `MCP/`: A standalone adapter exposing two backend agent-network tools over Streamable HTTP, built with its own Dockerfile and published as the version-pinned `hybro-mcp` image.

## API keys
Use `hybro setup` for OpenAI API Key or ChatGPT OAuth, DeepSeek API Key, or
Anthropic API Key.
Agents receive a scoped `HYBRO_AGENT_CONFIG` projection containing the backend
proxy URL, a separate inference token and image size. They pass these values
explicitly to their SDKs. The token cannot authenticate as a user or registrar;
it is required even under mock auth. Agents never receive Provider credentials
or the setup directory.
See [gateway architecture](backend/docs/System-Architecture.md#llm_gateway) for
the supported proxy subset and limits.

## Contributing
We welcome contributions from the community! Whether you are fixing a bug, adding a feature, or improving documentation, please feel free to open a pull request.

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/amazing-feature`)
3. Commit your changes (`git commit -m 'feat: add amazing feature'`)
4. Push to the branch (`git push origin feature/amazing-feature`)
5. Open a Pull Request

## License
Apache License 2.0

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
- **Zero-Config Developer Mode**: Start the frontend and backend instantly out of the box with zero required external API keys.


## Getting Started

### Prerequisites
- Docker with Compose v2.24+ (`docker compose`; the v1 `docker-compose` binary is not supported)
- Node.js 20.19+ (if running the frontend outside of Docker)
- Python 3.12+ and MongoDB 4.2+ (if running the backend outside of Docker; Docker Compose uses MongoDB 7.0)

### Quick Start (Docker)
The easiest way to get started is using the automated installation script, which will clone the repository, set up the environment, and spin up the Docker containers.

```bash
curl -fsSL https://raw.githubusercontent.com/hybroai/hybro/main/install.sh | sh
```

Alternatively, you can manually clone and run:

```bash
git clone https://github.com/hybroai/hybro.git
cd hybro
./scripts/hybro start
```

- **Hybro App**: http://localhost:3000
- **API Server**: http://localhost:8000

With no `.env`, `hybro start` runs in zero-config demo mode (mock auth,
agents error until `OPENAI_API_KEY` is set). See **Configuration** below to
enable working default agents and LLM calls.

## Configuration

The repo-root `.env` is the single source of truth for the backend, default
agents, and the frontend build. To bring it up manually:

```bash
cp .env.example .env
# Edit .env; at minimum set OPENAI_API_KEY
sh backend/scripts/ensure_webhook_signing_key.sh .env
sh backend/scripts/ensure_registrar_token.sh .env
sh backend/scripts/ensure_frontend_env.sh .env frontend/.env.local
```

`./scripts/hybro start` runs the three `ensure_*` steps for you whenever
`.env` exists, so after the initial `cp .env.example .env` (plus setting
`OPENAI_API_KEY`) you can just run `./scripts/hybro start --recreate` to
pick up runtime values. To run the backend's classifier, supervisor, context
memory, and synthesis generation through DeepSeek instead, set
`DEEPSEEK_API_KEY` and optionally `DEEPSEEK_MODEL_NAME`. Backend generation
selects the first configured provider in this order: DeepSeek, OpenAI, Gemini.
This does not change `default_agents/`, which are separate containers receiving
only an allow-listed subset of the root `.env` and still require
`OPENAI_API_KEY`; the optional embedding route also remains OpenAI-backed. If
you also change frontend-facing `NEXT_PUBLIC_*`
keys, use `./scripts/hybro start --build --recreate` - those values are
Docker build args baked into the Next.js bundle, so recreate alone keeps
the old browser config. `frontend/.env.local` is generated from `.env` -
do not hand-edit it.

## Running

`./scripts/hybro` is the day-2 lifecycle CLI. Common commands:

```bash
./scripts/hybro start                    # up -d, no rebuild (fast daily loop)
./scripts/hybro start --build            # rebuild images (after code/deps change)
./scripts/hybro start --recreate         # recreate containers (runtime .env changes)
./scripts/hybro start --build --recreate # rebuild+recreate (NEXT_PUBLIC_* / image changes)
./scripts/hybro start --check-key        # validate the OpenAI key used by default agents
./scripts/hybro logs backend             # stream one service (or all if no arg)
./scripts/hybro status                   # docker compose ps --all
./scripts/hybro stop                     # stop but keep containers
./scripts/hybro down                     # remove containers + default network
```

Run `./scripts/hybro --help` for the full subcommand reference. Power users
can still invoke `docker compose` directly.

## Architecture
This repository is the source of truth for the product. Its frontend and backend
are the in-repository `frontend/` and `backend/` directories used by Docker
Compose and CI.

The repository is split into these primary components:
- `backend/`: A FastAPI orchestration engine using MongoDB for persistence and optional Redis services for cross-process coordination.
- `frontend/`: A Next.js 16 (Turbopack) application for chat, local agent discovery, agent management, and inspection.
- `default_agents/`: A collection of ready-to-use A2A agents, each running as its own container, plus a one-shot `registrar` that registers them with the backend on startup.

## API keys
By default, the backend and default agents share `OPENAI_API_KEY`. If
`DEEPSEEK_API_KEY` is configured, the backend automatically gives DeepSeek
priority over OpenAI and Gemini for generation. This does not reconfigure the
separately deployed default agents: one root `.env` is the source of truth, but
Compose deliberately forwards only `OPENAI_API_KEY`, `OPENAI_MODEL`, and image
settings to those containers. Agents register regardless, but their calls fail
until their provider key is available.

## Contributing
We welcome contributions from the community! Whether you are fixing a bug, adding a feature, or improving documentation, please feel free to open a pull request.

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/amazing-feature`)
3. Commit your changes (`git commit -m 'feat: add amazing feature'`)
4. Push to the branch (`git push origin feature/amazing-feature`)
5. Open a Pull Request

## License
Apache License 2.0

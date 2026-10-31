# The single Hybro image: UI, API, MCP, sample agents, MongoDB, and the Caddy
# front proxy in one container, supervised by supervisord. Build from the
# repository root:
#
#   docker build -t hybro .
#
# Published as ghcr.io/hybroai/hybro for linux/amd64 and linux/arm64.

# uv is pinned to a version and digest, together with the lockfiles.
FROM ghcr.io/astral-sh/uv:0.7.13@sha256:6c1e19020ec221986a210027040044a5df8de762eb36d5240e382bc41d7a9043 AS uv

# ---------------------------------------------------------------- frontend
FROM node:24-bookworm-slim AS frontend
WORKDIR /src
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
# No build-time projection: browser settings are served per request and Caddy
# routes the API, so one image serves every install.
ENV NEXT_TELEMETRY_DISABLED=1
RUN npm run build \
    && cp -r .next/static .next/standalone/.next/static \
    && cp -r public .next/standalone/public

# ------------------------------------------------------------------ python
FROM ubuntu:22.04 AS python
COPY --from=uv /uv /bin/
ENV UV_PYTHON_INSTALL_DIR=/opt/python \
    UV_PYTHON_PREFERENCE=only-managed \
    UV_LINK_MODE=copy
RUN uv python install 3.12

# Backend and MCP: one environment from backend/uv.lock with the `mcp` extra.
WORKDIR /app/backend
COPY backend/pyproject.toml backend/uv.lock ./
RUN uv sync --frozen --no-dev --extra mcp --no-install-project --python 3.12

# Sample agents: the workspace lock; dependencies only, never the agents
# themselves (two declare a hatchling build without a file list).
WORKDIR /app/default_agents
COPY default_agents/pyproject.toml default_agents/uv.lock ./
COPY default_agents/echo_agent/pyproject.toml echo_agent/
COPY default_agents/image_generator_agent/pyproject.toml image_generator_agent/
COPY default_agents/story_agent/pyproject.toml story_agent/
COPY default_agents/travel_planner_agent/pyproject.toml travel_planner_agent/
COPY default_agents/weather_agent/pyproject.toml weather_agent/
RUN uv sync --frozen --no-dev --all-packages --no-install-workspace --python 3.12

# ----------------------------------------------------------------- runtime
FROM mongo:7.0 AS mongo
FROM caddy:2 AS caddy
FROM node:24-bookworm-slim AS node

FROM ubuntu:22.04
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates libcurl4 supervisor \
    && rm -rf /var/lib/apt/lists/*

# mongod from the official image, which is built on Ubuntu 22.04.
COPY --from=mongo /usr/bin/mongod /usr/bin/mongod
COPY --from=caddy /usr/bin/caddy /usr/bin/caddy
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=python /opt/python /opt/python
COPY --from=python /app/backend/.venv /app/backend/.venv
COPY --from=python /app/default_agents/.venv /app/default_agents/.venv

COPY backend/ /app/backend/
COPY MCP/server.py /app/MCP/server.py
COPY default_agents/runtime_config.py /app/default_agents/runtime_config.py
COPY default_agents/echo_agent/ /app/default_agents/echo_agent/
COPY default_agents/image_generator_agent/ /app/default_agents/image_generator_agent/
COPY default_agents/story_agent/ /app/default_agents/story_agent/
COPY default_agents/travel_planner_agent/ /app/default_agents/travel_planner_agent/
COPY default_agents/weather_agent/ /app/default_agents/weather_agent/
# The bundled agent manifest the backend registers at boot.
COPY default_agents/agents.yaml /app/bundled_agents.yaml
COPY --from=frontend /src/.next/standalone/ /app/frontend/

# Copied binaries are fragile across base image updates; fail the build here.
RUN mongod --version && node --version && caddy version \
    && install -d -m 0700 /var/lib/hybro/runtime && mkdir -p /data \
    # The services run as the config directory's owner, unknown at build time;
    # Next.js writes its image optimization cache here.
    && install -d -m 1777 /app/frontend/.next/cache

ENV HYBRO_CONTAINER=image \
    HYBRO_HOME=/var/lib/hybro/runtime \
    PYTHONUNBUFFERED=1 \
    LANG=C.UTF-8

# Caddy serves the UI, API, and MCP on 8080; 1455 is the ChatGPT OAuth
# callback listener, open only while a login is pending.
EXPOSE 8080 1455
STOPSIGNAL SIGTERM
ENTRYPOINT ["/app/backend/.venv/bin/python", "/app/backend/runtime_entrypoint.py"]

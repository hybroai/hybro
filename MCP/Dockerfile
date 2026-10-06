FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.7.13@sha256:6c1e19020ec221986a210027040044a5df8de762eb36d5240e382bc41d7a9043 /uv /bin/
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY server.py ./

USER 65532:65532
EXPOSE 8001
CMD ["/app/.venv/bin/python", "server.py", "--container"]

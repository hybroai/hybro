# ruff: noqa: I001

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

# Logging must be configured before importing application runtime modules.
from common.observability.bootstrap import settings as _logging_settings  # noqa: F401
from common.config.loader import settings

import api_gateway
from api_gateway.errors import (
    provider_not_configured_handler,
    setup_request_error_handler,
)
from common.auth import bind_auth_config
from common.protocols import HealthCheck
from common.middleware.request_concurrency import RequestConcurrencyLimitMiddleware
from common.middleware.request_logging import RequestLoggingMiddleware
from common.middleware.request_size import RequestBodyLimitMiddleware
from llm_gateway.errors import ProviderNotConfigured
from llm_gateway.setup_api import SetupRequestError
from container import (
    create_application_runtime,
    create_health_check_service,
    shutdown_runtime,
    startup_runtime,
    validate_runtime_bindings,
)

bind_auth_config(
    clerk_secret_key_value=settings.clerk_secret_key,
    authorized_parties=tuple(settings.frontend_origins),
)

AGENT_MESSAGE_MAX_BYTES = 6 * 1024 * 1024
AGENT_MESSAGE_MAX_CONCURRENT = 4
# A session send holds its request for up to the configured wait (30 s by
# default), so four concurrent sends also cap session throughput.
SESSION_MESSAGE_MAX_BYTES = 64 * 1024
SESSION_MESSAGE_MAX_CONCURRENT = 4
# Remote Agents post A2A push notifications without a user session; admission
# bounds how many bodies one worker reads at once.
A2A_PUSH_MAX_CONCURRENT = 8


class _RequestLoggingFastAPI(FastAPI):
    """Keep request correlation outside Starlette's server-error middleware."""

    def build_middleware_stack(self):
        return RequestLoggingMiddleware(super().build_middleware_stack())


@asynccontextmanager
async def lifespan(app: FastAPI):
    runtime = create_application_runtime(settings)
    try:
        await startup_runtime(app, runtime)
        validate_runtime_bindings(app, runtime)
        yield
    finally:
        await shutdown_runtime(app, runtime)


def compute_health_status(
    *,
    delivery_pubsub_connected: bool,
    eventing_connected: bool = True,
    delivery_kv_connected: bool,
    redis_runtime_connected: bool,
    redis_url: str,
    agent_search_index_ready: bool = False,
) -> dict:
    redis_expected = bool(redis_url)
    redis_degraded = redis_expected and not (
        delivery_pubsub_connected
        and eventing_connected
        and delivery_kv_connected
        and redis_runtime_connected
    )
    degraded = redis_degraded or not agent_search_index_ready
    return {
        "body": {
            "status": "degraded" if degraded else "ok",
            "delivery_pubsub_connected": delivery_pubsub_connected,
            "eventing_connected": eventing_connected,
            "delivery_kv_connected": delivery_kv_connected,
            "redis_runtime_connected": redis_runtime_connected,
            "redis_expected": redis_expected,
            "broker_connected": delivery_pubsub_connected,
            "broker_expected": redis_expected,
            "redis_service_connected": redis_runtime_connected,
            "agent_search_index_ready": agent_search_index_ready,
        },
        "status_code": 503 if degraded else 200,
    }


health_check_service = create_health_check_service(
    redis_url=settings.redis_url,
    compute_health_status=compute_health_status,
)


def get_health_check() -> HealthCheck:
    return health_check_service


def create_app(
    platform_facade_factory=None,
    agent_rate_limiter_factory=None,
    extra_routes=None,
) -> FastAPI:
    app = _RequestLoggingFastAPI(
        lifespan=lifespan,
        title="Multi-Agent AI System",
    )

    app.state.platform_facade_factory = platform_facade_factory
    app.state.agent_rate_limiter_factory = agent_rate_limiter_factory
    app.add_exception_handler(ProviderNotConfigured, provider_not_configured_handler)
    app.add_exception_handler(SetupRequestError, setup_request_error_handler)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.frontend_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
        allow_headers=[
            "Authorization",
            "Content-Type",
            "X-API-Key",
            "Cache-Control",
            "sentry-trace",
            "baggage",
            "X-Request-ID",
        ],
        expose_headers=["X-Request-ID"],
    )
    app.add_middleware(
        RequestBodyLimitMiddleware,
        path=f"{settings.api_prefix}/files/upload",
        max_bytes=6 * 1024 * 1024,
    )

    for path, limit in (
        ("chat/completions", 1024 * 1024),
        ("images/generations", 128 * 1024),
        ("images/edits", 10 * 1024 * 1024),
    ):
        app.add_middleware(
            RequestBodyLimitMiddleware,
            path=f"{settings.api_prefix}/internal/llm/{path}",
            max_bytes=limit,
        )

    app.add_middleware(
        RequestBodyLimitMiddleware,
        path=f"{settings.api_prefix}/agents/messages",
        method="POST",
        max_bytes=AGENT_MESSAGE_MAX_BYTES,
    )
    app.add_middleware(
        RequestBodyLimitMiddleware,
        path=f"{settings.api_prefix}/sessions/messages",
        method="POST",
        max_bytes=SESSION_MESSAGE_MAX_BYTES,
    )
    # Added last so admission also bounds requests still receiving their body.
    app.add_middleware(
        RequestConcurrencyLimitMiddleware,
        path=f"{settings.api_prefix}/agents/messages",
        max_concurrent=AGENT_MESSAGE_MAX_CONCURRENT,
    )
    app.add_middleware(
        RequestConcurrencyLimitMiddleware,
        path=f"{settings.api_prefix}/sessions/messages",
        max_concurrent=SESSION_MESSAGE_MAX_CONCURRENT,
    )
    app.add_middleware(
        RequestConcurrencyLimitMiddleware,
        path=f"{settings.api_prefix}/a2a/push",
        max_concurrent=A2A_PUSH_MAX_CONCURRENT,
        include_subpaths=True,
    )

    @app.get("/health")
    async def health_check(
        request: Request,
        health: HealthCheck = Depends(get_health_check),
    ):
        return await health.check(request)

    app.include_router(api_gateway.router, prefix=settings.api_prefix)

    if extra_routes:
        for router in extra_routes:
            app.include_router(router, prefix=settings.api_prefix)

    return app


app = create_app()

if settings.auth_mode == "mock":
    from common.auth import (
        ClerkUser,
        get_current_user,
        get_current_user_with_query_token,
        get_optional_user,
    )

    def mock_get_current_user():
        return ClerkUser(
            user_id="user_local_developer",
            session_id="mock_session",
            claims={"email": "local@developer.com", "username": "local_dev"},
        )

    app.dependency_overrides[get_current_user] = mock_get_current_user
    app.dependency_overrides[get_current_user_with_query_token] = mock_get_current_user
    app.dependency_overrides[get_optional_user] = mock_get_current_user


def main() -> None:
    import uvicorn

    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        access_log=False,
        log_config=None,
    )


if __name__ == "__main__":
    main()

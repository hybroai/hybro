# ruff: noqa: I001

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

# Logging must be configured before importing application runtime modules.
from common.observability.bootstrap import settings as _logging_settings  # noqa: F401
from common.config.settings import settings

import api_gateway
from common.auth import bind_auth_config
from common.middleware.request_logging import RequestLoggingMiddleware
from common.middleware.request_size import RequestBodyLimitMiddleware
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
    service_registrar_token_value=settings.default_agent_registrar_token,
    service_provider_id_value=settings.default_agent_provider_id,
)


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
    change_stream_connected: bool,
    agent_search_index_ready: bool = False,
    memory_search_index_ready: bool = False,
    search_indexes_ready: bool = False,
) -> dict:
    redis_expected = bool(redis_url)
    redis_degraded = redis_expected and not (
        delivery_pubsub_connected
        and eventing_connected
        and delivery_kv_connected
        and redis_runtime_connected
    )
    degraded = redis_degraded or not change_stream_connected or not search_indexes_ready
    return {
        "body": {
            "status": "degraded" if degraded else "ok",
            "change_stream_connected": change_stream_connected,
            "delivery_pubsub_connected": delivery_pubsub_connected,
            "eventing_connected": eventing_connected,
            "delivery_kv_connected": delivery_kv_connected,
            "redis_runtime_connected": redis_runtime_connected,
            "redis_expected": redis_expected,
            "broker_connected": delivery_pubsub_connected,
            "broker_expected": redis_expected,
            "redis_service_connected": redis_runtime_connected,
            "legacy_redis_service_connected": redis_runtime_connected,
            "agent_search_index_ready": agent_search_index_ready,
            "memory_search_index_ready": memory_search_index_ready,
            "search_indexes_ready": search_indexes_ready,
        },
        "status_code": 503 if degraded else 200,
    }


health_check_service = create_health_check_service(
    redis_url=settings.redis_url,
    compute_health_status=compute_health_status,
)


def get_health_check():
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

    @app.get("/health")
    async def health_check(
        request: Request,
        health=Depends(get_health_check),
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
        get_current_user_or_service,
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
    app.dependency_overrides[get_current_user_or_service] = mock_get_current_user
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

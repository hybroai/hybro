"""Typed defaults and JSON startup snapshot. No dotenv or import-time file I/O."""

import ipaddress
import logging
import math
import os
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Literal
from urllib.parse import urlsplit

from pydantic import (
    AliasChoices,
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationInfo,
    field_validator,
)

logger = logging.getLogger(__name__)


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)

    app_env: str = "development"  # development, staging, production

    frontend_origins: str | list[str] = [
        "http://localhost:3000",
        "https://hybro.ai",
    ]
    api_prefix: str = "/api/v1"

    mongodb_url: str = "localhost:27017"
    mongodb_db_name: str = "hybro"

    openai_api_key: str = ""
    openai_base_url: str = Field(
        default="",
        validation_alias=AliasChoices(
            "OPENAI_BASE_URL", "OPENAI_API_BASE", "openai_base_url"
        ),
    )
    lead_ai_model: str = "gpt-5-mini"
    classifier_ai_model: str = "gpt-4o-mini"
    embedding_model: str = "text-embedding-3-small"
    supervisor_model: str | None = None

    deepseek_api_key: str = ""
    deepseek_model_name: str = "deepseek-v4-flash"

    # Rejection-only migration sentinels. Gemini is not a supported provider.
    google_api_key: str = ""
    gemini_api_key: str = ""

    # LLM gateway routing and runtime policy. Timeouts must accommodate the
    # slowest configured model: a reasoning model at a high thinking level can
    # spend minutes on one turn, and a timeout shorter than that fails the turn
    # and burns the retry budget instead of producing an answer.
    llm_gateway_generation_provider: str = "openai"
    llm_gateway_max_attempts: int = 2
    llm_gateway_retry_backoff_seconds: float = 0.2
    llm_gateway_request_timeout_seconds: float = 300.0
    llm_gateway_stream_timeout_seconds: float = 300.0
    # Per-route timeout used by the orchestrator kernel. Orchestrator model
    # calls pass this explicitly, so it overrides the gateway defaults above.
    llm_gateway_route_timeout_seconds: float = 300.0
    llm_gateway_supervisor_json_timeout_seconds: float = 30.0
    llm_gateway_supervisor_text_timeout_seconds: float = 90.0
    llm_gateway_supervisor_stream_timeout_seconds: float = 90.0
    llm_gateway_default_generation_model: str = "lead_ai_model"
    llm_gateway_default_embedding_model: str = "embedding_model"
    llm_gateway_default_supervisor_model: str = "supervisor_model"

    # Orchestrator runtime profile parameters (Fast/Ultimate). Fast and Ultimate
    # share one Kernel and differ only in these resolved parameters. The
    # initial_routing dimension is pinned to explicit_agent_first (the API
    # pre-filters the candidate scope) and finalization to pass_through until
    # those reserved dimensions are consumed by the kernel.
    orchestrator_fast_model_route: str = "supervisor_model"
    orchestrator_fast_prompt_id: str = "orchestrator_fast"
    orchestrator_fast_prompt_version: str = "5"
    orchestrator_fast_thinking_level: str | None = None
    orchestrator_fast_max_model_turns: int = 12
    orchestrator_fast_grace_model_turns: int = 1
    orchestrator_fast_max_agent_calls: int = 10
    orchestrator_fast_max_parallel_calls: int = 3
    orchestrator_fast_max_transport_retries_per_call: int = 2
    orchestrator_fast_max_compactions: int = 2
    orchestrator_fast_deadline_seconds: float = 300.0
    orchestrator_fast_initial_routing: str = "explicit_agent_first"
    orchestrator_fast_tool_execution: str = "parallel"
    orchestrator_fast_finalization: str = "pass_through"

    orchestrator_ultimate_model_route: str = "supervisor_model"
    orchestrator_ultimate_prompt_id: str = "orchestrator_ultimate"
    orchestrator_ultimate_prompt_version: str = "5"
    orchestrator_ultimate_thinking_level: str | None = None
    orchestrator_ultimate_max_model_turns: int = 24
    orchestrator_ultimate_grace_model_turns: int = 2
    orchestrator_ultimate_max_agent_calls: int = 20
    orchestrator_ultimate_max_parallel_calls: int = 4
    orchestrator_ultimate_max_transport_retries_per_call: int = 3
    orchestrator_ultimate_max_compactions: int = 3
    orchestrator_ultimate_deadline_seconds: float = 600.0
    orchestrator_ultimate_initial_routing: str = "explicit_agent_first"
    orchestrator_ultimate_tool_execution: str = "parallel"
    orchestrator_ultimate_finalization: str = "pass_through"

    # Passthrough: zero model turns and one call to the single mentioned Agent.
    # The deadline counts active time only; a suspended Run's clock is stopped.
    orchestrator_passthrough_deadline_seconds: float = Field(default=300.0, gt=0)
    orchestrator_passthrough_max_transport_retries_per_call: int = Field(
        default=2, gt=0
    )

    # Mandatory canonical lifecycle worker cadence.
    orchestrator_worker_interval_seconds: int = Field(default=30, gt=0)

    # Waits. A suspended Run's deadline clock is stopped; an acknowledged remote
    # Agent call is polled until this ceiling, measured from its acceptance.
    orchestrator_call_wait_ceiling_hours: float = Field(default=168.0, gt=0)
    # Public base URL at which remote Agents reach this backend, for example
    # "https://hybro.example.com". When set, canonical dispatch registers an A2A
    # push-notification config with Agents whose card advertises
    # pushNotifications; polling remains the fallback. Empty disables push.
    a2a_push_callback_base_url: str = ""
    # Supervisor ask_user approval deadline; None means no deadline.
    approval_deadline_hours: float | None = Field(default=None, gt=0)

    # Sessions. A send waits this long for its run to settle or pause for the
    # owner before answering "working"; a run with no lifecycle event within
    # the grace is reported as not started.
    session_run_start_grace_seconds: float = Field(default=10.0, gt=0)

    # Orchestrator canary observability (step 8). The canary job is disabled by
    # default and reads only the existing orchestrator durable stores. Thresholds
    # follow the suggested §8.2 initial values and are tuned before launch.
    orchestrator_canary_enabled: bool = False
    orchestrator_canary_run_failure_rate_max: float = Field(default=0.01, ge=0)
    orchestrator_canary_run_failure_window_seconds: int = Field(default=300, gt=0)
    orchestrator_canary_blocked_intent_max_age_seconds: int = Field(default=600, gt=0)
    orchestrator_canary_recovery_cycle_max_age_seconds: int = Field(default=60, gt=0)
    orchestrator_canary_observation_conflicts_max: int = Field(default=0, ge=0)

    log_level: str = "INFO"
    log_format: str = "auto"

    # Agent Health
    agent_health_check_interval: int = 3600

    # Local Agent Discovery (Docker backend -> host gateway)
    local_agent_discovery_enabled: bool = False
    local_agent_discovery_host: str = "host.docker.internal"
    local_agent_discovery_port_start: int = Field(default=1024, ge=1, le=65535)
    local_agent_discovery_port_end: int = Field(default=65535, ge=1, le=65535)
    local_agent_discovery_interval_seconds: int = Field(default=120, gt=0)
    local_agent_discovery_connect_timeout_seconds: float = Field(default=0.05, gt=0)
    local_agent_discovery_probe_timeout_seconds: float = Field(default=3.0, gt=0)
    # Host ports this deployment publishes for its own agents; discovery skips
    # them so it does not register those agents a second time over the host URL.
    local_agent_discovery_excluded_ports: frozenset[int] = frozenset()

    # Compaction
    compaction_concurrency: int = 5

    # Clerk Authentication
    clerk_secret_key: str = ""  # Clerk Secret Key for backend API
    auth_mode: Literal["mock", "clerk"] = "mock"  # "mock" or "clerk"
    # Clerk user IDs allowed to change the model provider in the browser. Empty
    # disables browser setup in clerk mode; mock mode is open (loopback trust).
    admin_user_ids: list[
        Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    ] = Field(default_factory=list)

    # Inference-only credential for bundled agents; never grants user access.
    default_agent_llm_token: str = Field(default="", repr=False)
    # provider_id of the bundled agents the backend registers at boot.
    default_agent_provider_id: str = "Hybro AI"
    # Bundled agent manifest (`default_agents/agents.yaml`). Empty disables
    # boot registration of the bundled agents.
    bundled_agents_manifest: str = ""
    # Interface of the ChatGPT OAuth callback listener (port 1455). The image
    # binds all interfaces, because Docker forwards the published port to the
    # container's network interface, not its loopback.
    oauth_callback_host: str = "127.0.0.1"

    # Agent Health Check Settings
    agent_health_check_enabled: bool = True  # enable/disable agent health check
    cloud_health_check_timeout: float = 5.0  # seconds for on-demand cloud agent probe

    # Agent Capability Issue Tracking
    capability_issue_threshold: int = 2  # Exclude agents with >= this many open issues

    # Delivery / SSE settings
    heartbeat_interval_seconds: float = 30.0
    sse_connection_queue_maxsize: int = 100
    terminal_dedup_ttl_seconds: int = 300
    terminal_reservation_ttl_seconds: int = 30
    terminal_redis_io_timeout_seconds: float = 1.0
    terminal_dedup_cache_maxsize: int = 10_000
    delivery_started_ttl_seconds: int = 3600
    delivery_started_cache_maxsize: int = 10_000
    redis_dead_letter_channel: str = "delivery:dead_letter"
    dead_letter_memory_maxlen: int = 1000

    # Internal eventing (separate Redis client and lifecycle). The legacy
    # REDIS_INTERNAL_CHANNEL name remains accepted; the new name wins if both exist.
    eventing_redis_channel: str = Field(
        default="internal:global",
        validation_alias=AliasChoices(
            "eventing_redis_channel",
            "redis_internal_channel",
        ),
    )
    eventing_redis_dead_letter_channel: str = "eventing:dead_letter"
    eventing_redis_io_timeout_seconds: float = 5.0
    eventing_handler_queue_maxsize: int = 1000
    eventing_auxiliary_task_maxsize: int = Field(default=128, gt=0)
    eventing_enqueue_timeout_seconds: float = 1.0
    eventing_shutdown_timeout_seconds: float = 5.0
    eventing_dead_letter_memory_maxlen: int = 1000
    redis_subscription_reserved_connections: int = 10
    redis_room_subscription_production_limit: int = 40
    redis_room_subscription_ready_timeout_seconds: float = 5.0
    terminal_processing_statuses: frozenset[str] = frozenset(
        {
            "completed",
            "failed",
            "canceled",
            "rejected",
            "expired",
            "rate_limited",
            "error",
        }
    )

    # Redis transport for Delivery SSE
    redis_url: str = (
        ""  # e.g. "redis://localhost:6379/0" - empty string disables broker
    )
    redis_sse_channel_prefix: str = (
        "sse:room:"  # per-room channel: sse:room:{conversation_id}
    )
    redis_reconnect_delay: float = 1.0  # initial reconnect delay (seconds)
    redis_reconnect_max_delay: float = 30.0  # max reconnect delay ceiling (seconds)
    redis_terminal_key_prefix: str = "terminal:"

    # ===========================================
    # Context & Memory System Settings
    # See docs/System-Architecture.md for the current architecture
    # ===========================================

    # Compaction Settings (LOSSLESS - pointer-based, not summarization)
    compaction_enabled: bool = True  # Enable/disable auto-compaction
    compaction_max_full_turns: int = 20  # Max turns to keep in FULL representation
    compaction_max_total_tokens: int = (
        80000  # Trigger compaction when full turns exceed this
    )
    compaction_preserve_recent: int = 10  # Always keep this many recent turns FULL
    compaction_content_ttl_days: int = 0  # TTL for stored content (0 = forever)

    # Local room file storage
    hybro_file_dir: str = Field(
        default="",
        validation_alias=AliasChoices("HYBRO_FILE_DIR", "hybro_file_dir"),
    )

    # Graceful Shutdown Settings
    shutdown_drain_seconds: float = (
        5.0  # Drain period for SSE connections during shutdown
    )

    # Connection pool tuning (per-worker; total = workers * value)
    mongodb_max_pool_size: int = 50
    mongodb_min_pool_size: int = 10
    redis_max_connections: int = 50

    @field_validator("frontend_origins", mode="before")
    @classmethod
    def parse_frontend_origins(cls, v):
        if isinstance(v, str):
            # Split comma-separated string into list
            return [url.strip() for url in v.split(",") if url.strip()]
        return v

    @field_validator(
        "orchestrator_fast_thinking_level",
        "orchestrator_ultimate_thinking_level",
        mode="before",
    )
    @classmethod
    def normalize_optional_thinking_level(cls, value):
        if value is None or str(value).strip() == "":
            return None
        return str(value).strip()

    @field_validator("a2a_push_callback_base_url")
    @classmethod
    def validate_a2a_push_callback_base_url(cls, value: str) -> str:
        value = value.strip()
        if not value:
            return ""
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or "\\" in value
            or any(char.isspace() or ord(char) < 32 for char in value)
        ):
            raise ValueError(
                "a2a_push_callback_base_url must be an HTTP(S) URL with a hostname "
                "and no credentials, query, or fragment"
            )
        if parsed.scheme == "http" and not _is_private_push_host(parsed.hostname):
            raise ValueError(
                "a2a_push_callback_base_url must use https unless the host is "
                "loopback, host.docker.internal, or a private RFC 1918 address"
            )
        return value.rstrip("/")

    @field_validator("log_level", mode="before")
    @classmethod
    def validate_log_level(cls, value):
        normalized = str(value or "INFO").strip().upper()
        if normalized not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError(
                "LOG_LEVEL must be DEBUG, INFO, WARNING, ERROR, or CRITICAL"
            )
        return normalized

    @field_validator("log_format", mode="before")
    @classmethod
    def validate_log_format(cls, value):
        normalized = str(value or "auto").strip().lower()
        if normalized not in {"auto", "json", "logfmt"}:
            raise ValueError("LOG_FORMAT must be auto, json, or logfmt")
        return normalized

    @field_validator("terminal_processing_statuses", mode="before")
    @classmethod
    def parse_terminal_processing_statuses(cls, v):
        if isinstance(v, str):
            return frozenset(
                status.strip().lower() for status in v.split(",") if status.strip()
            )
        if v is None:
            return frozenset()
        return frozenset(str(status).strip().lower() for status in v)

    @field_validator("local_agent_discovery_excluded_ports", mode="before")
    @classmethod
    def parse_local_agent_discovery_excluded_ports(cls, value):
        if value is None:
            return frozenset()
        if isinstance(value, str):
            value = [part.strip() for part in value.split(",") if part.strip()]
        try:
            ports = frozenset(int(port) for port in value)
        except (TypeError, ValueError) as exc:
            raise ValueError("Excluded discovery ports must be whole numbers") from exc
        if any(not 1 <= port <= 65535 for port in ports):
            raise ValueError("Excluded discovery ports must be within 1..65535")
        return ports

    @field_validator("compaction_concurrency", mode="before")
    @classmethod
    def normalize_compaction_concurrency(cls, value):
        try:
            return max(1, int(value))
        except (TypeError, ValueError):
            return 5

    @field_validator(
        "orchestrator_canary_enabled",
        mode="before",
    )
    @classmethod
    def normalize_orchestrator_worker_switch(cls, value):
        if value is None or str(value).strip() == "":
            return False
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"1", "true", "yes", "on"}

    @property
    def is_gunicorn(self) -> bool:
        """Detect gunicorn from server-injected runtime metadata."""
        return os.environ.get("SERVER_SOFTWARE", "").startswith("gunicorn")


_PRIVATE_PUSH_NETWORKS = tuple(
    ipaddress.ip_network(network)
    for network in ("127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)


def _is_private_push_host(host: str) -> bool:
    """Hosts where a push token may travel over plain HTTP."""
    if host.lower() in {"localhost", "host.docker.internal"}:
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    if address.version == 6:
        return address.is_loopback
    return any(address in network for network in _PRIVATE_PUSH_NETWORKS)


# Credentials and legacy route hints are never user-editable backend JSON fields.
PRIVATE_FIELDS = frozenset(
    {
        "openai_api_key",
        "deepseek_api_key",
        "google_api_key",
        "gemini_api_key",
        "clerk_secret_key",
        "default_agent_llm_token",
    }
)
# Retired backend settings. Existing config.json files may still carry them
# (0.3's `config migrate --from-env` copied any matching env key), so they are
# accepted, dropped, and named in one startup warning instead of failing
# validation. Remove this set in the next major release.
REMOVED_FIELDS = frozenset(
    {
        "mongodb_host",
        "mongodb_port",
        # Compose-only: bundled agents now always run on the container's loopback.
        "bundled_agents_use_service_hosts",
        "mongodb_username",
        "orchestration_outcome_guardrails",
        "supervisor_max_steps",
        "parse_confidence_threshold",
        "cloud_health_cache_ttl",
        "max_tasks_per_user",
        "max_tasks_per_room",
        "run_watchdog_stale_minutes",
        "processing_status_expiry_minutes",
        "a2a_inline_file_max_raw_bytes",
        "a2a_inline_message_max_encoded_bytes",
        "cancellation_ttl_seconds",
        "cancellation_cache_maxsize",
        "redis_cancel_channel",
        "redis_cancel_key_prefix",
        "cs_backoff_base",
        "cs_backoff_max",
        "cs_backoff_factor",
        "cs_jitter_fraction",
        "orphan_threshold_minutes",
        "stale_check_minutes",
        "task_expiry_hours",
        "pending_task_warning_hours",
        "webhook_base_url",
        "webhook_signing_key",
        "feature_run_event_sse",
        "context_model_window",
        "context_system_prompt_tokens",
        "context_tool_schema_tokens",
        "context_response_reserve_tokens",
        "context_room_pct",
        "context_history_pct",
        "context_task_pct",
        "memory_search_enabled",
        "memory_search_temporal_decay_enabled",
        "memory_search_half_life_days",
        "memory_search_max_results",
        "memory_search_max_candidates",
        "memory_search_max_snippet_chars",
    }
)
ROUTE_FIELDS = frozenset(
    {
        "lead_ai_model",
        "classifier_ai_model",
        "supervisor_model",
        "embedding_model",
        "deepseek_model_name",
        "llm_gateway_generation_provider",
        "llm_gateway_default_generation_model",
        "llm_gateway_default_embedding_model",
        "llm_gateway_default_supervisor_model",
    }
)


def strip_removed_fields(
    values: dict[str, object],
) -> tuple[dict[str, object], tuple[str, ...]]:
    """Return the values without retired keys, and the sorted retired keys found."""
    removed = tuple(sorted(REMOVED_FIELDS.intersection(values)))
    if not removed:
        return values, ()
    return {k: v for k, v in values.items() if k not in REMOVED_FIELDS}, removed


def validate_openai_base_url(value: str | None) -> str | None:
    """Validate an OpenAI-compatible base URL; never probe or rewrite it."""
    if not value:
        return None
    try:
        if "\\" in value:
            raise ValueError
        parsed = urlsplit(value)
        validated = AnyHttpUrl(value)
        if (
            not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or validated.username is not None
            or validated.password is not None
            or any(
                char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value
            )
        ):
            raise ValueError
    except ValueError:
        raise ValueError(
            "Invalid OPENAI_BASE_URL; use an HTTP(S) URL with a hostname and no userinfo."
        ) from None
    # Preserve the deployment's exact path; no DNS/network probe or URL rewrite.
    return value


def validate_backend(values: dict[str, object]) -> Settings:
    if PRIVATE_FIELDS.intersection(values) or ROUTE_FIELDS.intersection(values):
        raise ValueError("Use setup for model selection and auth.json for credentials")
    for key, value in values.items():
        if key.endswith("_url") and isinstance(value, str) and value:
            parsed = urlsplit(value)
            if parsed.username or parsed.password:
                raise ValueError("URL credentials belong in auth.json")
        if key == "openai_base_url" and value:
            validate_openai_base_url(value)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if not math.isfinite(value) or value < 0:
                raise ValueError("Numeric settings must be finite and nonnegative")
            if key.endswith("_port") and not 1 <= value <= 65535:
                raise ValueError("Port must be between 1 and 65535")
    return Settings.model_validate(values)


# Public projection fields that are not settings: MCP is always `{origin}/mcp`.
DERIVED_FRONTEND_FIELDS = frozenset({"mcp_url"})


class FrontendSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    api_base_url: str = "http://localhost:8000"
    api_prefix: str = "/api/v1"
    server_url: str = "http://localhost:3000"
    clerk_publishable_key: str = ""
    clerk_sign_in_url: str = "/sign-in"
    clerk_sign_up_url: str = "/sign-up"
    clerk_sign_in_fallback_redirect_url: str = "/"
    clerk_sign_up_fallback_redirect_url: str = "/"
    enable_waitlist: bool = True
    max_message_length: int = Field(default=10000, gt=0)
    inspection_timeout_ms: int = Field(default=300000, gt=0)

    @field_validator("api_base_url", "server_url")
    @classmethod
    def validate_url(cls, value: str, info: ValidationInfo) -> str:
        # An empty API base URL makes the browser call its own origin.
        if info.field_name == "api_base_url" and value == "":
            return value
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
        ):
            raise ValueError("Expected an HTTP(S) URL without credentials")
        return value

    @field_validator("api_prefix")
    @classmethod
    def validate_prefix(cls, value: str) -> str:
        import re

        if not re.fullmatch(r"/[A-Za-z0-9/_-]+", value) or value.endswith("/"):
            raise ValueError("Expected an absolute API path without a trailing slash")
        return value


# backend/common/config/loader.py -> the repository's default_agents/agents.yaml
_REPO_MANIFEST = Path(__file__).resolve().parents[3] / "default_agents" / "agents.yaml"


def _apply_image_defaults(values: dict) -> None:
    """Defaults for the single image (``HYBRO_CONTAINER=image``).

    Every service runs in one container on loopback. The entrypoint
    (``runtime_entrypoint.py``) derives this instance's frontend origins and the
    bundled agent ports and passes them as internal transport variables.
    """
    for key, value in {
        "mongodb_url": "mongodb://127.0.0.1:27017/hybro?replicaSet=rs0",
        "redis_url": "",
        "hybro_file_dir": "/data/files",
        "local_agent_discovery_enabled": True,
        "bundled_agents_manifest": "/app/bundled_agents.yaml",
        "oauth_callback_host": "0.0.0.0",
        # Bundled agents on container loopback and host agents on the same port
        # share a URL key, so discovery must not probe the manifest ports.
        "local_agent_discovery_excluded_ports": os.environ.get(
            "LOCAL_AGENT_DISCOVERY_EXCLUDED_PORTS", ""
        ),
    }.items():
        values.setdefault(key, value)
    origins = os.environ.get("HYBRO_FRONTEND_ORIGINS")
    if origins:
        values["frontend_origins"] = origins


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    from common.config.runtime_store import RuntimeConfigStore, runtime_home

    store = RuntimeConfigStore(runtime_home(os.environ))
    document = store.load({}).config
    if document.removed_backend_fields:
        logger.warning(
            "Ignoring removed config.json backend settings: %s. "
            "Delete them; they will be rejected in the next major release.",
            ", ".join(document.removed_backend_fields),
        )
    if document.had_instances:
        logger.warning(
            "Ignoring instances in config.json: host ports are set with "
            "`hybro start --port` since 0.4. It is dropped on the next save."
        )
    values = dict(document.backend)
    services = store.read_service_credentials()
    if any(key in values and key in services for key in ("mongodb_url", "redis_url")):
        raise ValueError(
            "Specify each connection URL in config.json or auth.json, not both"
        )
    values.update({k: v for k, v in services.items() if k in Settings.model_fields})
    # Fixed Docker topology is a deployment context, not another user config source.
    container = os.environ.get("HYBRO_CONTAINER")
    if container == "image":
        _apply_image_defaults(values)
    else:
        # A checkout registers the repository's bundled agent manifest.
        values.setdefault("bundled_agents_manifest", os.fspath(_REPO_MANIFEST))
    settings = Settings.model_validate(values)
    if settings.auth_mode == "clerk" and not settings.clerk_secret_key:
        raise ValueError("Clerk authentication requires auth.json credentials")
    return settings


def __getattr__(name: str) -> object:
    if name == "settings":
        return get_settings()
    raise AttributeError(name)


if TYPE_CHECKING:
    settings: Settings

__all__ = ["Settings", "get_settings", "settings"]

import math
import os

from pydantic import AliasChoices, Field, field_validator, model_validator
from pydantic_settings import BaseSettings


def resolve_settings_env_file(base_dir: str) -> str:
    """Return the single env file path Settings should load.

    Prefer the monorepo-root ``.env`` when both ``docker-compose.yml`` and
    that file exist. Never load root and ``backend/.env`` together — a stale
    backend file would override root values. Fall back to ``backend/.env``.
    """
    repo_root = os.path.dirname(base_dir)
    root_env = os.path.join(repo_root, ".env")
    backend_env = os.path.join(base_dir, ".env")
    if os.path.isfile(os.path.join(repo_root, "docker-compose.yml")) and os.path.isfile(
        root_env
    ):
        return root_env
    return backend_env


class Settings(BaseSettings):
    app_env: str = "development"  # development, staging, production

    frontend_origins: str | list[str] = [
        "http://localhost:3000",
        "https://hybro.ai",
    ]
    api_prefix: str = "/api/v1"

    mongodb_url: str = "localhost:27017"
    mongodb_db_name: str = "hybro"
    mongodb_host: str = "127.0.0.1"
    mongodb_port: int = 27017
    mongodb_username: str = ""
    mongodb_password: str = ""

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

    # LLM gateway routing and runtime policy
    llm_gateway_generation_provider: str = "openai"
    llm_gateway_max_attempts: int = 2
    llm_gateway_retry_backoff_seconds: float = 0.2
    llm_gateway_request_timeout_seconds: float = 60.0
    llm_gateway_stream_timeout_seconds: float = 120.0
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

    # Mandatory canonical lifecycle worker cadence.
    orchestrator_worker_interval_seconds: int = Field(default=30, gt=0)

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

    # Feature Flags (runtime-toggleable behavior gates)
    feature_run_event_sse: bool = True
    orchestration_outcome_guardrails: bool = True

    # Execution Tuning
    supervisor_max_steps: int = 8
    run_watchdog_stale_minutes: int = 90

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

    # Compaction
    compaction_concurrency: int = 5

    parse_confidence_threshold: float = 0.3

    # Clerk Authentication
    clerk_secret_key: str = ""  # Clerk Secret Key for backend API
    auth_mode: str = "mock"  # "mock" or "clerk"

    # Default-agent registrar bootstrap (service identity for one-shot registration)
    default_agent_registrar_token: str = ""
    # provider_id assigned to agents registered through the service token.
    default_agent_provider_id: str = "Hybro AI"

    # Agent Health Check Settings
    agent_health_check_enabled: bool = True  # enable/disable agent health check
    cloud_health_check_timeout: float = 5.0  # seconds for on-demand cloud agent probe
    cloud_health_cache_ttl: float = 30.0  # cache healthy/unhealthy result for this long

    # Agent Capability Issue Tracking
    capability_issue_threshold: int = 2  # Exclude agents with >= this many open issues

    # A2A Long-Running Tasks Settings
    webhook_base_url: str = (
        ""  # Public URL where agents send webhooks (e.g., https://api.example.com)
    )
    webhook_signing_key: str = ""  # Secret key for HMAC token hashing (min 32 chars)
    max_tasks_per_user: int = 100  # Max concurrent non-terminal tasks per user
    max_tasks_per_room: int = 50  # Max concurrent non-terminal tasks per room
    stale_check_minutes: int = 10  # Poll tasks not updated in this time
    task_expiry_hours: int = 4  # Auto-fail tasks older than this
    pending_task_warning_hours: int = 1  # Warn (log) after this time
    orphan_threshold_minutes: int = 2  # Recover orphaned messages older than this
    processing_status_expiry_minutes: int = (
        30  # Clear stuck processing status older than this
    )

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

    # Execution cancellation runtime (environment names remain compatible)
    cancellation_ttl_seconds: int = 3600
    cancellation_cache_maxsize: int = 10_000

    # Cancellation change stream reconnection backoff
    cs_backoff_base: float = 1.0  # initial delay in seconds
    cs_backoff_max: float = 30.0  # ceiling delay in seconds
    cs_backoff_factor: float = 2.0  # multiplier per retry
    cs_jitter_fraction: float = 0.25  # +/-25% random jitter

    # Redis transports (Delivery SSE and Execution cancellation use separate clients)
    redis_url: str = (
        ""  # e.g. "redis://localhost:6379/0" - empty string disables broker
    )
    redis_sse_channel_prefix: str = "sse:room:"  # per-room channel: sse:room:{room_id}
    redis_cancel_channel: str = (
        "cancel:global"  # single channel for all cancellation events
    )
    redis_reconnect_delay: float = 1.0  # initial reconnect delay (seconds)
    redis_reconnect_max_delay: float = 30.0  # max reconnect delay ceiling (seconds)
    redis_cancel_key_prefix: str = "cancelled:"
    redis_terminal_key_prefix: str = "terminal:"

    # ===========================================
    # Context & Memory System Settings
    # See docs/System-Architecture.md for the current architecture
    # ===========================================

    # Token Budget Settings
    context_model_window: int = 128000  # Model's max context window
    context_system_prompt_tokens: int = 2000  # Reserved for system prompt
    context_tool_schema_tokens: int = 3000  # Reserved for tool schemas
    context_response_reserve_tokens: int = 4000  # Reserved for response
    context_room_pct: float = 0.15  # % of remaining for room context
    context_history_pct: float = 0.60  # % of remaining for conversation history
    context_task_pct: float = 0.25  # % of remaining for current task

    # Compaction Settings (LOSSLESS - pointer-based, not summarization)
    compaction_enabled: bool = True  # Enable/disable auto-compaction
    compaction_max_full_turns: int = 20  # Max turns to keep in FULL representation
    compaction_max_total_tokens: int = (
        80000  # Trigger compaction when full turns exceed this
    )
    compaction_preserve_recent: int = 10  # Always keep this many recent turns FULL
    compaction_content_ttl_days: int = 0  # TTL for stored content (0 = forever)

    # Memory Search Settings
    memory_search_enabled: bool = True  # Enable/disable memory search
    memory_search_temporal_decay_enabled: bool = True  # Enable recency boost
    memory_search_half_life_days: int = 30  # Half-life for temporal decay
    memory_search_max_results: int = 10  # Max results to return
    memory_search_max_candidates: int = 1000  # Max keyword candidates to rank
    memory_search_max_snippet_chars: int = 300  # Max chars per snippet

    # Local room file storage
    hybro_file_dir: str = Field(
        default="",
        validation_alias=AliasChoices("HYBRO_FILE_DIR", "hybro_file_dir"),
    )
    a2a_inline_file_max_raw_bytes: int = 5 * 1024 * 1024
    a2a_inline_message_max_encoded_bytes: int = 0

    # Graceful Shutdown Settings
    shutdown_drain_seconds: float = (
        5.0  # Drain period for SSE connections during shutdown
    )

    # Connection pool tuning (per-worker; total = workers * value)
    mongodb_max_pool_size: int = 50
    mongodb_min_pool_size: int = 10
    redis_max_connections: int = 50

    class Config:
        extra = "ignore"
        # backend/ when running from the monorepo or /app in the backend image.
        base_dir = os.path.dirname(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        )
        # See resolve_settings_env_file(). Missing files are ignored by
        # pydantic-settings. Under Docker Compose, process env is primary.
        env_file = resolve_settings_env_file(base_dir)

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

    @field_validator("compaction_concurrency", mode="before")
    @classmethod
    def normalize_compaction_concurrency(cls, value):
        try:
            return max(1, int(value))
        except (TypeError, ValueError):
            return 5

    @field_validator("webhook_signing_key", mode="before")
    @classmethod
    def validate_webhook_signing_key(cls, value):
        key = str(value or "").strip()
        if key and len(key.encode()) < 32:
            raise ValueError("WEBHOOK_SIGNING_KEY must be at least 32 bytes")
        return key

    @field_validator("feature_run_event_sse", mode="before")
    @classmethod
    def normalize_feature_run_event_sse(cls, value):
        if value is None or str(value).strip() == "":
            return False
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"1", "true", "yes", "on"}

    @field_validator("orchestration_outcome_guardrails", mode="before")
    @classmethod
    def normalize_orchestration_outcome_guardrails(cls, value):
        if value is None or str(value).strip() == "":
            return False
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"1", "true", "yes", "on"}

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

    @field_validator("a2a_inline_file_max_raw_bytes", mode="before")
    @classmethod
    def normalize_a2a_inline_file_max_raw_bytes(cls, value):
        try:
            return max(1, int(value))
        except (TypeError, ValueError):
            return 5 * 1024 * 1024

    @field_validator("a2a_inline_message_max_encoded_bytes", mode="before")
    @classmethod
    def normalize_a2a_inline_message_max_encoded_bytes(cls, value):
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    @model_validator(mode="after")
    def apply_a2a_inline_encoded_default(self):
        if self.a2a_inline_message_max_encoded_bytes <= 0:
            self.a2a_inline_message_max_encoded_bytes = 4 * math.ceil(
                self.a2a_inline_file_max_raw_bytes / 3
            )
        return self

    @property
    def is_gunicorn(self) -> bool:
        """Detect gunicorn from server-injected runtime metadata."""
        return os.environ.get("SERVER_SOFTWARE", "").startswith("gunicorn")


settings = Settings()


__all__ = [
    "Settings",
    "resolve_settings_env_file",
    "settings",
]

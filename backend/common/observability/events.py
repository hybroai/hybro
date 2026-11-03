"""The backend log event catalog.

Every backend log line is one of these events, emitted with
``get_logger(__name__).emit(events.X, **fields)``. ``required`` lists the
fields every call site supplies (bound log context counts); ``system`` marks
events a later phase copies into the system event store.
"""

from __future__ import annotations

from logging import DEBUG, ERROR, INFO, WARNING

from common.observability.catalog import Catalog

CATALOG = Catalog()
define = CATALOG.define

# Transport

HTTP_REQUEST_COMPLETED = define(
    "http_request_completed",
    INFO,
    required={"request_id", "method", "route", "status", "outcome", "duration_ms"},
    system=True,
)
LLM_CALL_COMPLETED = define(
    "llm_call_completed",
    INFO,
    required={"provider", "model", "operation", "attempt", "outcome", "duration_ms"},
    system=True,
)
A2A_CALL_COMPLETED = define(
    "a2a_call_completed",
    INFO,
    required={"agent", "operation", "attempt", "outcome", "duration_ms"},
    system=True,
)

# a2a_adapter

ARTIFACT_MATERIALIZATION_FAILED = define("artifact_materialization_failed", WARNING)
ARTIFACT_MATERIALIZATION_STORE_RETRY = define(
    "artifact_materialization_store_retry", WARNING
)
A2A_DOCKER_HOST_FALLBACK_SELECTED = define("a2a_docker_host_fallback_selected", DEBUG)

# agent

BUNDLED_AGENT_URL_TAKEN = define("bundled_agent_url_taken", WARNING)
BUNDLED_AGENT_REGISTERED = define("bundled_agent_registered", INFO)
BUNDLED_AGENT_REGISTRATION_FAILED = define("bundled_agent_registration_failed", WARNING)
AGENT_HEALTH_PROBE_UNREACHABLE = define("agent_health_probe_unreachable", WARNING)
AGENT_HEALTH_CHECK_FAILED = define("agent_health_check_failed", ERROR)
AGENT_CARD_UPDATE_FAILED = define("agent_card_update_failed", WARNING)
AGENT_STATUS_UPDATE_FAILED = define("agent_status_update_failed", ERROR)
AGENT_HEALTH_RETRY_FAILED = define("agent_health_retry_failed", ERROR)
AGENT_HEALTH_CYCLE_FAILED = define("agent_health_cycle_failed", ERROR)

# api_gateway

AGENT_CARD_INSPECTION_RECEIVED = define("agent_card_inspection_received", INFO)
AGENT_CONNECTION_INSPECTION_RECEIVED = define(
    "agent_connection_inspection_received", INFO
)
GATEWAY_SEND_MESSAGE_RECEIVED = define("gateway_send_message_received", INFO)
GATEWAY_SEND_MESSAGE_COMPLETED = define("gateway_send_message_completed", INFO)
GATEWAY_SEND_MESSAGE_BACKGROUND_SCHEDULED = define(
    "gateway_send_message_background_scheduled", INFO
)
MESSAGE_CANCELLATION_FAILED = define("message_cancellation_failed", ERROR)
SSE_STREAM_FAILED = define("sse_stream_failed", ERROR)
SSE_CONNECTION_FAILED = define("sse_connection_failed", ERROR)

# common

CLERK_USER_NAME_RESOLUTION_FAILED = define("clerk_user_name_resolution_failed", WARNING)
CLERK_TOKEN_VERIFICATION_FAILED = define("clerk_token_verification_failed", ERROR)
INVALID_ARTIFACT_PART_DROPPED = define("invalid_artifact_part_dropped", WARNING)
TOKEN_ESTIMATION_FALLBACK_SELECTED = define("token_estimation_fallback_selected", DEBUG)

# delivery

DELIVERY_COMPLETED = define("delivery_completed", INFO)

# execution

AGENT_CALL_COUNT_FAILED = define("agent_call_count_failed", WARNING)
HITL_CONTINUATION_CLAIM_UNAVAILABLE = define(
    "hitl_continuation_claim_unavailable", WARNING
)
HITL_ANSWER_RECONCILE_DEFERRED = define("hitl_answer_reconcile_deferred", WARNING)
HITL_CONTINUATION_RECOVERY_DEFERRED = define(
    "hitl_continuation_recovery_deferred", WARNING
)

# llm_gateway

PROVIDER_CONFIG_RELOADED = define("provider_config_reloaded", INFO)
PROVIDER_CONFIG_RELOAD_FAILED = define("provider_config_reload_failed", WARNING)
PROVIDER_CONFIG_REFRESH_FAILED = define("provider_config_refresh_failed", WARNING)
PROVIDER_LISTENER_FAILED = define("provider_listener_failed", WARNING)
OAUTH_CALLBACK_LISTENER_UNAVAILABLE = define(
    "oauth_callback_listener_unavailable", INFO
)
OAUTH_CODE_EXCHANGE_FAILED = define("oauth_code_exchange_failed", WARNING)

# local_agents

LOCAL_AGENT_DISCOVERY_CYCLE_FAILED = define("local_agent_discovery_cycle_failed", ERROR)

# session

SESSION_ORPHAN_CONTAINER_DELETE_FAILED = define(
    "session_orphan_container_delete_failed", WARNING
)
SESSION_MEMBER_ADD_FAILED = define("session_member_add_failed", WARNING)
SESSION_CLOSE_PENDING_LOOKUP_FAILED = define(
    "session_close_pending_lookup_failed", WARNING
)
SESSION_CLOSE_INTERACTION_CANCEL_FAILED = define(
    "session_close_interaction_cancel_failed", WARNING
)
SESSION_CLOSE_RUN_CANCEL_FAILED = define("session_close_run_cancel_failed", WARNING)

del define

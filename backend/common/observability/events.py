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

ARTIFACT_MATERIALIZATION_FAILED = define(
    "artifact_materialization_failed",
    WARNING,
    required={
        "conversation_id",
        "message_id",
        "artifact_ref",
        "part_slot",
        "failure_code",
    },
)
ARTIFACT_MATERIALIZATION_STORE_RETRIED = define(
    "artifact_materialization_store_retried",
    WARNING,
    required={"conversation_id", "message_id", "attempt", "max_attempts"},
)
A2A_DOCKER_HOST_FALLBACK_SELECTED = define(
    "a2a_docker_host_fallback_selected",
    DEBUG,
    required={"original_url", "fallback_url"},
)
AGENT_CARD_HEALTH_PROBE_TIMED_OUT = define(
    "agent_card_health_probe_timed_out",
    WARNING,
    required={"agent_url", "timeout_seconds"},
)
AGENT_CARD_HEALTH_PROBE_FAILED = define(
    "agent_card_health_probe_failed", WARNING, required={"agent_url"}
)
AGENT_CARD_RESOLUTION_FAILED = define(
    "agent_card_resolution_failed", WARNING, required={"agent_url"}
)
TASK_ARTIFACTS_MATERIALIZATION_FAILED = define(
    "task_artifacts_materialization_failed",
    ERROR,
    required={"operation", "call_record_id", "task_id"},
)
REMOTE_TASK_FETCH_FAILED = define(
    "remote_task_fetch_failed", ERROR, required={"task_id"}
)

# agent

BUNDLED_AGENT_URL_TAKEN = define(
    "bundled_agent_url_taken",
    WARNING,
    required={"bundled_name", "agent_id", "holder_source"},
)
BUNDLED_AGENT_REGISTERED = define(
    "bundled_agent_registered", INFO, required={"bundled_name", "agent_id"}
)
BUNDLED_AGENT_REGISTRATION_FAILED = define(
    "bundled_agent_registration_failed", WARNING, required={"bundled_name"}
)
AGENT_HEALTH_PROBE_SUCCEEDED = define(
    "agent_health_probe_succeeded", DEBUG, required={"agent_id", "card_parsed"}
)
AGENT_HEALTH_PROBE_FAILED = define(
    "agent_health_probe_failed", WARNING, required={"agent_id", "status_code"}
)
AGENT_HEALTH_CHECK_FAILED = define(
    "agent_health_check_failed", ERROR, required={"agent_id"}
)
AGENT_HEALTH_CHECK_SKIPPED = define(
    "agent_health_check_skipped", DEBUG, required={"agent_id", "reason"}
)
AGENT_CARD_SYNCED = define(
    "agent_card_synced", DEBUG, required={"agent_id", "changed_field_count"}
)
AGENT_CARD_SYNC_FAILED = define(
    "agent_card_sync_failed", WARNING, required={"agent_id"}
)
AGENT_STATUS_UPDATE_FAILED = define(
    "agent_status_update_failed", ERROR, required={"agent_id", "agent_status"}
)
AGENT_DEACTIVATED = define(
    "agent_deactivated", WARNING, required={"agent_id", "trigger"}
)
AGENT_REACTIVATED = define("agent_reactivated", INFO, required={"agent_id", "trigger"})
AGENT_HEALTH_RETRY_SCHEDULED = define(
    "agent_health_retry_scheduled",
    DEBUG,
    required={"agent_id", "delay_seconds", "consecutive_failure_count"},
)
AGENT_HEALTH_RETRY_CANCELLED = define(
    "agent_health_retry_cancelled", DEBUG, required={"agent_id"}
)
AGENT_HEALTH_RETRY_FAILED = define(
    "agent_health_retry_failed", ERROR, required={"agent_id"}
)
AGENT_HEALTH_CYCLE_STARTED = define(
    "agent_health_cycle_started", INFO, required={"agent_count"}
)
AGENT_HEALTH_CYCLE_FAILED = define("agent_health_cycle_failed", ERROR)
AGENT_LIVENESS_PROBE_FAILED = define(
    "agent_liveness_probe_failed", WARNING, required={"agent_id"}
)
AGENT_REGISTRATION_FAILED = define(
    "agent_registration_failed", ERROR, required={"provider_id"}
)
LOCAL_AGENT_RECORD_TAKEN_OVER = define(
    "local_agent_record_taken_over", INFO, required={"agent_id", "provider_id"}
)
VISIBLE_AGENTS_LIST_FAILED = define(
    "visible_agents_list_failed", ERROR, required={"user_id", "active_only"}
)
AGENT_INSPECTION_REQUEST_REJECTED = define(
    "agent_inspection_request_rejected", WARNING, required={"inspection"}
)
AGENT_INSPECTION_FAILED = define(
    "agent_inspection_failed", ERROR, required={"inspection"}
)
AGENT_DOCUMENT_PARSE_FAILED = define(
    "agent_document_parse_failed", WARNING, required={"document_kind"}
)
AGENT_RUNTIME_STORE_OPERATION_FAILED = define(
    "agent_runtime_store_operation_failed", ERROR, required={"operation"}
)
AGENT_TEXT_SEARCH_FAILED = define(
    "agent_text_search_failed", WARNING, required={"candidate_count"}
)
AGENT_SELECTION_STARTED = define(
    "agent_selection_started", INFO, required={"user_id", "message_char_count"}
)
AGENT_SELECTION_COMPLETED = define(
    "agent_selection_completed",
    INFO,
    required={"user_id", "strategy", "selected_agent_count", "candidate_count"},
)
AGENT_SELECTION_RERANK_FAILED = define(
    "agent_selection_rerank_failed", WARNING, required={"candidate_count"}
)

# api_gateway

AGENT_CARD_INSPECTION_RECEIVED = define(
    "agent_card_inspection_received", INFO, required={"agent_url"}
)
AGENT_CONNECTION_INSPECTION_RECEIVED = define(
    "agent_connection_inspection_received", INFO, required={"agent_url"}
)
GATEWAY_SEND_MESSAGE_RECEIVED = define(
    "gateway_send_message_received",
    INFO,
    required={"conversation_id", "client_request_id", "mode"},
)
GATEWAY_SEND_MESSAGE_COMPLETED = define(
    "gateway_send_message_completed",
    INFO,
    required={"conversation_id", "message_id", "client_request_id", "outcome"},
)
GATEWAY_SEND_MESSAGE_BACKGROUND_SCHEDULED = define(
    "gateway_send_message_background_scheduled",
    INFO,
    required={"conversation_id", "message_id", "client_request_id"},
)
ACTIVE_RUN_LOOKUP_FAILED = define(
    "active_run_lookup_failed", WARNING, required={"conversation_id"}
)
SSE_SNAPSHOT_BUILD_FAILED = define(
    "sse_snapshot_build_failed", WARNING, required={"conversation_id"}
)
SSE_STREAM_STARTED = define(
    "sse_stream_started", INFO, required={"conversation_id", "connection_id"}
)
SSE_STREAM_CLOSED = define(
    "sse_stream_closed", INFO, required={"conversation_id", "connection_id"}
)
SSE_STREAM_FAILED = define("sse_stream_failed", ERROR, required={"conversation_id"})
SSE_CONNECTION_FAILED = define(
    "sse_connection_failed", ERROR, required={"conversation_id"}
)
MESSAGE_CANCELLED = define(
    "message_cancelled", INFO, required={"message_id", "user_id"}
)
MESSAGE_CANCELLATION_FAILED = define(
    "message_cancellation_failed", ERROR, required={"message_id"}
)

# approval

HITL_STORE_OPERATION_FAILED = define(
    "hitl_store_operation_failed", ERROR, required={"operation"}
)
PENDING_HITL_REQUEST_CONFLICT_DETECTED = define(
    "pending_hitl_request_conflict_detected",
    ERROR,
    required={
        "lookup",
        "conversation_id",
        "display_message_id",
        "continuation_message_id",
        "display_request_id",
        "continuation_request_id",
    },
)
PENDING_HITL_DUPLICATE_READBACK_FAILED = define(
    "pending_hitl_duplicate_readback_failed",
    ERROR,
    required={
        "conversation_id",
        "hitl_request_id",
        "display_message_id",
        "continuation_message_id",
    },
)
HITL_REQUEST_REJECTED = define(
    "hitl_request_rejected",
    ERROR,
    required={
        "conversation_id",
        "interaction_id",
        "run_id",
        "public_source",
        "reason",
    },
)
HITL_ROUND_LIMIT_REACHED = define(
    "hitl_round_limit_reached",
    WARNING,
    required={
        "conversation_id",
        "interaction_id",
        "run_id",
        "continuation_message_id",
        "round_count",
        "max_round_count",
    },
)
HITL_REQUEST_PERSIST_FAILED = define(
    "hitl_request_persist_failed",
    ERROR,
    required={
        "conversation_id",
        "interaction_id",
        "run_id",
        "hitl_request_id",
        "public_source",
    },
)
REUSED_HITL_REQUEST_REPAIR_FAILED = define(
    "reused_hitl_request_repair_failed",
    ERROR,
    required={"conversation_id", "interaction_id", "run_id", "hitl_request_id"},
)
HITL_DISPLAY_PROJECTION_FAILED = define(
    "hitl_display_projection_failed",
    WARNING,
    required={"conversation_id", "hitl_request_id", "display_message_id"},
)
HITL_RUN_STATE_READ_FAILED = define(
    "hitl_run_state_read_failed", WARNING, required={"run_id"}
)
HITL_ANSWER_APPLICATION_FAILED = define(
    "hitl_answer_application_failed",
    ERROR,
    required={"interaction_id", "conversation_id", "run_id", "client_request_id"},
)
HITL_INPUT_RECEIVED_EMISSION_FAILED = define(
    "hitl_input_received_emission_failed",
    WARNING,
    required={"hitl_request_id", "interaction_id", "conversation_id"},
)
HITL_RECONCILIATION_PASS_FAILED = define(
    "hitl_reconciliation_pass_failed", ERROR, required={"reconciliation_pass"}
)
HITL_MATERIALIZATION_RESUME_DEFERRED = define(
    "hitl_materialization_resume_deferred",
    WARNING,
    required={"interaction_id", "conversation_id", "run_id"},
)
HITL_INTERACTION_EXPIRY_DEFERRED = define(
    "hitl_interaction_expiry_deferred",
    ERROR,
    required={"interaction_id", "conversation_id", "run_id"},
)
HITL_ANSWER_REFS_REPAIR_DEFERRED = define(
    "hitl_answer_refs_repair_deferred",
    WARNING,
    required={"interaction_id", "conversation_id", "run_id"},
)
HITL_APPLICATION_RESUME_DEFERRED = define(
    "hitl_application_resume_deferred",
    WARNING,
    required={"interaction_id", "conversation_id", "run_id"},
)
HITL_RESUME_COMMAND_RECONCILE_DEFERRED = define(
    "hitl_resume_command_reconcile_deferred",
    WARNING,
    required={"command_id", "interaction_id", "run_id"},
)
HITL_TERMINAL_INTERACTION_RECONCILE_DEFERRED = define(
    "hitl_terminal_interaction_reconcile_deferred",
    WARNING,
    required={"interaction_id", "conversation_id", "run_id"},
)
HITL_TERMINAL_REQUEST_RECONCILE_DEFERRED = define(
    "hitl_terminal_request_reconcile_deferred",
    WARNING,
    required={"hitl_request_id", "interaction_id", "conversation_id"},
)
HITL_OPEN_PROJECTION_RECOVERY_DEFERRED = define(
    "hitl_open_projection_recovery_deferred",
    WARNING,
    required={"interaction_id", "conversation_id", "run_id"},
)

# common

CLERK_USER_NAME_RESOLUTION_FAILED = define(
    "clerk_user_name_resolution_failed", WARNING, required={"user_id"}
)
CLERK_TOKEN_VERIFICATION_FAILED = define("clerk_token_verification_failed", ERROR)
REMOVED_CONFIG_FIELDS_IGNORED = define(
    "removed_config_fields_ignored", WARNING, required={"removed_fields"}
)
CONFIG_INSTANCES_IGNORED = define("config_instances_ignored", WARNING)
INVALID_ARTIFACT_PART_DROPPED = define(
    "invalid_artifact_part_dropped", WARNING, required={"reason"}
)
A2A_PART_SKIPPED = define("a2a_part_skipped", WARNING, required={"part_kind"})
TASK_MESSAGE_RESOLVED = define("task_message_resolved", DEBUG, required={"source"})
TASK_MESSAGE_NOT_FOUND = define("task_message_not_found", WARNING, required={"task_id"})
TASK_ARTIFACT_ID_NOT_FOUND = define("task_artifact_id_not_found", WARNING)
TASK_ARTIFACT_MERGED = define(
    "task_artifact_merged", DEBUG, required={"artifact_id", "merge_action"}
)
TASK_ARTIFACT_APPEND_TARGET_NOT_FOUND = define(
    "task_artifact_append_target_not_found", WARNING, required={"artifact_id"}
)
TOKEN_ESTIMATION_FALLBACK_SELECTED = define(
    "token_estimation_fallback_selected", DEBUG, required={"reason"}
)
INDEX_CREATION_FAILED = define(
    "index_creation_failed",
    WARNING,
    required={"collection_name", "index_name", "critical"},
)
INDEX_CONFLICT_DETECTED = define(
    "index_conflict_detected",
    WARNING,
    required={"collection_name", "index_name", "critical"},
)
INDEX_RECREATION_FAILED = define(
    "index_recreation_failed",
    ERROR,
    required={"collection_name", "index_name", "critical"},
)
SEARCH_INDEX_CREATION_FAILED = define(
    "search_index_creation_failed", WARNING, required={"index_name"}
)
EVENTING_DEAD_LETTER = define(
    "eventing_dead_letter",
    WARNING,
    required={"failure_stage", "exception_class"},
    system=True,
)
EVENTING_DEAD_LETTER_PUBLISH_FAILED = define(
    "eventing_dead_letter_publish_failed",
    WARNING,
    required={"failure_stage"},
    system=True,
)

# container

MULTI_WORKER_SAFETY_CHECK_PASSED = define("multi_worker_safety_check_passed", INFO)
RUNTIME_CLEANUP_TASK_FAILED = define(
    "runtime_cleanup_task_failed", WARNING, required={"cleanup_step"}
)
RUNTIME_CLEANUP_STEP_TIMED_OUT = define(
    "runtime_cleanup_step_timed_out", WARNING, required={"cleanup_step"}
)
RUNTIME_CLEANUP_STEP_FAILED = define(
    "runtime_cleanup_step_failed", WARNING, required={"cleanup_step"}
)
STARTUP_BINDINGS_VERIFIED = define("startup_bindings_verified", INFO)
RESYNC_SNAPSHOT_BUILD_FAILED = define(
    "resync_snapshot_build_failed", WARNING, required={"conversation_id"}
)
SUPERVISOR_ASK_USER_RESUME_FAILED = define(
    "supervisor_ask_user_resume_failed",
    WARNING,
    required={"run_id", "call_id", "conversation_id"},
)
REDIS_DISABLED = define("redis_disabled", INFO)
REDIS_KV_CONNECTED = define("redis_kv_connected", INFO)
REDIS_KV_CONNECTION_FAILED = define("redis_kv_connection_failed", WARNING)
REDIS_STREAMS_CONNECTED = define("redis_streams_connected", INFO)
REDIS_STREAMS_CONNECTION_FAILED = define("redis_streams_connection_failed", WARNING)
LEADER_ELECTION_ENABLED = define("leader_election_enabled", INFO)
ORCHESTRATOR_PROJECTION_NUDGE_FAILED = define(
    "orchestrator_projection_nudge_failed", WARNING
)
ORCHESTRATOR_RUNTIME_COMPOSED = define("orchestrator_runtime_composed", INFO)
ORCHESTRATOR_INGRESS_ADAPTER_BOUND = define("orchestrator_ingress_adapter_bound", INFO)
SHUTDOWN_EXECUTION_TASKS_CANCELLED = define(
    "shutdown_execution_tasks_cancelled", INFO, required={"cancelled_tasks"}
)

# jobs

JOB_STARTED = define("job_started", INFO, required={"job", "interval_seconds"})
JOB_STOPPED = define("job_stopped", INFO, required={"job"})
JOB_ITERATION_FAILED = define(
    "job_iteration_failed", ERROR, required={"job"}, system=True
)
JOB_SKIPPED_NOT_LEADER = define("job_skipped_not_leader", DEBUG, required={"job"})
JOB_ALREADY_STARTED = define("job_already_started", WARNING, required={"job"})
JOB_START_SKIPPED = define("job_start_skipped", INFO, required={"job", "reason"})
ORCHESTRATOR_CANARY_THRESHOLD_BREACHED = define(
    "orchestrator_canary_threshold_breached",
    WARNING,
    required={"metric", "threshold", "observed"},
)
HITL_RECONCILIATION_SKIPPED = define(
    "hitl_reconciliation_skipped", WARNING, required={"reason"}
)
ROOM_COMPACTED = define(
    "room_compacted",
    INFO,
    required={"conversation_id", "compacted_turns", "tokens_saved"},
)
ROOM_COMPACTION_FAILED = define(
    "room_compaction_failed", WARNING, required={"conversation_id"}
)
COMPACTION_SWEEP_COMPLETED = define(
    "compaction_sweep_completed",
    INFO,
    required={"scanned_rooms", "compacted_rooms", "skipped_rooms", "failed_rooms"},
)
ORPHANED_UPLOADS_CLEANED = define(
    "orphaned_uploads_cleaned", INFO, required={"deleted_uploads"}
)

# orchestrator_composition

ORCHESTRATOR_RUN_RECOVERY_QUARANTINED = define(
    "orchestrator_run_recovery_quarantined",
    WARNING,
    required={"run_id", "quarantine_reason", "failure_count"},
)
ORCHESTRATOR_RUN_RECOVERY_CONFLICT_RETRIED = define(
    "orchestrator_run_recovery_conflict_retried",
    DEBUG,
    required={"run_id", "failure_count"},
)
ORCHESTRATOR_RUN_RECOVERY_FAILED = define(
    "orchestrator_run_recovery_failed",
    ERROR,
    required={"run_id", "failure_count"},
)

# dal

REDIS_LOCK_OPERATION_FAILED = define(
    "redis_lock_operation_failed",
    WARNING,
    required={"operation", "lock_kind"},
    system=True,
)

# context_memory

CONTEXT_MEMORY_PROJECTION_FAILED = define(
    "context_memory_projection_failed",
    ERROR,
    required={"conversation_id", "message_id"},
)
CONTEXT_MEMORY_CONTENT_CLEANUP_FAILED = define(
    "context_memory_content_cleanup_failed", ERROR, required={"conversation_id"}
)
TURN_COMPACTION_WRITE_FAILED = define(
    "turn_compaction_write_failed",
    ERROR,
    required={"conversation_id", "compacted_turn_count"},
)
OBSOLETE_INDEX_DROP_FAILED = define(
    "obsolete_index_drop_failed",
    WARNING,
    required={"collection_name", "index_name"},
)

# conversation

CONVERSATION_STORE_OPERATION_FAILED = define(
    "conversation_store_operation_failed", ERROR, required={"operation"}
)
CONVERSATION_DOCUMENT_PARSE_FAILED = define(
    "conversation_document_parse_failed", WARNING, required={"document_kind"}
)
TIMELINE_MESSAGE_PARSE_FAILED = define(
    "timeline_message_parse_failed",
    ERROR,
    required={"conversation_id", "message_id", "message_source"},
)
ROOM_TIMELINE_READ_FAILED = define(
    "room_timeline_read_failed", ERROR, required={"conversation_id"}
)
INDEX_READINESS_CHECK_FAILED = define(
    "index_readiness_check_failed",
    ERROR,
    required={"readiness_check", "issue_count"},
)
ROOM_EPOCH_ACTIVATION_FAILED = define(
    "room_epoch_activation_failed", ERROR, required={"conversation_id"}
)
ROOM_CREATION_COMPENSATION_FAILED = define(
    "room_creation_compensation_failed", WARNING, required={"conversation_id"}
)
ROOM_ACTIVITY_UPDATE_FAILED = define(
    "room_activity_update_failed", WARNING, required={"conversation_id"}
)
ROOM_DELETION_STEP_FAILED = define(
    "room_deletion_step_failed",
    WARNING,
    required={"conversation_id", "deletion_step"},
)
ROOM_EPOCH_DEACTIVATION_CONFLICT_DETECTED = define(
    "room_epoch_deactivation_conflict_detected",
    WARNING,
    required={"conversation_id", "deletion_id", "room_epoch"},
)
QUOTE_SNIPPET_CREATION_FAILED = define(
    "quote_snippet_creation_failed",
    ERROR,
    required={"conversation_id", "user_id", "client_request_id"},
)
QUOTE_SOURCE_LOOKUP_FAILED = define(
    "quote_source_lookup_failed", WARNING, required={"conversation_id", "run_id"}
)
QUOTE_DELETION_FAILED = define(
    "quote_deletion_failed",
    WARNING,
    required={"conversation_id", "quote_id", "client_request_id"},
)
QUOTE_DISPLAY_FIELDS_DISCARDED = define(
    "quote_display_fields_discarded",
    DEBUG,
    required={"conversation_id", "client_request_id", "field_names"},
)
MEMBERSHIP_AGENTS_FALLBACK_SELECTED = define(
    "membership_agents_fallback_selected", DEBUG, required={"user_id"}
)
MEMBERSHIP_AGENT_FIELDS_NOT_FOUND = define(
    "membership_agent_fields_not_found",
    WARNING,
    required={"agent_id", "missing_agent_fields"},
)
MENTIONS_DROPPED = define(
    "mentions_dropped",
    WARNING,
    required={"reason", "dropped_mention_count"},
)
TARGET_SCOPE_REJECTED = define(
    "target_scope_rejected", WARNING, required={"scope_source", "reason"}
)
TARGET_SCOPE_RESOLVED = define(
    "target_scope_resolved",
    INFO,
    required={"conversation_id", "scope_source", "candidate_count"},
)
TARGET_SCOPE_RESOLUTION_FAILED = define(
    "target_scope_resolution_failed",
    ERROR,
    required={"conversation_id", "scope_source"},
)
USER_MESSAGE_PERSIST_STARTED = define(
    "user_message_persist_started",
    INFO,
    required={
        "conversation_id",
        "user_id",
        "client_request_id",
        "target_group",
        "candidate_scope_mode",
        "selected_agent_count",
        "mentioned_agent_count",
        "message_char_count",
    },
)
USER_MESSAGE_PERSISTED = define(
    "user_message_persisted",
    INFO,
    required={"conversation_id", "message_id", "client_request_id", "target_group"},
)
FILE_REFERENCE_CLAIM_FAILED = define(
    "file_reference_claim_failed",
    WARNING,
    required={"conversation_id", "message_id", "client_request_id", "file_count"},
)
FILE_REFERENCE_RELEASE_FAILED = define(
    "file_reference_release_failed",
    WARNING,
    required={
        "conversation_id",
        "message_id",
        "client_request_id",
        "file_count",
        "release_reason",
    },
)
FILE_REFERENCE_COMMIT_FAILED = define(
    "file_reference_commit_failed",
    WARNING,
    required={"conversation_id", "message_id", "client_request_id", "file_count"},
)
MESSAGE_PREFLIGHT_SCOPE_RESOLVED = define(
    "message_preflight_scope_resolved",
    INFO,
    required={
        "conversation_id",
        "message_id",
        "client_request_id",
        "target_group",
        "candidate_count",
        "auto_assign",
    },
)
ORCHESTRATION_ENVELOPE_PREPARED = define(
    "orchestration_envelope_prepared",
    INFO,
    required={"conversation_id", "message_id", "client_request_id", "candidate_count"},
)
ORCHESTRATION_ENVELOPE_PERSIST_FAILED = define(
    "orchestration_envelope_persist_failed",
    WARNING,
    required={"conversation_id", "message_id", "client_request_id"},
)
MESSAGE_PREFLIGHT_FAILED = define(
    "message_preflight_failed",
    WARNING,
    required={"conversation_id", "message_id", "client_request_id", "canceled"},
)
MESSAGE_PREFLIGHT_COMPLETED = define(
    "message_preflight_completed",
    INFO,
    required={
        "conversation_id",
        "message_id",
        "client_request_id",
        "preflight_outcome",
    },
)
HITL_PROJECTION_VERIFICATION_FAILED = define(
    "hitl_projection_verification_failed",
    WARNING,
    required={"conversation_id", "message_id", "hitl_request_id"},
)
ARTIFACT_APPEND_SKIPPED = define(
    "artifact_append_skipped", WARNING, required={"message_id", "reason"}
)

# delivery

DELIVERY_COMPLETED = define(
    "delivery_completed",
    INFO,
    required={
        "conversation_id",
        "message_id",
        "outcome",
        "terminal_kind",
        "duration_ms",
    },
)
CANONICAL_FINAL_CONFLICT_IGNORED = define(
    "canonical_final_conflict_ignored", WARNING, required={"message_id", "run_id"}
)
TERMINAL_DEDUP_REDIS_OPERATION_TIMED_OUT = define(
    "terminal_dedup_redis_operation_timed_out",
    WARNING,
    required={"operation", "timeout_seconds"},
)

# execution

PENDING_HITL_LOOKUP_FAILED = define(
    "pending_hitl_lookup_failed", WARNING, required={"conversation_id"}
)
PREFLIGHT_STATUS_EMISSION_FAILED = define(
    "preflight_status_emission_failed",
    WARNING,
    required={"conversation_id", "status_stage"},
)
PREFLIGHT_CLEANUP_FAILED = define(
    "preflight_cleanup_failed", WARNING, required={"conversation_id"}
)
ORCHESTRATION_TASK_FAILED = define(
    "orchestration_task_failed", ERROR, required={"message_id"}
)
CLIENT_REQUEST_ID_RESOLUTION_FAILED = define(
    "client_request_id_resolution_failed", WARNING, required={"message_id"}
)
ORCHESTRATOR_LIFECYCLE_LISTENER_FAILED = define(
    "orchestrator_lifecycle_listener_failed", WARNING
)
RUN_CANCELLATION_RECONCILIATION_DEFERRED = define(
    "run_cancellation_reconciliation_deferred",
    WARNING,
    required={"run_id", "conversation_id"},
)
RUN_CANCELLATION_CLEANUP_FAILED = define(
    "run_cancellation_cleanup_failed",
    WARNING,
    required={"run_id", "conversation_id"},
)
HITL_WAKE_OBSERVATION_READ_FAILED = define(
    "hitl_wake_observation_read_failed", WARNING, required={"call_record_id"}
)
HITL_WAKE_OBSERVATION_PROCESSING_FAILED = define(
    "hitl_wake_observation_processing_failed",
    WARNING,
    required={"call_record_id", "observation_id"},
)
EARLIER_TURN_RUN_LOAD_FAILED = define(
    "earlier_turn_run_load_failed", WARNING, required={"turn_id"}
)
SUPERVISOR_ASK_USER_CREATION_FAILED = define(
    "supervisor_ask_user_creation_failed",
    ERROR,
    required={"run_id", "conversation_id", "call_id", "interaction_id"},
)
INTERRUPTED_RUN_SETTLEMENT_FAILED = define(
    "interrupted_run_settlement_failed", WARNING, required={"run_id"}
)
ORCHESTRATOR_PROJECTION_FAILED = define(
    "orchestrator_projection_failed",
    WARNING,
    required={"run_id", "conversation_id", "intent_id", "intent_kind"},
)
INVALID_RUN_DOCUMENT_SKIPPED = define(
    "invalid_run_document_skipped",
    ERROR,
    required={
        "document_kind",
        "run_id",
        "validation_fingerprint",
        "validation_errors",
    },
)
AGENT_CALL_COUNT_FAILED = define(
    "agent_call_count_failed", WARNING, required={"agent_id", "state"}
)
REMOTE_CANCELLATION_FAILED = define(
    "remote_cancellation_failed", WARNING, required={"call_record_id"}
)
HITL_ANSWER_RECONCILE_DEFERRED = define(
    "hitl_answer_reconcile_deferred", WARNING, required={"call_record_id"}
)
HITL_CONTINUATION_RECOVERY_DEFERRED = define(
    "hitl_continuation_recovery_deferred", WARNING, required={"call_record_id"}
)
HITL_CONTINUATION_CLAIM_REJECTED = define(
    "hitl_continuation_claim_rejected",
    WARNING,
    required={"call_record_id", "state_version", "current_state_version"},
)
RUN_RECOVERY_SCHEDULE_FAILED = define(
    "run_recovery_schedule_failed", ERROR, required={"run_id", "conversation_id"}
)
A2A_PUSH_PROCESSING_DEFERRED = define(
    "a2a_push_processing_deferred", WARNING, required={"observation_id"}
)
A2A_PUSH_INGRESS_FAILED = define(
    "a2a_push_ingress_failed", WARNING, required={"call_record_id"}
)
A2A_RECOVERY_PHASE_FAILED = define(
    "a2a_recovery_phase_failed", ERROR, required={"phase"}
)

# llm_gateway

PROVIDER_CONFIG_RELOADED = define(
    "provider_config_reloaded", INFO, required={"provider_state"}
)
PROVIDER_CONFIG_RELOAD_FAILED = define(
    "provider_config_reload_failed", WARNING, required={"reason"}
)
PROVIDER_CONFIG_REFRESH_FAILED = define("provider_config_refresh_failed", WARNING)
PROVIDER_LISTENER_FAILED = define("provider_listener_failed", WARNING)
OAUTH_CALLBACK_LISTENER_START_FAILED = define(
    "oauth_callback_listener_start_failed", INFO
)
OAUTH_CODE_EXCHANGE_FAILED = define("oauth_code_exchange_failed", WARNING)

# local_agents

LOCAL_AGENT_DISCOVERY_CYCLE_FAILED = define(
    "local_agent_discovery_cycle_failed", ERROR, required={"trigger"}
)
LOCAL_AGENT_RECORDS_PRUNED = define(
    "local_agent_records_pruned", INFO, required={"pruned_record_count"}
)

# session

SESSION_ORPHAN_CONTAINER_DELETE_FAILED = define(
    "session_orphan_container_delete_failed",
    WARNING,
    required={"session_id", "conversation_id"},
)
SESSION_MEMBER_ADD_FAILED = define(
    "session_member_add_failed", WARNING, required={"session_id", "agent_id"}
)
SESSION_CLOSE_PENDING_LOOKUP_FAILED = define(
    "session_close_pending_lookup_failed", WARNING, required={"conversation_id"}
)
SESSION_CLOSE_INTERACTION_CANCEL_FAILED = define(
    "session_close_interaction_cancel_failed",
    WARNING,
    required={"conversation_id", "interaction_id"},
)
SESSION_CLOSE_RUN_CANCEL_FAILED = define(
    "session_close_run_cancel_failed",
    WARNING,
    required={"conversation_id", "run_id"},
)

del define

export const CANONICAL_RUN_EVENT_TYPES = [
  'run_started',
  'turn_start',
  'message_start',
  'message_update',
  'message_end',
  'tool_execution_start',
  'tool_execution_update',
  'tool_execution_end',
  'turn_end',
  'retry_scheduled',
  'model_decision',
  'run_waiting_input',
  'run_resumed',
  'run_settled',
] as const

export type CanonicalRunEventType = (typeof CANONICAL_RUN_EVENT_TYPES)[number]
export type TurnLifecycleState = 'active' | 'awaiting_input' | 'completed' | 'failed' | 'canceled'
export type SafeSummary = Record<string, string | number | boolean | null>

export interface RunStartedPayload {
  hybro_turn_id: string
  user_message_id: string
  started_at: string
  mode: 'fast' | 'direct' | 'ultimate' | 'supervisor' | 'passthrough'
}

export interface TurnStartPayload {
  internal_turn_id: string
  attempt: number
}

export interface MessageStartPayload {
  internal_turn_id: string
  message_id: string
  role: 'assistant'
}

export interface TextDeltaEvent {
  type: 'text_delta'
  content_index: number
  delta_index: number
  start_offset: number
  end_offset: number
  delta: string
}

export interface MessageUpdatePayload {
  internal_turn_id: string
  message_id: string
  assistant_message_event: TextDeltaEvent
}

export type MessageEndPayload = {
  internal_turn_id: string
  message_id: string
  text: string
} & (
  | { stop_reason: 'tool_use'; disposition: 'commentary'; error_summary?: null }
  | { stop_reason: 'stop'; disposition: 'final'; error_summary?: null }
  | {
      stop_reason: 'length' | 'content_filter' | 'error' | 'deferred'
      disposition: 'error'
      error_summary: string
    }
  | { stop_reason: 'aborted'; disposition: 'aborted'; error_summary?: null }
)

export interface ToolExecutionStartPayload {
  internal_turn_id: string
  tool_call_id: string
  tool_name: string
  input: SafeSummary
  execution_kind?: 'agent' | 'tool'
  target?: { name: string; source?: 'cloud' | 'local' | 'hub' | null } | null
  request_summary?: string
}

export interface ToolExecutionUpdatePayload {
  internal_turn_id: string
  tool_call_id: string
  tool_name: string
  update_index: number
  status: 'running' | 'suspended'
  partial_result: string
  execution_kind?: 'agent' | 'tool'
  target?: { name: string; source?: 'cloud' | 'local' | 'hub' | null } | null
}

export type ToolExecutionEndPayload = {
  internal_turn_id: string
  tool_call_id: string
  tool_name: string
  result: string
  duration_ms: number
  execution_kind?: 'agent' | 'tool'
  target?: { name: string; source?: 'cloud' | 'local' | 'hub' | null } | null
  detail_available?: boolean
} & (
  | { outcome: 'completed'; is_error: false; failure_reason?: null }
  | {
      outcome: 'failed'
      is_error: true
      failure_reason?: 'rejected' | 'expired' | 'validation' | 'authorization' | 'execution' | null
      /** The failing Agent's own reason, when it reported one. */
      error_code?: string | null
      error_message?: string | null
    }
  | { outcome: 'canceled'; is_error: false; failure_reason?: null }
)

export interface TurnEndPayload {
  internal_turn_id: string
  message_id?: string | null
  tool_call_ids: string[]
  status: 'completed' | 'error' | 'aborted'
}

export interface RetryScheduledPayload {
  internal_turn_id: string
  attempt: number
  delay_ms: number
  error_class:
    | 'provider_timeout'
    | 'provider_error'
    | 'content_filter'
    | 'assembly_error'
    | 'tool_failure'
    | 'process_restart'
}

export type ModelDecisionPayload = {
  internal_turn_id: string
  decision: 'interaction_received' | 'answered_from_context' | 'forwarded_to_user' | 'no_progress' | 'degraded_to_user'
  agent_label?: string | null
  question_summary?: string | null
  source_summary?: string | null
  reason?: string | null
}

export interface RunWaitingInputPayload {
  interaction_id: string
  request_ids: string[]
  requested_at: string
}

export interface RunResumedPayload {
  interaction_id: string
  resolved_request_ids: string[]
  resumed_at: string
}

export type RunSettledPayload = {
  started_at: string
  settled_at: string
  duration_ms: number
} & (
  | {
      status: 'completed'
      final_message_id: string
      failure_code?: null
      error_summary?: null
      cancellation_code?: null
      rule_id?: null
    }
  | {
      status: 'failed'
      final_message_id?: null
      failure_code:
        | 'budget_exhausted'
        | 'provider_error'
        | 'assembly_error'
        | 'tool_failure'
        | 'hitl_error'
        | 'rejected'
        | 'internal_error'
      error_summary: string
      cancellation_code?: null
      rule_id?: null
    }
  | {
      status: 'canceled'
      final_message_id?: null
      failure_code?: null
      error_summary?: null
      cancellation_code: 'user_requested' | 'room_closed' | 'shutdown' | 'policy'
      /** The check rule that denied the call; only with `policy`. */
      rule_id?: string | null
    }
)

export interface CanonicalPayloadMap {
  run_started: RunStartedPayload
  turn_start: TurnStartPayload
  message_start: MessageStartPayload
  message_update: MessageUpdatePayload
  message_end: MessageEndPayload
  tool_execution_start: ToolExecutionStartPayload
  tool_execution_update: ToolExecutionUpdatePayload
  tool_execution_end: ToolExecutionEndPayload
  turn_end: TurnEndPayload
  retry_scheduled: RetryScheduledPayload
  model_decision: ModelDecisionPayload
  run_waiting_input: RunWaitingInputPayload
  run_resumed: RunResumedPayload
  run_settled: RunSettledPayload
}

export type CanonicalRunEventData = {
  [K in CanonicalRunEventType]: {
    room_seq: number
    room_event_id?: string
    parent_event_id?: string
    delivery_id?: string
    trace_id?: string
    event_id: string
    run_id: string
    seq: number
    type: K
    payload: CanonicalPayloadMap[K]
    correlation_id: string
  }
}[CanonicalRunEventType]

export interface AssistantProjection {
  messageId: string
  internalTurnId: string
  text: string
  status: 'streaming' | 'completed' | 'error' | 'aborted'
  contentIndex: number
  nextDeltaIndex: number
  endOffset: number
  order: number
}

export interface InternalTurnProjection {
  internalTurnId: string
  attempt: number
  messageIds: string[]
  toolCallIds: string[]
  status: 'active' | 'completed' | 'error' | 'aborted'
}

export type TurnActivityItem =
  | {
      kind: 'assistant'
      id: string
      internalTurnId: string
      text: string
      status: 'completed' | 'error' | 'aborted'
      order: number
    }
  | {
      kind: 'tool'
      id: string
      internalTurnId: string
      toolCallId: string
      label: string
      input: SafeSummary
      partialResult: string
      result?: string
      isError?: boolean
      durationMs?: number
      failureReason?: string
      updateIndex: number
      status: 'running' | 'suspended' | 'completed' | 'failed' | 'canceled'
      executionKind: 'agent' | 'tool'
      targetName?: string
      requestSummary: string
      detailAvailable: boolean
      errorCode?: string
      errorMessage?: string
      order: number
    }
  | {
      kind: 'retry'
      id: string
      internalTurnId: string
      attempt: number
      delayMs: number
      errorClass: string
      order: number
    }
  | {
      kind: 'decision'
      id: string
      internalTurnId: string
      decision: 'interaction_received' | 'answered_from_context' | 'forwarded_to_user' | 'no_progress' | 'degraded_to_user'
      agentLabel?: string
      questionSummary?: string
      sourceSummary?: string
      reason?: string
      order: number
    }

export interface HITLQuestionProjection {
  requestId: string
  messageId: string
  questionIndex: number
  questionCount: number
  prompt: string
  promptType: string
  choices: string[]
  source: string
  agentLabel?: string
  status: 'requested' | 'responded' | 'expired' | 'canceled' | 'error'
  answerRef?: string
}

export interface HITLInteractionProjection {
  interactionId: string
  state: 'awaiting_input' | 'resumed' | 'expired' | 'canceled' | 'error'
  requestIds: string[]
  requests: HITLQuestionProjection[]
  requestedAt: string
  resumedAt?: string
}

export interface TurnProjection {
  id: string
  runId: string
  conversationId: string
  userMessageId: string
  clientRequestId: string
  state: TurnLifecycleState
  startedAt: string
  settledAt?: string
  durationMs?: number
  terminalCode?: string
  terminalSummary?: string
  internalTurns: InternalTurnProjection[]
  activity: TurnActivityItem[]
  currentAssistant?: AssistantProjection
  finalAnswer?: AssistantProjection
  finalCommitted: boolean
  hitlInteractions: HITLInteractionProjection[]
  activeInteractionId?: string
  agentCallMessageIds: string[]
}

export interface RoomSnapshotAssistant {
  message_id: string
  internal_turn_id: string
  text: string
  status: 'streaming' | 'completed' | 'error' | 'aborted'
  order: number
  content_index?: number
  next_delta_index?: number
  end_offset?: number
}

export type RoomSnapshotActivityItem =
  | {
      kind: 'assistant'
      message_id: string
      internal_turn_id: string
      text: string
      status: 'completed' | 'error' | 'aborted'
      order: number
    }
  | {
      kind: 'tool'
      id: string
      internal_turn_id: string
      tool_call_id: string
      label: string
      input: SafeSummary
      partial_result: string
      result: string | null
      is_error: boolean | null
      duration_ms: number | null
      failure_reason?: 'rejected' | 'expired' | 'validation' | 'authorization' | 'execution' | null
      status: 'running' | 'suspended' | 'completed' | 'failed' | 'canceled'
      update_index: number
      execution_kind?: 'agent' | 'tool'
      target_name?: string | null
      request_summary?: string
      detail_available?: boolean
      error_code?: string | null
      error_message?: string | null
      order: number
    }
  | {
      kind: 'retry'
      id: string
      internal_turn_id: string
      attempt: number
      delay_ms: number
      error_class: string
      order: number
    }
  | {
      kind: 'decision'
      id: string
      internal_turn_id: string
      decision: 'interaction_received' | 'answered_from_context' | 'forwarded_to_user' | 'no_progress' | 'degraded_to_user'
      agent_label?: string | null
      question_summary?: string | null
      source_summary?: string | null
      reason?: string | null
      order: number
    }

export interface RoomSnapshotTurn {
  hybro_turn_id: string
  run_id: string
  user_message_id: string
  client_request_id: string
  state: TurnLifecycleState
  started_at: string
  settled_at: string | null
  duration_ms: number | null
  terminal_code: string | null
  terminal_summary: string | null
  internal_turns: Array<{
    internal_turn_id: string
    attempt: number
    message_ids: string[]
    tool_call_ids: string[]
    status: 'active' | 'completed' | 'error' | 'aborted'
  }>
  activity: RoomSnapshotActivityItem[]
  current_assistant: RoomSnapshotAssistant | null
  final_answer: RoomSnapshotAssistant | null
  final_committed: boolean
  hitl_interactions: Array<{
    interaction_id: string
    state: 'awaiting_input' | 'resumed' | 'expired' | 'canceled' | 'error'
    request_ids: string[]
    requests: Array<{
      request_id: string
      message_id: string
      question_index: number
      question_count: number
      prompt: string
      prompt_type: string
      choices: string[]
      source: string
      agent_label: string | null
      status: 'requested' | 'responded' | 'expired' | 'canceled' | 'error'
      answer_ref: string | null
    }>
    requested_at: string
    resumed_at: string | null
  }>
  active_interaction_id: string | null
  agent_call_message_ids: string[]
}

export interface CanonicalHITLRequestData {
  room_seq: number
  room_event_id?: string
  parent_event_id?: string
  delivery_id?: string
  trace_id?: string
  run_id: string
  request_id: string
  message_id: string
  interaction_id: string
  related_user_message_id: string
  client_request_id: string
  question_index: number
  question_count: number
  prompt: string
  prompt_type: string
  choices?: string[] | null
  source: 'agent' | 'supervisor' | 'system'
  agent_label?: string | null
  header?: string | null
  required?: boolean
  allow_other?: boolean
  choice_options?: Array<{ value: string; label?: string | null; description?: string | null }> | null
}

export interface CanonicalHITLResponseData {
  room_seq: number
  room_event_id?: string
  parent_event_id?: string
  delivery_id?: string
  trace_id?: string
  run_id: string
  request_id: string
  message_id: string
  interaction_id: string
  related_user_message_id: string
  client_request_id: string
  question_index: number
  question_count: number
  source: string
  status: 'responded' | 'expired' | 'canceled' | 'error'
  answer_ref?: string | null
  answer_text?: string | null
}

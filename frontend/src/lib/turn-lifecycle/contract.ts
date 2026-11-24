import {
  CANONICAL_RUN_EVENT_TYPES,
  type CanonicalHITLRequestData,
  type CanonicalHITLResponseData,
  type CanonicalRunEventData,
  type CanonicalRunEventType,
  type RoomSnapshotActivityItem,
  type RoomSnapshotAssistant,
  type RoomSnapshotTurn,
  type SafeSummary,
} from './types'

const CANONICAL_TYPE_SET = new Set<string>(CANONICAL_RUN_EVENT_TYPES)
const TOOL_CALL_ID = /^inv_[A-Za-z0-9_-]{8,128}$/
const META_KEYS = ['room_seq', 'room_event_id', 'parent_event_id', 'delivery_id', 'trace_id'] as const

function record(value: unknown): value is Record<string, unknown> {
  return Boolean(value && typeof value === 'object' && !Array.isArray(value))
}

function exactKeys(
  value: Record<string, unknown>,
  required: readonly string[],
  optional: readonly string[] = [],
): boolean {
  const allowed = new Set([...required, ...optional])
  return required.every((key) => Object.prototype.hasOwnProperty.call(value, key))
    && Object.keys(value).every((key) => allowed.has(key))
}

function text(value: unknown, max = 32_000): value is string {
  return typeof value === 'string' && value.length <= max
}

function id(value: unknown): value is string {
  return typeof value === 'string' && value.length > 0 && value.length <= 256
}

function timestamp(value: unknown): value is string {
  return typeof value === 'string' && value.length > 0 && Number.isFinite(Date.parse(value))
}

function integer(value: unknown, min = 0): value is number {
  return typeof value === 'number' && Number.isInteger(value) && value >= min
}

function oneOf<T extends string>(value: unknown, values: readonly T[]): value is T {
  return typeof value === 'string' && (values as readonly string[]).includes(value)
}

function uniqueIds(value: unknown, minLength = 0): value is string[] {
  return Array.isArray(value)
    && value.length >= minLength
    && value.every(id)
    && new Set(value).size === value.length
}

function safeSummary(value: unknown): value is SafeSummary {
  return record(value) && Object.values(value).every((item) => (
    item === null || ['string', 'number', 'boolean'].includes(typeof item)
  ))
}

function executionTarget(value: unknown): boolean {
  if (!record(value)) return false
  return exactKeys(value, ['name'], ['source'])
    && typeof value.name === 'string'
    && value.name.length > 0
    && value.name.length <= 160
    && (value.source === undefined || value.source === null || oneOf(value.source, ['cloud', 'local', 'hub']))
}

function validExecutionIdentity(value: Record<string, unknown>): boolean {
  if (value.execution_kind === undefined) return true
  if (!oneOf(value.execution_kind, ['agent', 'tool'])) return false
  if (value.execution_kind === 'agent') return executionTarget(value.target)
  return value.target === undefined || value.target === null
}

const AGENT_ERROR_CODE = /^[A-Za-z][A-Za-z0-9_.:-]{0,63}$/

/** An Agent's own failure reason may appear only on a failed execution. */
function validAgentError(value: Record<string, unknown>, failed: boolean): boolean {
  const absent = (field: unknown) => field === undefined || field === null
  if (absent(value.error_code) && absent(value.error_message)) return true
  return failed
    && typeof value.error_code === 'string'
    && AGENT_ERROR_CODE.test(value.error_code)
    && (absent(value.error_message) || text(value.error_message, 500))
}

function validatePayload(type: CanonicalRunEventType, value: unknown): boolean {
  if (!record(value)) return false
  switch (type) {
    case 'run_started':
      return exactKeys(value, ['hybro_turn_id', 'user_message_id', 'started_at', 'mode'])
        && id(value.hybro_turn_id)
        && id(value.user_message_id)
        && timestamp(value.started_at)
        && oneOf(value.mode, ['fast', 'direct', 'ultimate', 'supervisor', 'passthrough'])
    case 'turn_start':
      return exactKeys(value, ['internal_turn_id', 'attempt'])
        && id(value.internal_turn_id)
        && integer(value.attempt, 1)
    case 'message_start':
      return exactKeys(value, ['internal_turn_id', 'message_id', 'role'])
        && id(value.internal_turn_id)
        && id(value.message_id)
        && value.role === 'assistant'
    case 'message_update': {
      if (!exactKeys(value, ['internal_turn_id', 'message_id', 'assistant_message_event'])
        || !id(value.internal_turn_id)
        || !id(value.message_id)
        || !record(value.assistant_message_event)) return false
      const delta = value.assistant_message_event
      return exactKeys(delta, ['type', 'content_index', 'delta_index', 'start_offset', 'end_offset', 'delta'])
        && delta.type === 'text_delta'
        && integer(delta.content_index)
        && integer(delta.delta_index)
        && integer(delta.start_offset)
        && integer(delta.end_offset)
        && text(delta.delta)
        && delta.end_offset === delta.start_offset + Array.from(delta.delta).length
    }
    case 'message_end': {
      if (!exactKeys(value, ['internal_turn_id', 'message_id', 'stop_reason', 'disposition', 'text'], ['error_summary'])
        || !id(value.internal_turn_id)
        || !id(value.message_id)
        || !text(value.text)) return false
      const combinations: Record<string, readonly string[]> = {
        commentary: ['tool_use'],
        final: ['stop'],
        error: ['length', 'content_filter', 'error', 'deferred'],
        aborted: ['aborted'],
      }
      if (typeof value.disposition !== 'string'
        || !combinations[value.disposition]?.includes(String(value.stop_reason))) return false
      return value.disposition === 'error'
        ? text(value.error_summary, 1_000) && value.error_summary.length > 0
        : value.error_summary === undefined || value.error_summary === null
    }
    case 'tool_execution_start':
      return exactKeys(value, ['internal_turn_id', 'tool_call_id', 'tool_name', 'input'], ['execution_kind', 'target', 'request_summary'])
        && id(value.internal_turn_id)
        && typeof value.tool_call_id === 'string'
        && TOOL_CALL_ID.test(value.tool_call_id)
        && typeof value.tool_name === 'string'
        && value.tool_name.length > 0
        && value.tool_name.length <= 160
        && safeSummary(value.input)
        && (value.request_summary === undefined || text(value.request_summary, 1_000))
        && validExecutionIdentity(value)
    case 'tool_execution_update':
      return exactKeys(value, ['internal_turn_id', 'tool_call_id', 'tool_name', 'update_index', 'status', 'partial_result'], ['execution_kind', 'target'])
        && id(value.internal_turn_id)
        && typeof value.tool_call_id === 'string'
        && TOOL_CALL_ID.test(value.tool_call_id)
        && typeof value.tool_name === 'string'
        && value.tool_name.length > 0
        && value.tool_name.length <= 160
        && integer(value.update_index, 1)
        && oneOf(value.status, ['running', 'suspended'])
        && text(value.partial_result, 1_000)
        && validExecutionIdentity(value)
    case 'tool_execution_end': {
      if (!exactKeys(value, ['internal_turn_id', 'tool_call_id', 'tool_name', 'outcome', 'result', 'is_error', 'duration_ms'], ['failure_reason', 'execution_kind', 'target', 'detail_available', 'error_code', 'error_message'])
        || !id(value.internal_turn_id)
        || typeof value.tool_call_id !== 'string'
        || !TOOL_CALL_ID.test(value.tool_call_id)
        || typeof value.tool_name !== 'string'
        || value.tool_name.length === 0
        || value.tool_name.length > 160
        || !text(value.result, 1_000)
        || !integer(value.duration_ms)
        || !oneOf(value.outcome, ['completed', 'failed', 'canceled'])
        || typeof value.is_error !== 'boolean'
        || (value.detail_available !== undefined && typeof value.detail_available !== 'boolean')
        || !validAgentError(value, value.outcome === 'failed')
        || !validExecutionIdentity(value)) return false
      if (value.outcome === 'completed') {
        return value.is_error === false && (value.failure_reason === undefined || value.failure_reason === null)
      }
      if (value.outcome === 'canceled') {
        return value.is_error === false && value.result === ''
          && (value.failure_reason === undefined || value.failure_reason === null)
      }
      return value.is_error === true
        && (value.failure_reason === undefined || value.failure_reason === null
          || oneOf(value.failure_reason, ['rejected', 'expired', 'validation', 'authorization', 'execution']))
    }
    case 'turn_end':
      return exactKeys(value, ['internal_turn_id', 'tool_call_ids', 'status'], ['message_id'])
        && id(value.internal_turn_id)
        && uniqueIds(value.tool_call_ids)
        && oneOf(value.status, ['completed', 'error', 'aborted'])
        && (value.message_id === undefined || value.message_id === null || id(value.message_id))
        && (value.status !== 'completed' || id(value.message_id))
    case 'retry_scheduled':
      return exactKeys(value, ['internal_turn_id', 'attempt', 'delay_ms', 'error_class'])
        && id(value.internal_turn_id)
        && integer(value.attempt, 2)
        && integer(value.delay_ms)
        && oneOf(value.error_class, ['provider_timeout', 'provider_error', 'content_filter', 'assembly_error', 'tool_failure', 'process_restart'])
    case 'model_decision':
      return exactKeys(value, ['internal_turn_id', 'decision'], ['agent_label', 'question_summary', 'source_summary', 'reason'])
        && id(value.internal_turn_id)
        && oneOf(value.decision, ['interaction_received', 'answered_from_context', 'forwarded_to_user', 'no_progress', 'degraded_to_user'])
        && (value.agent_label === undefined || value.agent_label === null || (typeof value.agent_label === 'string' && value.agent_label.length <= 160))
        && (value.question_summary === undefined || value.question_summary === null || text(value.question_summary, 1_000))
        && (value.source_summary === undefined || value.source_summary === null || text(value.source_summary, 1_000))
        && (value.reason === undefined || value.reason === null || text(value.reason, 1_000))
        && (!['answered_from_context', 'forwarded_to_user'].includes(value.decision) || !!value.agent_label)
        && (!['no_progress', 'degraded_to_user'].includes(value.decision) || !!value.reason)
    case 'run_waiting_input':
      return exactKeys(value, ['interaction_id', 'request_ids', 'requested_at'])
        && id(value.interaction_id)
        && uniqueIds(value.request_ids, 1)
        && timestamp(value.requested_at)
    case 'run_resumed':
      return exactKeys(value, ['interaction_id', 'resolved_request_ids', 'resumed_at'])
        && id(value.interaction_id)
        && uniqueIds(value.resolved_request_ids, 1)
        && timestamp(value.resumed_at)
    case 'run_settled': {
      if (!exactKeys(value, ['status', 'started_at', 'settled_at', 'duration_ms'], ['final_message_id', 'failure_code', 'error_summary', 'cancellation_code', 'rule_id'])
        || !oneOf(value.status, ['completed', 'failed', 'canceled'])
        || !timestamp(value.started_at)
        || !timestamp(value.settled_at)
        || Date.parse(value.settled_at) < Date.parse(value.started_at)
        || !integer(value.duration_ms)) return false
      if (value.status === 'completed') {
        return id(value.final_message_id)
          && (value.failure_code === undefined || value.failure_code === null)
          && (value.error_summary === undefined || value.error_summary === null)
          && (value.cancellation_code === undefined || value.cancellation_code === null)
          && (value.rule_id === undefined || value.rule_id === null)
      }
      if (value.status === 'failed') {
        return oneOf(value.failure_code, ['budget_exhausted', 'provider_error', 'assembly_error', 'tool_failure', 'hitl_error', 'rejected', 'internal_error'])
          && text(value.error_summary, 1_000)
          && value.error_summary.length > 0
          && (value.final_message_id === undefined || value.final_message_id === null)
          && (value.cancellation_code === undefined || value.cancellation_code === null)
          && (value.rule_id === undefined || value.rule_id === null)
      }
      return oneOf(value.cancellation_code, ['user_requested', 'room_closed', 'shutdown', 'policy', 'owner_refused'])
        && (value.rule_id === undefined || value.rule_id === null
          || (value.cancellation_code === 'policy' && id(value.rule_id)))
        && (value.final_message_id === undefined || value.final_message_id === null)
        && (value.failure_code === undefined || value.failure_code === null)
        && (value.error_summary === undefined || value.error_summary === null)
    }
  }
}

export function isCanonicalRunEventType(value: unknown): value is CanonicalRunEventType {
  return typeof value === 'string' && CANONICAL_TYPE_SET.has(value)
}

export type CanonicalRunEventValidation =
  | { canonical: false }
  | { canonical: true; valid: false; reason: string }
  | { canonical: true; valid: true; data: CanonicalRunEventData }

export function validateCanonicalRunEventData(value: unknown): CanonicalRunEventValidation {
  if (!record(value) || !isCanonicalRunEventType(value.type)) return { canonical: false }
  if (!exactKeys(value, ['event_id', 'run_id', 'seq', 'type', 'payload', 'correlation_id', 'room_seq'], ['room_event_id', 'parent_event_id', 'delivery_id', 'trace_id'])
    || !id(value.event_id)
    || !id(value.run_id)
    || !integer(value.seq)
    || !id(value.correlation_id)
    || !integer(value.room_seq)
    || (value.room_event_id !== undefined && !id(value.room_event_id))
    || (value.parent_event_id !== undefined && !id(value.parent_event_id))
    || (value.delivery_id !== undefined && !id(value.delivery_id))
    || (value.trace_id !== undefined && !id(value.trace_id))
    || !validatePayload(value.type, value.payload)) {
    return { canonical: true, valid: false, reason: `Malformed canonical ${value.type} event` }
  }
  if (value.type === 'run_started'
    && (value.payload as Record<string, unknown>).hybro_turn_id !== value.run_id) {
    return { canonical: true, valid: false, reason: 'run_started root identity does not match run_id' }
  }
  return { canonical: true, valid: true, data: value as CanonicalRunEventData }
}

// The backend translator always sends the presentation fields (required,
// allow_other) and the sanitized owner answer; they are public UI data.
const REQUEST_OPTIONAL = [
  ...META_KEYS,
  'choices',
  'agent_label',
  'header',
  'required',
  'allow_other',
  'choice_options',
] as const
const RESPONSE_OPTIONAL = [
  ...META_KEYS,
  'answer_ref',
  'answer_text',
  'answered_by_client_id',
] as const

function choiceOption(value: unknown): boolean {
  return record(value)
    && exactKeys(value, ['value'], ['label', 'description'])
    && text(value.value, 500)
    && (value.label === undefined || value.label === null || text(value.label, 200))
    && (value.description === undefined || value.description === null || text(value.description, 500))
}

export function isCanonicalHITLRequestData(value: unknown): value is CanonicalHITLRequestData {
  if (!record(value) || !Object.prototype.hasOwnProperty.call(value, 'run_id')) return false
  return exactKeys(value, [
    'run_id', 'request_id', 'message_id', 'interaction_id', 'related_user_message_id',
    'client_request_id', 'question_index', 'question_count', 'prompt', 'prompt_type', 'source', 'room_seq',
  ], REQUEST_OPTIONAL)
    && integer(value.room_seq)
    && id(value.run_id)
    && id(value.request_id)
    && id(value.message_id)
    && id(value.interaction_id)
    && id(value.related_user_message_id)
    && id(value.client_request_id)
    && integer(value.question_index)
    && integer(value.question_count, 1)
    && value.question_index < value.question_count
    && text(value.prompt, 4_000)
    && oneOf(value.prompt_type, ['text', 'textarea', 'choice', 'single_choice', 'multi_choice', 'confirmation', 'approval', 'authentication', 'date'])
    && oneOf(value.source, ['agent', 'supervisor', 'system'])
    && (value.choices === undefined || value.choices === null || (Array.isArray(value.choices) && value.choices.every((choice) => text(choice, 1_000))))
    && (value.agent_label === undefined || value.agent_label === null || text(value.agent_label, 160))
    && (value.header === undefined || value.header === null || text(value.header, 40))
    && (value.required === undefined || typeof value.required === 'boolean')
    && (value.allow_other === undefined || typeof value.allow_other === 'boolean')
    && (value.choice_options === undefined || value.choice_options === null
      || (Array.isArray(value.choice_options) && value.choice_options.length <= 20
        && value.choice_options.every(choiceOption)))
    && (value.room_event_id === undefined || id(value.room_event_id))
    && (value.parent_event_id === undefined || id(value.parent_event_id))
    && (value.delivery_id === undefined || id(value.delivery_id))
    && (value.trace_id === undefined || id(value.trace_id))
}

export function isCanonicalHITLResponseData(value: unknown): value is CanonicalHITLResponseData {
  if (!record(value) || !Object.prototype.hasOwnProperty.call(value, 'run_id')) return false
  return exactKeys(value, [
    'run_id', 'request_id', 'message_id', 'interaction_id', 'related_user_message_id',
    'client_request_id', 'question_index', 'question_count', 'source', 'status', 'room_seq',
  ], RESPONSE_OPTIONAL)
    && integer(value.room_seq)
    && id(value.run_id)
    && id(value.request_id)
    && id(value.message_id)
    && id(value.interaction_id)
    && id(value.related_user_message_id)
    && id(value.client_request_id)
    && integer(value.question_index)
    && integer(value.question_count, 1)
    && value.question_index < value.question_count
    && typeof value.source === 'string'
    && value.source.length > 0
    && oneOf(value.status, ['responded', 'expired', 'canceled', 'error'])
    && (value.answer_ref === undefined || value.answer_ref === null || id(value.answer_ref))
    && (value.answer_text === undefined || value.answer_text === null || text(value.answer_text, 1_000))
    && (value.answered_by_client_id === undefined || value.answered_by_client_id === null || id(value.answered_by_client_id))
    && (value.room_event_id === undefined || id(value.room_event_id))
    && (value.parent_event_id === undefined || id(value.parent_event_id))
    && (value.delivery_id === undefined || id(value.delivery_id))
    && (value.trace_id === undefined || id(value.trace_id))
}

function snapshotAssistant(value: unknown): value is RoomSnapshotAssistant {
  if (!record(value)) return false
  return exactKeys(value, ['message_id', 'internal_turn_id', 'text', 'status', 'order'], ['content_index', 'next_delta_index', 'end_offset'])
    && id(value.message_id)
    && id(value.internal_turn_id)
    && text(value.text)
    && oneOf(value.status, ['streaming', 'completed', 'error', 'aborted'])
    && integer(value.order)
    && (value.content_index === undefined || integer(value.content_index))
    && (value.next_delta_index === undefined || integer(value.next_delta_index))
    && (value.end_offset === undefined || integer(value.end_offset))
}

function snapshotActivity(value: unknown): value is RoomSnapshotActivityItem {
  if (!record(value) || typeof value.kind !== 'string') return false
  if (value.kind === 'assistant') {
    return exactKeys(value, ['kind', 'message_id', 'internal_turn_id', 'text', 'status', 'order'])
      && id(value.message_id)
      && id(value.internal_turn_id)
      && text(value.text)
      && oneOf(value.status, ['completed', 'error', 'aborted'])
      && integer(value.order)
  }
  if (value.kind === 'tool') {
    if (!exactKeys(value, ['kind', 'id', 'internal_turn_id', 'tool_call_id', 'label', 'input', 'partial_result', 'result', 'is_error', 'duration_ms', 'status', 'update_index', 'order'], ['failure_reason', 'execution_kind', 'target_name', 'request_summary', 'detail_available', 'error_code', 'error_message'])
      || !id(value.id)
      || !id(value.internal_turn_id)
      || typeof value.tool_call_id !== 'string'
      || !TOOL_CALL_ID.test(value.tool_call_id)
      || !text(value.label, 160)
      || value.label.length === 0
      || !safeSummary(value.input)
      || !text(value.partial_result, 1_000)
      || !oneOf(value.status, ['running', 'suspended', 'completed', 'failed', 'canceled'])
      || !integer(value.update_index)
      || !integer(value.order)
      || (value.execution_kind !== undefined && !oneOf(value.execution_kind, ['agent', 'tool']))
      || (value.target_name !== undefined && value.target_name !== null && !text(value.target_name, 160))
      || (value.request_summary !== undefined && !text(value.request_summary, 1_000))
      || (value.detail_available !== undefined && typeof value.detail_available !== 'boolean')
      || !validAgentError(value, value.status === 'failed')) return false
    const failureReasonValid = value.failure_reason === undefined
      || value.failure_reason === null
      || oneOf(value.failure_reason, ['rejected', 'expired', 'validation', 'authorization', 'execution'])
    if (!failureReasonValid) return false
    if (value.status === 'running' || value.status === 'suspended') {
      return value.result === null && value.is_error === null
        && value.duration_ms === null && value.failure_reason === undefined
    }
    if (!text(value.result, 1_000) || !integer(value.duration_ms) || value.duration_ms < 0) return false
    if (value.status === 'completed') {
      return value.is_error === false
        && (value.failure_reason === undefined || value.failure_reason === null)
    }
    if (value.status === 'failed') return value.is_error === true
    return value.result.length === 0 && value.is_error === false
      && (value.failure_reason === undefined || value.failure_reason === null)
  }
  if (value.kind === 'retry') {
    return exactKeys(value, ['kind', 'id', 'internal_turn_id', 'attempt', 'delay_ms', 'error_class', 'order'])
      && id(value.id)
      && id(value.internal_turn_id)
      && integer(value.attempt, 2)
      && integer(value.delay_ms)
      && typeof value.error_class === 'string'
      && value.error_class.length > 0
      && integer(value.order)
  }
  if (value.kind === 'decision') {
    return exactKeys(value, ['kind', 'id', 'internal_turn_id', 'decision', 'order'], ['agent_label', 'question_summary', 'source_summary', 'reason'])
      && id(value.id)
      && id(value.internal_turn_id)
      && oneOf(value.decision, ['interaction_received', 'answered_from_context', 'forwarded_to_user', 'no_progress', 'degraded_to_user'])
      && (value.agent_label === undefined || value.agent_label === null || text(value.agent_label, 160))
      && (value.question_summary === undefined || value.question_summary === null || text(value.question_summary, 1_000))
      && (value.source_summary === undefined || value.source_summary === null || text(value.source_summary, 1_000))
      && (value.reason === undefined || value.reason === null || text(value.reason, 1_000))
      && (!['answered_from_context', 'forwarded_to_user'].includes(value.decision) || !!value.agent_label)
      && (!['no_progress', 'degraded_to_user'].includes(value.decision) || !!value.reason)
      && integer(value.order)
  }
  return false
}

export function isRoomSnapshotTurn(value: unknown): value is RoomSnapshotTurn {
  if (!record(value) || !exactKeys(value, [
    'hybro_turn_id', 'run_id', 'user_message_id', 'client_request_id', 'state', 'started_at',
    'settled_at', 'duration_ms', 'terminal_code', 'terminal_summary', 'internal_turns',
    'activity', 'current_assistant', 'final_answer', 'final_committed', 'hitl_interactions',
    'active_interaction_id', 'agent_call_message_ids',
  ])) return false
  if (!id(value.hybro_turn_id)
    || value.run_id !== value.hybro_turn_id
    || !id(value.user_message_id)
    || !id(value.client_request_id)
    || !oneOf(value.state, ['active', 'awaiting_input', 'completed', 'failed', 'canceled'])
    || !timestamp(value.started_at)
    || (value.settled_at !== null && !timestamp(value.settled_at))
    || (value.duration_ms !== null && !integer(value.duration_ms))
    || (value.terminal_code !== null && typeof value.terminal_code !== 'string')
    || (value.terminal_summary !== null && !text(value.terminal_summary, 1_000))
    || !Array.isArray(value.internal_turns)
    || !Array.isArray(value.activity)
    || (value.current_assistant !== null && !snapshotAssistant(value.current_assistant))
    || (value.final_answer !== null && !snapshotAssistant(value.final_answer))
    || typeof value.final_committed !== 'boolean'
    || !Array.isArray(value.hitl_interactions)
    || (value.active_interaction_id !== null && !id(value.active_interaction_id))
    || !uniqueIds(value.agent_call_message_ids)
    || !value.agent_call_message_ids.every((messageId) => {
      const prefix = `orchestrator:${value.run_id}:`
      return messageId.startsWith(prefix) && TOOL_CALL_ID.test(messageId.slice(prefix.length))
    })) return false
  if (!value.internal_turns.every((item) => record(item)
    && exactKeys(item, ['internal_turn_id', 'attempt', 'message_ids', 'tool_call_ids', 'status'])
    && id(item.internal_turn_id)
    && integer(item.attempt, 1)
    && uniqueIds(item.message_ids)
    && uniqueIds(item.tool_call_ids)
    && oneOf(item.status, ['active', 'completed', 'error', 'aborted']))) return false
  if (!value.activity.every(snapshotActivity)) return false

  const internalTurns = value.internal_turns as RoomSnapshotTurn['internal_turns']
  const activity = value.activity as RoomSnapshotActivityItem[]
  const internalIds = internalTurns.map((item) => item.internal_turn_id)
  if (new Set(internalIds).size !== internalIds.length) return false
  const owners = new Map(internalTurns.map((item) => [item.internal_turn_id, item]))
  for (const item of activity) {
    const owner = owners.get(item.internal_turn_id)
    if (!owner) return false
    if (item.kind === 'assistant' && !owner.message_ids.includes(item.message_id)) return false
    if (item.kind === 'tool' && !owner.tool_call_ids.includes(item.tool_call_id)) return false
    if (item.kind === 'retry' && !['error', 'aborted'].includes(owner.status)) return false
  }
  const currentAssistant = value.current_assistant as RoomSnapshotAssistant | null
  if (currentAssistant) {
    const owner = owners.get(currentAssistant.internal_turn_id)
    if (currentAssistant.status !== 'streaming'
      || !owner
      || owner.status !== 'active'
      || !owner.message_ids.includes(currentAssistant.message_id)) return false
  }
  const finalAnswer = value.final_answer as RoomSnapshotAssistant | null
  if (finalAnswer) {
    const owner = owners.get(finalAnswer.internal_turn_id)
    if (finalAnswer.status !== 'completed'
      || !owner
      || !owner.message_ids.includes(finalAnswer.message_id)) return false
  }

  const startedAt = Date.parse(value.started_at)
  const settledAt = value.settled_at === null ? null : Date.parse(value.settled_at)
  const failureCodes = new Set([
    'budget_exhausted', 'provider_error', 'assembly_error', 'tool_failure',
    'hitl_error', 'rejected', 'internal_error',
  ])
  const cancellationCodes = new Set(['user_requested', 'room_closed', 'shutdown', 'policy', 'owner_refused'])
  if (value.state === 'active' || value.state === 'awaiting_input') {
    if (value.settled_at !== null || value.duration_ms !== null
      || value.terminal_code !== null || value.terminal_summary !== null) return false
  } else {
    if (settledAt === null || settledAt < startedAt || value.duration_ms === null) return false
    if (value.state === 'completed') {
      if (!value.final_committed || value.terminal_code !== null || value.terminal_summary !== null) return false
    } else if (value.state === 'failed') {
      if (value.terminal_code === null || !failureCodes.has(value.terminal_code)
        || value.terminal_summary === null) return false
    } else if (value.terminal_code === null
      || !cancellationCodes.has(value.terminal_code)
      || value.terminal_summary !== null) return false
  }

  return value.hitl_interactions.every((interaction) => record(interaction)
    && exactKeys(interaction, ['interaction_id', 'state', 'request_ids', 'requests', 'requested_at', 'resumed_at'])
    && id(interaction.interaction_id)
    && oneOf(interaction.state, ['awaiting_input', 'resumed', 'expired', 'canceled', 'error'])
    && uniqueIds(interaction.request_ids, 1)
    && Array.isArray(interaction.requests)
    && interaction.requests.every((request) => record(request)
      && exactKeys(request, ['request_id', 'message_id', 'question_index', 'question_count', 'prompt', 'prompt_type', 'choices', 'source', 'agent_label', 'status', 'answer_ref'])
      && id(request.request_id)
      && id(request.message_id)
      && integer(request.question_index)
      && integer(request.question_count, 1)
      && text(request.prompt, 4_000)
      && typeof request.prompt_type === 'string'
      && Array.isArray(request.choices)
      && request.choices.every((choice) => text(choice, 1_000))
      && typeof request.source === 'string'
      && (request.agent_label === null || text(request.agent_label, 160))
      && oneOf(request.status, ['requested', 'responded', 'expired', 'canceled', 'error'])
      && (request.answer_ref === null || id(request.answer_ref)))
    && timestamp(interaction.requested_at)
    && (interaction.resumed_at === null || timestamp(interaction.resumed_at)))
}

export function hasCanonicalSnapshotCapability(value: unknown): boolean {
  return record(value)
    && value.turn_lifecycle_schema === 1
    && Array.isArray(value.turns)
}

export function validateCanonicalSnapshotTurns(value: unknown): RoomSnapshotTurn[] | null {
  if (!hasCanonicalSnapshotCapability(value)) return null
  const turns = (value as { turns: unknown[] }).turns
  if (!turns.every(isRoomSnapshotTurn)) return null
  const ids = turns.map((turn) => turn.hybro_turn_id)
  return new Set(ids).size === ids.length ? turns : null
}

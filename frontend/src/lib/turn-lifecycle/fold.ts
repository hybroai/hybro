import type {
  AgentResponseData,
  TaskSubmittedData,
  TaskUpdateData,
} from '@/lib/types/sse'
import type {
  CanonicalHITLRequestData,
  CanonicalHITLResponseData,
  CanonicalRunEventData,
  MessageEndPayload,
  MessageStartPayload,
  MessageUpdatePayload,
  ModelDecisionPayload,
  RetryScheduledPayload,
  RoomSnapshotTurn,
  RunResumedPayload,
  RunSettledPayload,
  RunWaitingInputPayload,
  ToolExecutionEndPayload,
  ToolExecutionStartPayload,
  ToolExecutionUpdatePayload,
  TurnActivityItem,
  TurnEndPayload,
  TurnProjection,
  TurnStartPayload,
} from './types'

export type TurnProjectionMap = Record<string, TurnProjection>

export type CanonicalFoldEvent =
  | { kind: 'run_event'; data: CanonicalRunEventData }
  | { kind: 'agent_response'; data: AgentResponseData }
  | { kind: 'hitl_request'; data: CanonicalHITLRequestData }
  | { kind: 'hitl_response'; data: CanonicalHITLResponseData }
  | { kind: 'task_submitted'; data: TaskSubmittedData }
  | { kind: 'task_update'; data: TaskUpdateData }

export type FoldResult =
  | { ok: true; turns: TurnProjectionMap; changed: boolean }
  | { ok: false; turns: TurnProjectionMap; violation: string }

function cloneTurn(turn: TurnProjection): TurnProjection {
  return JSON.parse(JSON.stringify(turn)) as TurnProjection
}

function unchanged(turns: TurnProjectionMap): FoldResult {
  return { ok: true, turns, changed: false }
}

function failed(turns: TurnProjectionMap, violation: string): FoldResult {
  return { ok: false, turns, violation }
}

function changed(turns: TurnProjectionMap, turn: TurnProjection): FoldResult {
  return { ok: true, turns: { ...turns, [turn.id]: turn }, changed: true }
}

function codePointSlice(value: string, start: number, end: number): string {
  return Array.from(value).slice(start, end).join('')
}

function hasOpenChildren(turn: TurnProjection): boolean {
  return Boolean(
    turn.currentAssistant
    || turn.activeInteractionId
    || turn.internalTurns.some((item) => item.status === 'active')
    || turn.activity.some((item) => (
      item.kind === 'tool' && (item.status === 'running' || item.status === 'suspended')
    )),
  )
}

function eventOrder(data: { room_seq?: number }): number | null {
  return typeof data.room_seq === 'number'
    && Number.isInteger(data.room_seq)
    && data.room_seq >= 0
    ? data.room_seq
    : null
}

function foldRunEvent(
  turns: TurnProjectionMap,
  conversationId: string,
  data: CanonicalRunEventData,
): FoldResult {
  const order = eventOrder(data)
  if (order === null) return failed(turns, `${data.type} is missing authoritative room_seq`)

  if (data.type === 'run_started') {
    const existing = turns[data.run_id]
    if (existing) {
      return existing.userMessageId === data.payload.user_message_id
        && existing.clientRequestId === data.correlation_id
        ? unchanged(turns)
        : failed(turns, 'run_started contradicts the existing Turn root')
    }
    const turn: TurnProjection = {
      id: data.payload.hybro_turn_id,
      runId: data.run_id,
      conversationId,
      userMessageId: data.payload.user_message_id,
      clientRequestId: data.correlation_id,
      state: 'active',
      startedAt: data.payload.started_at,
      internalTurns: [],
      activity: [],
      finalCommitted: false,
      hitlInteractions: [],
      agentCallMessageIds: [],
    }
    return changed(turns, turn)
  }

  const existing = turns[data.run_id]
  if (!existing) return failed(turns, `${data.type} arrived before run_started`)
  if (existing.clientRequestId !== data.correlation_id) {
    return failed(turns, `${data.type} does not match the Turn correlation root`)
  }
  if (['completed', 'failed', 'canceled'].includes(existing.state)) {
    if (data.type === 'run_settled' && existing.state === data.payload.status) return unchanged(turns)
    return failed(turns, `${data.type} arrived after the Turn settled`)
  }
  if (existing.finalAnswer) {
    const finalPayload = data.type === 'message_end' ? data.payload as MessageEndPayload : null
    const duplicateFinal = finalPayload?.disposition === 'final'
      && finalPayload.message_id === existing.finalAnswer.messageId
      && finalPayload.internal_turn_id === existing.finalAnswer.internalTurnId
      && finalPayload.text === existing.finalAnswer.text
    const closesFinalTurn = data.type === 'turn_end'
      && data.payload.internal_turn_id === existing.finalAnswer.internalTurnId
    if (!duplicateFinal && !closesFinalTurn && data.type !== 'run_settled') {
      return failed(turns, `${data.type} arrived after the final Assistant message`)
    }
  }

  const turn = cloneTurn(existing)

  if (data.type === 'turn_start') {
    const payload = data.payload as TurnStartPayload
    const duplicate = turn.internalTurns.find((item) => item.internalTurnId === payload.internal_turn_id)
    if (duplicate) {
      return duplicate.attempt === payload.attempt ? unchanged(turns) : failed(turns, 'turn_start attempt changed')
    }
    if (turn.internalTurns.some((item) => item.status === 'active')) {
      return failed(turns, 'turn_start arrived while another internal Turn is active')
    }
    turn.internalTurns.push({
      internalTurnId: payload.internal_turn_id,
      attempt: payload.attempt,
      messageIds: [],
      toolCallIds: [],
      status: 'active',
    })
    return changed(turns, turn)
  }

  if (data.type === 'retry_scheduled') {
    const payload = data.payload as RetryScheduledPayload
    const internal = turn.internalTurns.find((item) => item.internalTurnId === payload.internal_turn_id)
    if (!internal || !['error', 'aborted'].includes(internal.status)) {
      return failed(turns, 'retry_scheduled does not belong to a closed failed internal Turn')
    }
    if (turn.activity.some((item) => item.kind === 'retry' && item.id === data.event_id)) return unchanged(turns)
    turn.activity.push({
      kind: 'retry',
      id: data.event_id,
      internalTurnId: payload.internal_turn_id,
      attempt: payload.attempt,
      delayMs: payload.delay_ms,
      errorClass: payload.error_class,
      order,
    })
    return changed(turns, turn)
  }

  if (data.type === 'model_decision') {
    const payload = data.payload as ModelDecisionPayload
    const internal = turn.internalTurns.find((item) => item.internalTurnId === payload.internal_turn_id)
    if (!internal || internal.status !== 'active') {
      return failed(turns, 'model_decision does not belong to an active internal Turn')
    }
    if (turn.activity.some((item) => item.kind === 'decision' && item.id === data.event_id)) return unchanged(turns)
    turn.activity.push({
      kind: 'decision',
      id: data.event_id,
      internalTurnId: payload.internal_turn_id,
      decision: payload.decision,
      agentLabel: payload.agent_label ?? undefined,
      questionSummary: payload.question_summary ?? undefined,
      sourceSummary: payload.source_summary ?? undefined,
      reason: payload.reason ?? undefined,
      order,
    })
    return changed(turns, turn)
  }

  if (data.type === 'run_waiting_input') {
    const payload = data.payload as RunWaitingInputPayload
    const interaction = turn.hitlInteractions.find((item) => item.interactionId === payload.interaction_id)
    if (!interaction || interaction.requestIds.length !== payload.request_ids.length
      || interaction.requestIds.some((requestId, index) => requestId !== payload.request_ids[index])) {
      return failed(turns, 'run_waiting_input does not reference the complete ordered HITL request set')
    }
    const indexes = interaction.requests.map((request) => request.questionIndex).sort((a, b) => a - b)
    if (interaction.requests.length !== interaction.requestIds.length
      || interaction.requests.some((request) => request.questionCount !== interaction.requests.length)
      || indexes.some((index, position) => index !== position)) {
      return failed(turns, 'run_waiting_input references an incomplete questionnaire')
    }
    if (turn.activeInteractionId === interaction.interactionId && turn.state === 'awaiting_input') return unchanged(turns)
    if (turn.activeInteractionId) return failed(turns, 'run_waiting_input conflicts with another active interaction')
    interaction.requestedAt = payload.requested_at
    interaction.state = 'awaiting_input'
    turn.activeInteractionId = interaction.interactionId
    turn.state = 'awaiting_input'
    return changed(turns, turn)
  }

  if (data.type === 'run_resumed') {
    const payload = data.payload as RunResumedPayload
    const interaction = turn.hitlInteractions.find((item) => item.interactionId === payload.interaction_id)
    if (!interaction || turn.activeInteractionId !== interaction.interactionId
      || interaction.requestIds.length !== payload.resolved_request_ids.length
      || interaction.requestIds.some((requestId, index) => requestId !== payload.resolved_request_ids[index])
      || interaction.requests.some((request) => request.status !== 'responded')) {
      return failed(turns, 'run_resumed does not match a fully responded active interaction')
    }
    interaction.state = 'resumed'
    interaction.resumedAt = payload.resumed_at
    turn.activeInteractionId = undefined
    turn.state = 'active'
    return changed(turns, turn)
  }

  if (data.type === 'run_settled') {
    const payload = data.payload as RunSettledPayload
    if (hasOpenChildren(turn)) return failed(turns, 'run_settled arrived with open child lifecycle state')
    if (payload.status === 'completed') {
      const lastInternal = turn.internalTurns[turn.internalTurns.length - 1]
      const earlierTurnsClosed = turn.internalTurns.slice(0, -1).every((item) => (
        item.status === 'completed'
        || (['error', 'aborted'].includes(item.status)
          && turn.activity.some((activity) => activity.kind === 'retry' && activity.internalTurnId === item.internalTurnId))
      ))
      if (!turn.finalCommitted
        || turn.finalAnswer?.messageId !== payload.final_message_id
        || !lastInternal
        || lastInternal.status !== 'completed'
        || turn.finalAnswer.internalTurnId !== lastInternal.internalTurnId
        || lastInternal.messageIds[lastInternal.messageIds.length - 1] !== turn.finalAnswer.messageId
        || !earlierTurnsClosed) {
        return failed(turns, 'completed run_settled arrived before exact final commitment and Turn closure')
      }
    } else if (turn.internalTurns.length > 0) {
      const lastInternal = turn.internalTurns[turn.internalTurns.length - 1]
      if (!['error', 'aborted'].includes(lastInternal.status)) {
        return failed(turns, `${payload.status} run_settled requires an error/aborted final internal Turn`)
      }
    }
    turn.state = payload.status
    turn.settledAt = payload.settled_at
    turn.durationMs = payload.duration_ms
    if (payload.status === 'failed') {
      turn.terminalCode = payload.failure_code
      turn.terminalSummary = payload.error_summary
    } else if (payload.status === 'canceled') {
      turn.terminalCode = payload.cancellation_code
    }
    return changed(turns, turn)
  }

  const internalTurnId = (data.payload as { internal_turn_id: string }).internal_turn_id
  const internal = turn.internalTurns.find((item) => item.internalTurnId === internalTurnId)
  if (!internal) return failed(turns, `${data.type} references an unknown internal Turn`)

  if (data.type === 'message_start') {
    const payload = data.payload as MessageStartPayload
    if (internal.status !== 'active') return failed(turns, 'message_start references a closed internal Turn')
    if (turn.currentAssistant) {
      return turn.currentAssistant.messageId === payload.message_id
        ? unchanged(turns)
        : failed(turns, 'message_start arrived while another Assistant is open')
    }
    if (internal.messageIds.includes(payload.message_id)) return unchanged(turns)
    internal.messageIds.push(payload.message_id)
    turn.currentAssistant = {
      messageId: payload.message_id,
      internalTurnId,
      text: '',
      status: 'streaming',
      contentIndex: 0,
      nextDeltaIndex: 0,
      endOffset: 0,
      order,
    }
    return changed(turns, turn)
  }

  if (data.type === 'message_update') {
    const payload = data.payload as MessageUpdatePayload
    const current = turn.currentAssistant
    const delta = payload.assistant_message_event
    if (!current || current.messageId !== payload.message_id || current.internalTurnId !== internalTurnId) {
      return failed(turns, 'message_update does not match the open Assistant')
    }
    if (delta.content_index !== current.contentIndex) return failed(turns, 'message_update changed content_index')
    if (delta.delta_index < current.nextDeltaIndex) {
      const duplicate = delta.end_offset <= current.endOffset
        && codePointSlice(current.text, delta.start_offset, delta.end_offset) === delta.delta
      return duplicate ? unchanged(turns) : failed(turns, 'message_update duplicate contradicts assembled text')
    }
    if (delta.delta_index !== current.nextDeltaIndex || delta.start_offset !== current.endOffset) {
      return failed(turns, 'message_update offset/index gap requires snapshot recovery')
    }
    current.text += delta.delta
    current.endOffset = delta.end_offset
    current.nextDeltaIndex += 1
    return changed(turns, turn)
  }

  if (data.type === 'message_end') {
    const payload = data.payload as MessageEndPayload
    const current = turn.currentAssistant
    if (!current || current.messageId !== payload.message_id || current.internalTurnId !== internalTurnId) {
      const priorActivity = turn.activity.find((item): item is Extract<TurnActivityItem, { kind: 'assistant' }> => (
        item.kind === 'assistant' && item.id === payload.message_id
      ))
      const priorText = turn.finalAnswer?.messageId === payload.message_id
        ? turn.finalAnswer.text
        : priorActivity?.text
      return priorText === payload.text ? unchanged(turns) : failed(turns, 'message_end does not match the open Assistant')
    }
    if (current.nextDeltaIndex > 0 && current.text !== payload.text) {
      return failed(turns, 'message_end contradicts assembled durable deltas')
    }
    const status: NonNullable<TurnProjection['currentAssistant']>['status'] =
      payload.disposition === 'commentary' || payload.disposition === 'final'
        ? 'completed'
        : payload.disposition
    const checkpoint = {
      ...current,
      text: payload.text,
      status,
      endOffset: Array.from(payload.text).length,
      order,
    }
    if (payload.disposition === 'final') {
      if (turn.finalAnswer
        && (turn.finalAnswer.messageId !== payload.message_id
          || turn.finalAnswer.internalTurnId !== internalTurnId
          || turn.finalAnswer.text !== payload.text)) {
        return failed(turns, 'final Assistant identity changed')
      }
      turn.finalAnswer = checkpoint
    } else if (payload.text) {
      turn.activity.push({
        kind: 'assistant',
        id: payload.message_id,
        internalTurnId,
        text: payload.text,
        status,
        order,
      })
    }
    turn.currentAssistant = undefined
    return changed(turns, turn)
  }

  if (data.type === 'tool_execution_start') {
    const payload = data.payload as ToolExecutionStartPayload
    const prior = turn.activity.find((item) => item.kind === 'tool' && item.toolCallId === payload.tool_call_id)
    if (prior) return prior.internalTurnId === internalTurnId ? unchanged(turns) : failed(turns, 'Tool identity changed ownership')
    if (internal.status !== 'active') return failed(turns, 'tool_execution_start references a closed internal Turn')
    internal.toolCallIds.push(payload.tool_call_id)
    turn.activity.push({
      kind: 'tool',
      id: payload.tool_call_id,
      internalTurnId,
      toolCallId: payload.tool_call_id,
      label: payload.tool_name,
      input: payload.input,
      partialResult: '',
      updateIndex: 0,
      status: 'running',
      executionKind: payload.execution_kind ?? 'tool',
      targetName: payload.target?.name,
      requestSummary: payload.request_summary ?? '',
      detailAvailable: false,
      order,
    })
    return changed(turns, turn)
  }

  if (data.type === 'tool_execution_update') {
    const payload = data.payload as ToolExecutionUpdatePayload
    const tool = turn.activity.find((item): item is Extract<TurnActivityItem, { kind: 'tool' }> => (
      item.kind === 'tool' && item.toolCallId === payload.tool_call_id
    ))
    if (!tool || tool.internalTurnId !== internalTurnId) return failed(turns, 'tool_execution_update has no matching Tool start')
    if (['completed', 'failed', 'canceled'].includes(tool.status)) return failed(turns, 'tool_execution_update arrived after Tool terminal')
    if (payload.update_index <= tool.updateIndex) return unchanged(turns)
    if (payload.execution_kind !== undefined && payload.execution_kind !== tool.executionKind) {
      return failed(turns, 'tool_execution_update changed execution kind')
    }
    if (payload.target !== undefined && payload.target !== null
      && payload.target.name !== tool.targetName) {
      return failed(turns, 'tool_execution_update changed execution target')
    }
    tool.updateIndex = payload.update_index
    tool.status = payload.status
    tool.partialResult = payload.partial_result
    return changed(turns, turn)
  }

  if (data.type === 'tool_execution_end') {
    const payload = data.payload as ToolExecutionEndPayload
    const tool = turn.activity.find((item): item is Extract<TurnActivityItem, { kind: 'tool' }> => (
      item.kind === 'tool' && item.toolCallId === payload.tool_call_id
    ))
    if (!tool || tool.internalTurnId !== internalTurnId) return failed(turns, 'tool_execution_end has no matching Tool start')
    if (['completed', 'failed', 'canceled'].includes(tool.status)) {
      return tool.status === payload.outcome ? unchanged(turns) : failed(turns, 'Tool terminal outcome changed')
    }
    if (payload.execution_kind !== undefined && payload.execution_kind !== tool.executionKind) {
      return failed(turns, 'tool_execution_end changed execution kind')
    }
    if (payload.target !== undefined && payload.target !== null
      && payload.target.name !== tool.targetName) {
      return failed(turns, 'tool_execution_end changed execution target')
    }
    tool.status = payload.outcome
    tool.result = payload.result
    tool.isError = payload.is_error
    tool.durationMs = payload.duration_ms
    tool.detailAvailable = payload.detail_available === true
    if ('failure_reason' in payload && payload.failure_reason) tool.failureReason = payload.failure_reason
    if (payload.outcome === 'failed') {
      tool.errorCode = payload.error_code ?? undefined
      tool.errorMessage = payload.error_message ?? undefined
    }
    return changed(turns, turn)
  }

  if (data.type === 'turn_end') {
    const payload = data.payload as TurnEndPayload
    if (internal.status !== 'active') return internal.status === payload.status ? unchanged(turns) : failed(turns, 'Internal Turn terminal changed')
    const tools = turn.activity.filter((item): item is Extract<TurnActivityItem, { kind: 'tool' }> => (
      item.kind === 'tool' && item.internalTurnId === internalTurnId
    ))
    // turn_end names the turn's tools in call order; they start in pass
    // order (a call asked about later starts after a sibling), so the
    // inventory must match exactly as a set. The contract keeps the ids unique.
    const listed = new Set(payload.tool_call_ids)
    if (tools.some((tool) => tool.status === 'running' || tool.status === 'suspended')
      || tools.length !== payload.tool_call_ids.length
      || tools.some((tool) => !listed.has(tool.toolCallId))) {
      return failed(turns, 'turn_end Tool inventory is not exact and terminal')
    }
    const expectedMessageId = internal.messageIds[internal.messageIds.length - 1]
    if ((expectedMessageId ?? null) !== (payload.message_id ?? null)) {
      return failed(turns, 'turn_end message identity does not match its Assistant')
    }
    internal.status = payload.status
    return changed(turns, turn)
  }

  return failed(turns, `Unhandled canonical event ${(data as CanonicalRunEventData).type}`)
}

function findExactRoot(
  turns: TurnProjectionMap,
  runId: string,
  clientRequestId: string,
  userMessageId: string,
): TurnProjection | undefined {
  const turn = turns[runId]
  return turn?.clientRequestId === clientRequestId && turn.userMessageId === userMessageId
    ? turn
    : undefined
}

function foldHITLRequest(
  turns: TurnProjectionMap,
  data: CanonicalHITLRequestData,
): FoldResult {
  const existing = findExactRoot(turns, data.run_id, data.client_request_id, data.related_user_message_id)
  if (!existing) return failed(turns, 'Canonical HITL request does not match the exact Turn root')
  const turn = cloneTurn(existing)
  let interaction = turn.hitlInteractions.find((item) => item.interactionId === data.interaction_id)
  if (!interaction) {
    interaction = {
      interactionId: data.interaction_id,
      state: 'awaiting_input',
      requestIds: [],
      requests: [],
      requestedAt: '',
    }
    turn.hitlInteractions.push(interaction)
  }
  const prior = interaction.requests.find((request) => request.requestId === data.request_id)
  if (prior) {
    const exactDuplicate = prior.messageId === data.message_id
      && prior.questionIndex === data.question_index
      && prior.questionCount === data.question_count
      && prior.prompt === data.prompt
      && prior.promptType === data.prompt_type
      && prior.source === data.source
      && prior.agentLabel === (data.agent_label ?? undefined)
      && prior.choices.length === (data.choices ?? []).length
      && prior.choices.every((choice, index) => choice === (data.choices ?? [])[index])
    return exactDuplicate
      ? unchanged(turns)
      : failed(turns, 'Canonical HITL request identity changed')
  }
  if (interaction.requests.some((request) => request.questionIndex === data.question_index)) {
    return failed(turns, 'Canonical HITL question index is duplicated')
  }
  interaction.requestIds.push(data.request_id)
  interaction.requests.push({
    requestId: data.request_id,
    messageId: data.message_id,
    questionIndex: data.question_index,
    questionCount: data.question_count,
    prompt: data.prompt,
    promptType: data.prompt_type,
    choices: data.choices ?? [],
    source: data.source,
    agentLabel: data.agent_label ?? undefined,
    status: 'requested',
  })
  return changed(turns, turn)
}

function foldHITLResponse(
  turns: TurnProjectionMap,
  data: CanonicalHITLResponseData,
): FoldResult {
  const existing = findExactRoot(turns, data.run_id, data.client_request_id, data.related_user_message_id)
  if (!existing) return failed(turns, 'Canonical HITL response does not match the exact Turn root')
  const turn = cloneTurn(existing)
  const interaction = turn.hitlInteractions.find((item) => item.interactionId === data.interaction_id)
  const request = interaction?.requests.find((item) => item.requestId === data.request_id)
  if (!interaction || !request
    || request.messageId !== data.message_id
    || request.questionIndex !== data.question_index
    || request.questionCount !== data.question_count) {
    return failed(turns, 'Canonical HITL response does not match its durable request')
  }
  if (request.status !== 'requested') return request.status === data.status ? unchanged(turns) : failed(turns, 'HITL terminal status changed')
  request.status = data.status
  request.answerRef = data.answer_ref ?? undefined
  const terminal = interaction.requests.length === interaction.requestIds.length
    && interaction.requests.every((item) => item.status !== 'requested')
  if (terminal) {
    const statuses = new Set(interaction.requests.map((item) => item.status))
    const nonResumable = (['error', 'canceled', 'expired'] as const).find((status) => statuses.has(status))
    if (nonResumable) {
      interaction.state = nonResumable
      if (turn.activeInteractionId === interaction.interactionId) turn.activeInteractionId = undefined
    }
  }
  return changed(turns, turn)
}

function foldAgentResponse(turns: TurnProjectionMap, data: AgentResponseData): FoldResult {
  const turn = Object.values(turns).find((item) => (
    item.clientRequestId === data.client_request_id
    && item.userMessageId === data.related_message_id
    && item.finalAnswer?.messageId === data.message_id
  ))
  if (!turn) return unchanged(turns)
  const durableText = data.content ?? ''
  if (turn.finalCommitted) {
    return turn.finalAnswer?.text === durableText
      ? unchanged(turns)
      : failed(turns, 'Contradictory duplicate final agent_response')
  }
  const next = cloneTurn(turn)
  next.finalAnswer = { ...next.finalAnswer!, text: durableText }
  next.finalCommitted = true
  return changed(turns, next)
}

function foldTask(
  turns: TurnProjectionMap,
): FoldResult {
  // Canonical Agent Cards are folded exclusively from tool_execution_*
  // events. Deprecated task_submitted/task_update frames are never applied
  // to live canonical state.
  return failed(turns, 'Canonical task card frames are deprecated')
}

export function foldCanonicalEvent(
  turns: TurnProjectionMap,
  conversationId: string,
  event: CanonicalFoldEvent,
): FoldResult {
  switch (event.kind) {
    case 'run_event':
      return foldRunEvent(turns, conversationId, event.data)
    case 'hitl_request':
      return foldHITLRequest(turns, event.data)
    case 'hitl_response':
      return foldHITLResponse(turns, event.data)
    case 'agent_response':
      return foldAgentResponse(turns, event.data)
    case 'task_submitted':
    case 'task_update':
      return foldTask(turns)
  }
}

export function snapshotTurnToProjection(conversationId: string, value: RoomSnapshotTurn): TurnProjection {
  const assistant = (item: RoomSnapshotTurn['current_assistant']): TurnProjection['currentAssistant'] => item ? ({
    messageId: item.message_id,
    internalTurnId: item.internal_turn_id,
    text: item.text,
    status: item.status,
    contentIndex: item.content_index ?? 0,
    nextDeltaIndex: item.next_delta_index ?? 0,
    endOffset: item.end_offset ?? Array.from(item.text).length,
    order: item.order,
  }) : undefined
  return {
    id: value.hybro_turn_id,
    runId: value.run_id,
    conversationId,
    userMessageId: value.user_message_id,
    clientRequestId: value.client_request_id,
    state: value.state,
    startedAt: value.started_at,
    settledAt: value.settled_at ?? undefined,
    durationMs: value.duration_ms ?? undefined,
    terminalCode: value.terminal_code ?? undefined,
    terminalSummary: value.terminal_summary ?? undefined,
    internalTurns: value.internal_turns.map((item) => ({
      internalTurnId: item.internal_turn_id,
      attempt: item.attempt,
      messageIds: [...item.message_ids],
      toolCallIds: [...item.tool_call_ids],
      status: item.status,
    })),
    activity: value.activity.map((item) => {
      if (item.kind === 'assistant') return {
        kind: 'assistant' as const,
        id: item.message_id,
        internalTurnId: item.internal_turn_id,
        text: item.text,
        status: item.status,
        order: item.order,
      }
      if (item.kind === 'retry') return {
        kind: 'retry' as const,
        id: item.id,
        internalTurnId: item.internal_turn_id,
        attempt: item.attempt,
        delayMs: item.delay_ms,
        errorClass: item.error_class,
        order: item.order,
      }
      if (item.kind === 'decision') return {
        kind: 'decision' as const,
        id: item.id,
        internalTurnId: item.internal_turn_id,
        decision: item.decision,
        agentLabel: item.agent_label ?? undefined,
        questionSummary: item.question_summary ?? undefined,
        sourceSummary: item.source_summary ?? undefined,
        reason: item.reason ?? undefined,
        order: item.order,
      }
      return {
        kind: 'tool' as const,
        id: item.id,
        internalTurnId: item.internal_turn_id,
        toolCallId: item.tool_call_id,
        label: item.label,
        input: item.input,
        partialResult: item.partial_result,
        result: item.result ?? undefined,
        isError: item.is_error ?? undefined,
        durationMs: item.duration_ms ?? undefined,
        failureReason: item.failure_reason ?? undefined,
        updateIndex: item.update_index,
        status: item.status,
        executionKind: item.execution_kind ?? 'tool',
        targetName: item.target_name ?? undefined,
        requestSummary: item.request_summary ?? '',
        detailAvailable: item.detail_available === true,
        errorCode: item.error_code ?? undefined,
        errorMessage: item.error_message ?? undefined,
        order: item.order,
      }
    }),
    currentAssistant: assistant(value.current_assistant),
    finalAnswer: assistant(value.final_answer),
    finalCommitted: value.final_committed,
    hitlInteractions: value.hitl_interactions.map((interaction) => ({
      interactionId: interaction.interaction_id,
      state: interaction.state,
      requestIds: [...interaction.request_ids],
      requests: interaction.requests.map((request) => ({
        requestId: request.request_id,
        messageId: request.message_id,
        questionIndex: request.question_index,
        questionCount: request.question_count,
        prompt: request.prompt,
        promptType: request.prompt_type,
        choices: [...request.choices],
        source: request.source,
        agentLabel: request.agent_label ?? undefined,
        status: request.status,
        answerRef: request.answer_ref ?? undefined,
      })),
      requestedAt: interaction.requested_at,
      resumedAt: interaction.resumed_at ?? undefined,
    })),
    activeInteractionId: value.active_interaction_id ?? undefined,
    agentCallMessageIds: [...value.agent_call_message_ids],
  }
}

export function validateProjectionClosure(turn: TurnProjection): string | null {
  const internalIds = turn.internalTurns.map((item) => item.internalTurnId)
  if (new Set(internalIds).size !== internalIds.length) return 'snapshot Turn repeats an internal Turn identity'
  const owners = new Map(turn.internalTurns.map((item) => [item.internalTurnId, item]))
  for (const activity of turn.activity) {
    const owner = owners.get(activity.internalTurnId)
    if (!owner) return 'snapshot activity has no internal Turn owner'
    if (activity.kind === 'assistant' && !owner.messageIds.includes(activity.id)) {
      return 'snapshot Assistant activity is absent from its internal Turn inventory'
    }
    if (activity.kind === 'tool' && !owner.toolCallIds.includes(activity.toolCallId)) {
      return 'snapshot Tool activity is absent from its internal Turn inventory'
    }
    if (activity.kind === 'retry' && !['error', 'aborted'].includes(owner.status)) {
      return 'snapshot retry does not belong to a closed failed internal Turn'
    }
  }
  for (const internal of turn.internalTurns) {
    const toolRows = turn.activity.filter((item): item is Extract<TurnActivityItem, { kind: 'tool' }> => (
      item.kind === 'tool' && item.internalTurnId === internal.internalTurnId
    ))
    if (toolRows.length !== internal.toolCallIds.length
      || toolRows.some((tool, index) => tool.toolCallId !== internal.toolCallIds[index])) {
      return 'snapshot Turn Tool inventory does not match activity ownership'
    }
    if (internal.status !== 'active'
      && toolRows.some((tool) => tool.status === 'running' || tool.status === 'suspended')) {
      return 'closed snapshot internal Turn contains an open Tool'
    }
  }
  if (turn.currentAssistant) {
    const owner = owners.get(turn.currentAssistant.internalTurnId)
    if (turn.currentAssistant.status !== 'streaming'
      || !owner
      || owner.status !== 'active'
      || !owner.messageIds.includes(turn.currentAssistant.messageId)) {
      return 'snapshot current Assistant has no streaming active internal Turn owner'
    }
  }
  if (turn.finalAnswer) {
    const owner = owners.get(turn.finalAnswer.internalTurnId)
    if (turn.finalAnswer.status !== 'completed'
      || !owner
      || !owner.messageIds.includes(turn.finalAnswer.messageId)) {
      return 'snapshot final Assistant has no completed internal Turn ownership'
    }
  }
  if (turn.finalCommitted && !turn.finalAnswer) return 'snapshot final commit has no final Assistant'
  if (turn.state === 'awaiting_input' && !turn.activeInteractionId) {
    return 'awaiting_input snapshot Turn has no active interaction'
  }
  for (const interaction of turn.hitlInteractions) {
    if (interaction.requestIds.length !== interaction.requests.length
      || interaction.requestIds.some((requestId, index) => (
        interaction.requests[index]?.requestId !== requestId
      ))) {
      return 'snapshot HITL request inventory is not exact and ordered'
    }
    const indexes = interaction.requests.map((request) => request.questionIndex).sort((a, b) => a - b)
    if (interaction.requests.some((request) => request.questionCount !== interaction.requests.length)
      || indexes.some((index, position) => index !== position)) {
      return 'snapshot HITL questionnaire is incomplete'
    }
  }
  if (turn.activeInteractionId
    && !turn.hitlInteractions.some((item) => item.interactionId === turn.activeInteractionId && item.state === 'awaiting_input')) {
    return 'snapshot active HITL identity has no interaction owner'
  }
  if (['completed', 'failed', 'canceled'].includes(turn.state) && hasOpenChildren(turn)) {
    return 'terminal snapshot Turn contains open child lifecycle state'
  }
  if (turn.state === 'completed') {
    const lastInternal = turn.internalTurns[turn.internalTurns.length - 1]
    const earlierTurnsClosed = turn.internalTurns.slice(0, -1).every((item) => (
      item.status === 'completed'
      || (['error', 'aborted'].includes(item.status)
        && turn.activity.some((activity) => (
          activity.kind === 'retry' && activity.internalTurnId === item.internalTurnId
        )))
    ))
    if (!turn.finalCommitted
      || !turn.finalAnswer
      || lastInternal?.status !== 'completed'
      || turn.finalAnswer.internalTurnId !== lastInternal.internalTurnId
      || lastInternal.messageIds[lastInternal.messageIds.length - 1] !== turn.finalAnswer.messageId
      || !earlierTurnsClosed) {
      return 'completed snapshot Turn has no exact committed final closure'
    }
  } else if ((turn.state === 'failed' || turn.state === 'canceled') && turn.internalTurns.length > 0) {
    const lastInternal = turn.internalTurns[turn.internalTurns.length - 1]
    if (!['error', 'aborted'].includes(lastInternal.status)) {
      return `${turn.state} snapshot Turn does not end with error/aborted closure`
    }
  }
  return null
}

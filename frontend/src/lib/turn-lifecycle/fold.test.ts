import { beforeEach, describe, expect, it } from 'vitest'
import { validateCanonicalRunEventData, validateCanonicalSnapshotTurns } from './contract'
import { foldCanonicalEvent, snapshotTurnToProjection, type TurnProjectionMap } from './fold'
import type {
  CanonicalRunEventData,
  RoomSnapshotActivityItem,
  RoomSnapshotTurn,
} from './types'
import { useTurnPresentationStore } from '@/stores/turn-presentation-store'
import { selectCanonicalComposerAuthority, useTurnStore } from '@/stores/turn-store'

const TS = '2030-01-01T00:00:00.000Z'

function runEvent(
  roomSeq: number,
  type: CanonicalRunEventData['type'],
  payload: Record<string, unknown>,
): CanonicalRunEventData {
  return {
    room_seq: roomSeq,
    event_id: `event-${roomSeq}`,
    run_id: 'run-1',
    seq: roomSeq,
    type,
    payload,
    correlation_id: 'client-1',
  } as CanonicalRunEventData
}

function apply(turns: TurnProjectionMap, data: CanonicalRunEventData): TurnProjectionMap {
  const result = foldCanonicalEvent(turns, 'room-1', { kind: 'run_event', data })
  expect(result.ok).toBe(true)
  return result.ok ? result.turns : turns
}

function finalReadyProjection(): TurnProjectionMap {
  let turns: TurnProjectionMap = {}
  turns = apply(turns, runEvent(1, 'run_started', {
    hybro_turn_id: 'run-1', user_message_id: 'user-1', started_at: TS, mode: 'fast',
  }))
  turns = apply(turns, runEvent(2, 'turn_start', { internal_turn_id: 'turn-1', attempt: 1 }))
  turns = apply(turns, runEvent(3, 'message_start', {
    internal_turn_id: 'turn-1', message_id: 'assistant-1', role: 'assistant',
  }))
  turns = apply(turns, runEvent(4, 'message_update', {
    internal_turn_id: 'turn-1',
    message_id: 'assistant-1',
    assistant_message_event: {
      type: 'text_delta', content_index: 0, delta_index: 0,
      start_offset: 0, end_offset: 4, delta: 'done',
    },
  }))
  turns = apply(turns, runEvent(5, 'message_end', {
    internal_turn_id: 'turn-1', message_id: 'assistant-1',
    stop_reason: 'stop', disposition: 'final', text: 'done',
  }))
  turns = apply(turns, runEvent(6, 'turn_end', {
    internal_turn_id: 'turn-1', message_id: 'assistant-1', tool_call_ids: [], status: 'completed',
  }))
  return turns
}

describe('canonical Turn contract and fold', () => {
  it('runtime-validates closed known subtypes and rejects unknown nested/private fields', () => {
    const valid = runEvent(1, 'message_update', {
      internal_turn_id: 'turn-1',
      message_id: 'assistant-1',
      assistant_message_event: {
        type: 'text_delta', content_index: 0, delta_index: 0,
        start_offset: 0, end_offset: 1, delta: '好',
      },
    })
    expect(validateCanonicalRunEventData({
      ...valid,
      delivery_id: 'delivery-1',
      trace_id: 'trace-1',
    })).toMatchObject({ canonical: true, valid: true })

    expect(validateCanonicalRunEventData({
      ...valid,
      payload: {
        ...valid.payload,
        assistant_message_event: {
          ...((valid.payload as unknown) as { assistant_message_event: Record<string, unknown> }).assistant_message_event,
          thinking: 'private',
        },
      },
    })).toMatchObject({ canonical: true, valid: false })
    expect(validateCanonicalRunEventData({ ...valid, room_seq: undefined })).toMatchObject({ canonical: true, valid: false })
  })

  it('assembles offsets once and atomically moves commentary into Trace', () => {
    let turns: TurnProjectionMap = {}
    turns = apply(turns, runEvent(1, 'run_started', {
      hybro_turn_id: 'run-1', user_message_id: 'user-1', started_at: TS, mode: 'supervisor',
    }))
    turns = apply(turns, runEvent(2, 'turn_start', { internal_turn_id: 'turn-1', attempt: 1 }))
    turns = apply(turns, runEvent(3, 'message_start', {
      internal_turn_id: 'turn-1', message_id: 'assistant-1', role: 'assistant',
    }))
    const delta = runEvent(4, 'message_update', {
      internal_turn_id: 'turn-1', message_id: 'assistant-1',
      assistant_message_event: {
        type: 'text_delta', content_index: 0, delta_index: 0,
        start_offset: 0, end_offset: 13, delta: 'Checking now.',
      },
    })
    turns = apply(turns, delta)
    turns = apply(turns, delta)
    expect(turns['run-1'].currentAssistant?.text).toBe('Checking now.')

    turns = apply(turns, runEvent(5, 'message_end', {
      internal_turn_id: 'turn-1', message_id: 'assistant-1',
      stop_reason: 'tool_use', disposition: 'commentary', text: 'Checking now.',
    }))
    expect(turns['run-1'].currentAssistant).toBeUndefined()
    expect(turns['run-1'].finalAnswer).toBeUndefined()
    expect(turns['run-1'].activity[0]).toMatchObject({ kind: 'assistant', text: 'Checking now.', order: 5 })
  })

  it('rejects message_end text that contradicts already assembled durable deltas', () => {
    let turns: TurnProjectionMap = {}
    turns = apply(turns, runEvent(1, 'run_started', {
      hybro_turn_id: 'run-1', user_message_id: 'user-1', started_at: TS, mode: 'fast',
    }))
    turns = apply(turns, runEvent(2, 'turn_start', { internal_turn_id: 'turn-1', attempt: 1 }))
    turns = apply(turns, runEvent(3, 'message_start', {
      internal_turn_id: 'turn-1', message_id: 'assistant-1', role: 'assistant',
    }))
    turns = apply(turns, runEvent(4, 'message_update', {
      internal_turn_id: 'turn-1', message_id: 'assistant-1',
      assistant_message_event: {
        type: 'text_delta', content_index: 0, delta_index: 0,
        start_offset: 0, end_offset: 12, delta: 'durable text',
      },
    }))

    const result = foldCanonicalEvent(turns, 'room-1', {
      kind: 'run_event',
      data: runEvent(5, 'message_end', {
        internal_turn_id: 'turn-1', message_id: 'assistant-1',
        stop_reason: 'stop', disposition: 'final', text: 'short',
      }),
    })
    expect(result.ok).toBe(false)
    expect(result.turns['run-1'].currentAssistant?.text).toBe('durable text')
  })

  it('keeps repeated same-Agent Tool calls distinct and updates each opaque identity in place', () => {
    let turns: TurnProjectionMap = {}
    turns = apply(turns, runEvent(1, 'run_started', {
      hybro_turn_id: 'run-1', user_message_id: 'user-1', started_at: TS, mode: 'direct',
    }))
    turns = apply(turns, runEvent(2, 'turn_start', { internal_turn_id: 'turn-1', attempt: 1 }))
    for (const [index, id] of ['inv_weather_0001', 'inv_weather_0002'].entries()) {
      turns = apply(turns, runEvent(3 + index, 'tool_execution_start', {
        internal_turn_id: 'turn-1', tool_call_id: id, tool_name: 'Weather Agent', input: {},
      }))
    }
    turns = apply(turns, runEvent(5, 'tool_execution_update', {
      internal_turn_id: 'turn-1', tool_call_id: 'inv_weather_0001', tool_name: 'Weather Agent',
      update_index: 2, status: 'running', partial_result: 'halfway',
    }))
    turns = apply(turns, runEvent(6, 'tool_execution_update', {
      internal_turn_id: 'turn-1', tool_call_id: 'inv_weather_0001', tool_name: 'Weather Agent',
      update_index: 1, status: 'running', partial_result: 'old',
    }))
    const tools = turns['run-1'].activity.filter((item) => item.kind === 'tool')
    expect(tools).toHaveLength(2)
    expect(tools[0]).toMatchObject({ toolCallId: 'inv_weather_0001', partialResult: 'halfway', updateIndex: 2 })
    expect(tools[1]).toMatchObject({ toolCallId: 'inv_weather_0002' })
  })

  it('owns HITL only by the exact Turn root and complete request set', () => {
    let turns: TurnProjectionMap = {}
    turns = apply(turns, runEvent(1, 'run_started', {
      hybro_turn_id: 'run-1', user_message_id: 'user-1', started_at: TS, mode: 'fast',
    }))
    let result = foldCanonicalEvent(turns, 'room-1', {
      kind: 'hitl_request',
      data: {
        room_seq: 2, run_id: 'run-1', request_id: 'request-1', message_id: 'hitl-message-1',
        interaction_id: 'interaction-1', related_user_message_id: 'other-user',
        client_request_id: 'client-1', question_index: 0, question_count: 1,
        prompt: 'Continue?', prompt_type: 'confirmation', source: 'supervisor',
      },
    })
    expect(result.ok).toBe(false)
    expect(result.turns['run-1'].hitlInteractions).toEqual([])

    result = foldCanonicalEvent(turns, 'room-1', {
      kind: 'hitl_request',
      data: {
        room_seq: 3, run_id: 'run-1', request_id: 'request-1', message_id: 'hitl-message-1',
        interaction_id: 'interaction-1', related_user_message_id: 'user-1',
        client_request_id: 'client-1', question_index: 0, question_count: 1,
        prompt: 'Continue?', prompt_type: 'confirmation', source: 'supervisor',
      },
    })
    expect(result.ok).toBe(true)
    if (result.ok) turns = result.turns
    turns = apply(turns, runEvent(4, 'run_waiting_input', {
      interaction_id: 'interaction-1', request_ids: ['request-1'], requested_at: TS,
    }))
    expect(turns['run-1']).toMatchObject({ state: 'awaiting_input', activeInteractionId: 'interaction-1' })
  })

  it('commits only the exact final identity and rejects premature settlement without terminalizing children', () => {
    let turns = finalReadyProjection()
    let result = foldCanonicalEvent(turns, 'room-1', {
      kind: 'agent_response',
      data: {
        message_id: 'assistant-1', agent_id: 'specialist', content: 'wrong',
        client_request_id: 'different', related_message_id: 'user-1',
      },
    })
    expect(result.ok).toBe(true)
    if (result.ok) turns = result.turns
    expect(turns['run-1'].finalCommitted).toBe(false)

    result = foldCanonicalEvent(turns, 'room-1', {
      kind: 'run_event',
      data: runEvent(7, 'run_settled', {
        status: 'completed', started_at: TS, settled_at: '2030-01-01T00:00:01.000Z',
        duration_ms: 1000, final_message_id: 'assistant-1',
      }),
    })
    expect(result.ok).toBe(false)
    expect(result.turns['run-1'].state).toBe('active')

    result = foldCanonicalEvent(turns, 'room-1', {
      kind: 'agent_response',
      data: {
        message_id: 'assistant-1', agent_id: 'system:hybro', content: 'durable done',
        client_request_id: 'client-1', related_message_id: 'user-1',
      },
    })
    expect(result.ok).toBe(true)
    if (result.ok) turns = result.turns
    turns = apply(turns, runEvent(8, 'run_settled', {
      status: 'completed', started_at: TS, settled_at: '2030-01-01T00:00:01.000Z',
      duration_ms: 1000, final_message_id: 'assistant-1',
    }))
    expect(turns['run-1']).toMatchObject({ state: 'completed', finalCommitted: true, durationMs: 1000 })
    expect(turns['run-1'].finalAnswer?.text).toBe('durable done')

    const contradictory = foldCanonicalEvent(turns, 'room-1', {
      kind: 'agent_response',
      data: {
        message_id: 'assistant-1', agent_id: 'system:hybro', content: 'replaced',
        client_request_id: 'client-1', related_message_id: 'user-1',
      },
    })
    expect(contradictory.ok).toBe(false)
    expect(contradictory.turns['run-1'].finalAnswer?.text).toBe('durable done')
  })

  it('seals a final Assistant against successor turns and requires final ownership at settlement', () => {
    let turns = finalReadyProjection()
    let result = foldCanonicalEvent(turns, 'room-1', {
      kind: 'run_event',
      data: runEvent(7, 'turn_start', { internal_turn_id: 'turn-2', attempt: 1 }),
    })
    expect(result.ok).toBe(false)

    result = foldCanonicalEvent(turns, 'room-1', {
      kind: 'agent_response',
      data: {
        message_id: 'assistant-1', agent_id: 'system:hybro', content: 'done',
        client_request_id: 'client-1', related_message_id: 'user-1',
      },
    })
    expect(result.ok).toBe(true)
    if (result.ok) turns = result.turns
    const adversarial = {
      ...turns['run-1'],
      internalTurns: [
        ...turns['run-1'].internalTurns,
        {
          internalTurnId: 'turn-2', attempt: 2, messageIds: ['assistant-2'],
          toolCallIds: [], status: 'completed' as const,
        },
      ],
    }
    result = foldCanonicalEvent({ 'run-1': adversarial }, 'room-1', {
      kind: 'run_event',
      data: runEvent(8, 'run_settled', {
        status: 'completed', started_at: TS, settled_at: '2030-01-01T00:00:01.000Z',
        duration_ms: 1000, final_message_id: 'assistant-1',
      }),
    })
    expect(result.ok).toBe(false)
  })

  it('rejects state-contradictory snapshot terminal metadata', () => {
    const base: RoomSnapshotTurn = {
      hybro_turn_id: 'run-1', run_id: 'run-1', user_message_id: 'user-1', client_request_id: 'client-1',
      state: 'active', started_at: TS, settled_at: null, duration_ms: null,
      terminal_code: null, terminal_summary: null, internal_turns: [], activity: [],
      current_assistant: null, final_answer: null, final_committed: false,
      hitl_interactions: [], active_interaction_id: null, agent_call_message_ids: [],
    }
    const validates = (turn: RoomSnapshotTurn) => validateCanonicalSnapshotTurns({
      turn_lifecycle_schema: 1, turns: [turn],
    })
    expect(validates(base)).not.toBeNull()
    expect(validates({ ...base, settled_at: TS })).toBeNull()
    expect(validates({ ...base, state: 'awaiting_input', terminal_code: 'policy' })).toBeNull()
    expect(validates({
      ...base, state: 'failed', settled_at: TS, duration_ms: 0,
      terminal_code: 'not-allowlisted', terminal_summary: 'Failed.',
    })).toBeNull()
    expect(validates({
      ...base, state: 'canceled', settled_at: TS, duration_ms: 0,
      terminal_code: 'user_requested', terminal_summary: 'raw reason',
    })).toBeNull()
    expect(validates({
      ...base, state: 'completed', settled_at: TS, duration_ms: 0,
      final_committed: false,
    })).toBeNull()
    expect(validates({
      ...base, state: 'failed', settled_at: '2029-12-31T23:59:59.000Z', duration_ms: 0,
      terminal_code: 'internal_error', terminal_summary: 'Failed.',
    })).toBeNull()
  })

  it('rejects orphan snapshot activity and invalid Assistant aggregate ownership', () => {
    const base: RoomSnapshotTurn = {
      hybro_turn_id: 'run-1', run_id: 'run-1', user_message_id: 'user-1', client_request_id: 'client-1',
      state: 'active', started_at: TS, settled_at: null, duration_ms: null,
      terminal_code: null, terminal_summary: null,
      internal_turns: [{
        internal_turn_id: 'turn-1', attempt: 1, message_ids: ['assistant-1'],
        tool_call_ids: ['inv_weather_0001'], status: 'active',
      }],
      activity: [], current_assistant: null, final_answer: null, final_committed: false,
      hitl_interactions: [], active_interaction_id: null, agent_call_message_ids: [],
    }
    const validates = (turn: RoomSnapshotTurn) => validateCanonicalSnapshotTurns({
      turn_lifecycle_schema: 1, turns: [turn],
    })
    expect(validates({
      ...base,
      current_assistant: {
        message_id: 'assistant-1', internal_turn_id: 'turn-1', text: 'live', status: 'streaming', order: 1,
      },
    })).not.toBeNull()
    expect(validates({
      ...base,
      current_assistant: {
        message_id: 'assistant-1', internal_turn_id: 'turn-1', text: 'done', status: 'completed', order: 1,
      },
    })).toBeNull()
    expect(validates({
      ...base,
      final_answer: {
        message_id: 'assistant-1', internal_turn_id: 'turn-1', text: 'live', status: 'streaming', order: 1,
      },
    })).toBeNull()
    expect(validates({
      ...base,
      final_answer: {
        message_id: 'assistant-1', internal_turn_id: 'turn-1', text: 'done', status: 'completed', order: 1,
      },
    })).not.toBeNull()

    const orphanActivity: RoomSnapshotActivityItem[] = [
      {
        kind: 'assistant', message_id: 'assistant-1', internal_turn_id: 'missing-turn',
        text: 'commentary', status: 'completed', order: 2,
      },
      {
        kind: 'retry', id: 'retry-1', internal_turn_id: 'missing-turn',
        attempt: 2, delay_ms: 0, error_class: 'context_overflow', order: 3,
      },
      {
        kind: 'tool', id: 'inv_weather_0001', internal_turn_id: 'missing-turn',
        tool_call_id: 'inv_weather_0001', label: 'Weather Agent', input: {},
        partial_result: '', result: null, is_error: null, duration_ms: null,
        status: 'running', update_index: 0, order: 4,
      },
    ]
    for (const activity of orphanActivity) {
      expect(validates({ ...base, activity: [activity] })).toBeNull()
    }
    expect(validates({
      ...base,
      current_assistant: {
        message_id: 'assistant-1', internal_turn_id: 'missing-turn', text: 'live', status: 'streaming', order: 1,
      },
    })).toBeNull()
    expect(validates({
      ...base,
      final_answer: {
        message_id: 'missing-message', internal_turn_id: 'turn-1', text: 'done', status: 'completed', order: 1,
      },
    })).toBeNull()
  })

  it('rejects snapshot Tool rows that contradict the live terminal contract', () => {
    const base: RoomSnapshotTurn = {
      hybro_turn_id: 'run-1', run_id: 'run-1', user_message_id: 'user-1', client_request_id: 'client-1',
      state: 'active', started_at: TS, settled_at: null, duration_ms: null,
      terminal_code: null, terminal_summary: null,
      internal_turns: [{
        internal_turn_id: 'turn-1', attempt: 1, message_ids: [],
        tool_call_ids: ['inv_weather_0001'], status: 'active',
      }],
      activity: [{
        kind: 'tool', id: 'inv_weather_0001', internal_turn_id: 'turn-1',
        tool_call_id: 'inv_weather_0001', label: 'Weather Agent', input: {},
        partial_result: '', result: 'Sunny', is_error: false, duration_ms: 10,
        status: 'completed', update_index: 0, order: 1,
      }],
      current_assistant: null, final_answer: null, final_committed: false,
      hitl_interactions: [], active_interaction_id: null, agent_call_message_ids: [],
    }
    const validates = (turn: RoomSnapshotTurn) => validateCanonicalSnapshotTurns({
      turn_lifecycle_schema: 1, turns: [turn],
    })
    expect(validates(base)).not.toBeNull()
    const tool = base.activity[0] as Extract<RoomSnapshotActivityItem, { kind: 'tool' }>
    expect(validates({ ...base, activity: [{ ...tool, is_error: true }] })).toBeNull()
    expect(validates({ ...base, activity: [{ ...tool, status: 'failed', is_error: false }] })).toBeNull()
    expect(validates({ ...base, activity: [{ ...tool, status: 'canceled', result: 'not empty' }] })).toBeNull()
    expect(validates({
      ...base,
      activity: [{ ...tool, failure_reason: 'private_exception' } as never],
    })).toBeNull()
    expect(validates({ ...base, activity: [{ ...tool, duration_ms: -1 }] })).toBeNull()
    expect(validates({ ...base, activity: [{ ...tool, label: 'x'.repeat(161) }] })).toBeNull()
    expect(validates({
      ...base,
      activity: [{ ...tool, status: 'running', result: null, is_error: null, duration_ms: 1 }],
    })).toBeNull()
  })

  it('maps canonical snapshots to the same visible projection and preserves presentation state on replacement', () => {
    const live = finalReadyProjection()['run-1']
    const snapshot: RoomSnapshotTurn = {
      hybro_turn_id: 'run-1', run_id: 'run-1', user_message_id: 'user-1', client_request_id: 'client-1',
      state: 'active', started_at: TS, settled_at: null, duration_ms: null,
      terminal_code: null, terminal_summary: null,
      internal_turns: [{
        internal_turn_id: 'turn-1', attempt: 1, message_ids: ['assistant-1'], tool_call_ids: [], status: 'completed',
      }],
      activity: [], current_assistant: null,
      final_answer: {
        message_id: 'assistant-1', internal_turn_id: 'turn-1', text: 'done', status: 'completed', order: 5,
      },
      final_committed: false, hitl_interactions: [], active_interaction_id: null, agent_call_message_ids: [],
    }
    const restored = snapshotTurnToProjection('room-1', snapshot)
    expect(restored.finalAnswer).toMatchObject({ messageId: live.finalAnswer?.messageId, text: live.finalAnswer?.text })
    expect(restored.internalTurns).toEqual(live.internalTurns)

    useTurnStore.getState().replaceSnapshot('room-1', [snapshot])
    useTurnPresentationStore.getState().setExpanded('run-1', false)
    useTurnStore.getState().replaceSnapshot('room-1', [{ ...snapshot, final_committed: false }])
    expect(useTurnPresentationStore.getState().turns['run-1']).toMatchObject({
      expanded: false, manualAction: 'collapsed',
    })
  })

  it('derives the canonical composer gate only from valid unsettled Turn state', () => {
    const active = finalReadyProjection()['run-1']
    expect(selectCanonicalComposerAuthority({ turns: { 'run-1': active }, order: ['run-1'] }))
      .toMatchObject({ authoritative: true, normalComposerBlocked: true, processing: true })
    const completed = { ...active, state: 'completed' as const, finalCommitted: true }
    expect(selectCanonicalComposerAuthority({ turns: { 'run-1': completed }, order: ['run-1'] }))
      .toMatchObject({ authoritative: true, normalComposerBlocked: false, processing: false })
  })
})

beforeEach(() => {
  useTurnStore.getState().clear()
  useTurnPresentationStore.getState().clear()
})

describe('canonical model_decision activity', () => {
  function decisionTurn(): TurnProjectionMap {
    let turns: TurnProjectionMap = {}
    turns = apply(turns, runEvent(1, 'run_started', {
      hybro_turn_id: 'run-1', user_message_id: 'user-1', started_at: TS, mode: 'fast',
    }))
    turns = apply(turns, runEvent(2, 'turn_start', { internal_turn_id: 'turn-1', attempt: 1 }))
    turns = apply(turns, runEvent(3, 'message_start', {
      internal_turn_id: 'turn-1', message_id: 'assistant-1', role: 'assistant',
    }))
    return turns
  }

  it('folds all five decision kinds into the active turn', () => {
    let turns = decisionTurn()
    turns = apply(turns, runEvent(4, 'model_decision', {
      internal_turn_id: 'turn-1', decision: 'interaction_received',
      agent_label: 'Agent A', question_summary: 'Which?',
    }))
    turns = apply(turns, runEvent(5, 'model_decision', {
      internal_turn_id: 'turn-1', decision: 'answered_from_context',
      agent_label: 'Agent A', question_summary: 'Which?',
      source_summary: 'from earlier messages and attachments',
    }))
    turns = apply(turns, runEvent(6, 'model_decision', {
      internal_turn_id: 'turn-1', decision: 'forwarded_to_user',
      agent_label: 'Agent A', question_summary: 'Which?',
    }))
    turns = apply(turns, runEvent(7, 'model_decision', {
      internal_turn_id: 'turn-1', decision: 'no_progress',
      agent_label: 'Agent A', question_summary: 'Which?',
      reason: 'auto_reply_limit_reached',
    }))
    turns = apply(turns, runEvent(8, 'model_decision', {
      internal_turn_id: 'turn-1', decision: 'degraded_to_user',
      agent_label: 'Agent A', question_summary: 'Which?',
      reason: 'decision_turn_inconclusive',
    }))
    const decisions = turns['run-1'].activity.filter((item) => item.kind === 'decision')
    expect(decisions.map((d) => d.decision)).toEqual([
      'interaction_received', 'answered_from_context', 'forwarded_to_user',
      'no_progress', 'degraded_to_user',
    ])
    expect(decisions[1]).toMatchObject({
      kind: 'decision', decision: 'answered_from_context', agentLabel: 'Agent A',
      sourceSummary: 'from earlier messages and attachments',
    })
    expect(decisions[2]).toMatchObject({ decision: 'forwarded_to_user', agentLabel: 'Agent A' })
    expect(decisions[3].reason).toBe('auto_reply_limit_reached')
  })

  it('deduplicates a decision by event id', () => {
    let turns = decisionTurn()
    const event = runEvent(4, 'model_decision', {
      internal_turn_id: 'turn-1', decision: 'interaction_received',
      agent_label: 'Agent A', question_summary: 'Which?',
    })
    turns = apply(turns, event)
    turns = apply(turns, event)
    expect(turns['run-1'].activity.filter((item) => item.kind === 'decision')).toHaveLength(1)
  })

  it('rejects a decision that does not belong to an active turn', () => {
    const turns = decisionTurn()
    const result = foldCanonicalEvent(turns, 'room-1', {
      kind: 'run_event',
      data: runEvent(4, 'model_decision', {
        internal_turn_id: 'missing-turn', decision: 'interaction_received', agent_label: 'Agent A',
      }),
    })
    expect(result.ok).toBe(false)
  })
})

describe('canonical model_decision contract validation', () => {
  it('accepts valid decision payloads and enforces context fields', () => {
    const base = {
      type: 'model_decision',
      payload: { internal_turn_id: 'turn-1', decision: 'interaction_received' },
    } as const
    expect(validateCanonicalRunEventData(runEvent(1, 'model_decision', base.payload))).toMatchObject({
      canonical: true, valid: true,
    })

    const answered = runEvent(1, 'model_decision', {
      internal_turn_id: 'turn-1', decision: 'answered_from_context',
      agent_label: 'Agent A', question_summary: 'Which?',
    })
    expect(validateCanonicalRunEventData(answered)).toMatchObject({ canonical: true, valid: true })

    // answered_from_context requires agent_label
    const missingLabel = runEvent(1, 'model_decision', {
      internal_turn_id: 'turn-1', decision: 'answered_from_context', question_summary: 'Which?',
    })
    expect(validateCanonicalRunEventData(missingLabel)).toMatchObject({ canonical: true, valid: false })

    // no_progress / degraded_to_user require a reason
    const missingReason = runEvent(1, 'model_decision', {
      internal_turn_id: 'turn-1', decision: 'no_progress', agent_label: 'Agent A',
    })
    expect(validateCanonicalRunEventData(missingReason)).toMatchObject({ canonical: true, valid: false })

    const noProgress = runEvent(1, 'model_decision', {
      internal_turn_id: 'turn-1', decision: 'no_progress', reason: 'auto_reply_limit_reached',
    })
    expect(validateCanonicalRunEventData(noProgress)).toMatchObject({ canonical: true, valid: true })
  })

  it('rejects unknown decision values', () => {
    const event = runEvent(1, 'model_decision', {
      internal_turn_id: 'turn-1', decision: 'not_a_real_decision',
    })
    expect(validateCanonicalRunEventData(event)).toMatchObject({ canonical: true, valid: false })
  })
})

describe('Agent error codes on failed tool executions', () => {
  function startedTool(): TurnProjectionMap {
    let turns: TurnProjectionMap = {}
    turns = apply(turns, runEvent(1, 'run_started', {
      hybro_turn_id: 'run-1', user_message_id: 'user-1', started_at: TS, mode: 'direct',
    }))
    turns = apply(turns, runEvent(2, 'turn_start', { internal_turn_id: 'turn-1', attempt: 1 }))
    return apply(turns, runEvent(3, 'tool_execution_start', {
      internal_turn_id: 'turn-1', tool_call_id: 'inv_weather_0001', tool_name: 'Weather Agent', input: {},
    }))
  }

  const failedEnd = (extra: Record<string, unknown>) => runEvent(4, 'tool_execution_end', {
    internal_turn_id: 'turn-1', tool_call_id: 'inv_weather_0001', tool_name: 'Weather Agent',
    outcome: 'failed', result: '', is_error: true, duration_ms: 3, failure_reason: 'execution', ...extra,
  })

  it('folds the code and message onto the failed tool item', () => {
    const turns = apply(startedTool(), failedEnd({
      error_code: 'llm_quota_exceeded', error_message: 'Quota exceeded',
    }))
    expect(turns['run-1'].activity.find((item) => item.kind === 'tool')).toMatchObject({
      status: 'failed', errorCode: 'llm_quota_exceeded', errorMessage: 'Quota exceeded',
    })
  })

  it('accepts null as absent and rejects error details outside a failure', () => {
    expect(validateCanonicalRunEventData(failedEnd({ error_code: null, error_message: null })))
      .toMatchObject({ valid: true })
    expect(validateCanonicalRunEventData(failedEnd({ error_code: 'bad code' })))
      .toMatchObject({ valid: false })
    expect(validateCanonicalRunEventData(runEvent(4, 'tool_execution_end', {
      internal_turn_id: 'turn-1', tool_call_id: 'inv_weather_0001', tool_name: 'Weather Agent',
      outcome: 'completed', result: 'ok', is_error: false, duration_ms: 3, error_code: 'x',
    }))).toMatchObject({ valid: false })
  })

  it('restores the code from a snapshot tool row', () => {
    const snapshot: RoomSnapshotTurn = {
      hybro_turn_id: 'run-1', run_id: 'run-1', user_message_id: 'user-1', client_request_id: 'client-1',
      state: 'active', started_at: TS, settled_at: null, duration_ms: null,
      terminal_code: null, terminal_summary: null,
      internal_turns: [{
        internal_turn_id: 'turn-1', attempt: 1, message_ids: [],
        tool_call_ids: ['inv_weather_0001'], status: 'active',
      }],
      activity: [{
        kind: 'tool', id: 'inv_weather_0001', internal_turn_id: 'turn-1',
        tool_call_id: 'inv_weather_0001', label: 'Weather Agent', input: {},
        partial_result: '', result: '', is_error: true, duration_ms: 10,
        status: 'failed', update_index: 0, order: 1, failure_reason: 'execution',
        error_code: 'llm_quota_exceeded', error_message: 'Quota exceeded',
      }],
      current_assistant: null, final_answer: null, final_committed: false,
      hitl_interactions: [], active_interaction_id: null, agent_call_message_ids: [],
    }
    expect(validateCanonicalSnapshotTurns({ turn_lifecycle_schema: 1, turns: [snapshot] })).not.toBeNull()
    expect(snapshotTurnToProjection('room-1', snapshot).activity[0]).toMatchObject({
      errorCode: 'llm_quota_exceeded', errorMessage: 'Quota exceeded',
    })
  })
})

describe('settlement of a call a check rule denied', () => {
  const canceled = (extra: Record<string, unknown>) => runEvent(9, 'run_settled', {
    status: 'canceled', started_at: TS, settled_at: TS, duration_ms: 0, ...extra,
  })

  it('accepts the rule id on a policy cancel only', () => {
    expect(validateCanonicalRunEventData(canceled({ cancellation_code: 'policy', rule_id: 'rule_1' })))
      .toMatchObject({ valid: true })
    expect(validateCanonicalRunEventData(canceled({ cancellation_code: 'policy' })))
      .toMatchObject({ valid: true })
    expect(validateCanonicalRunEventData(canceled({ cancellation_code: 'user_requested', rule_id: 'rule_1' })))
      .toMatchObject({ valid: false })
    expect(validateCanonicalRunEventData(canceled({ cancellation_code: 'policy', rule_id: '' })))
      .toMatchObject({ valid: false })
  })
})

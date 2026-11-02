import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { AnySSEFrame, RoomSnapshotTurn } from '@/lib/types/sse'
import { createProcessingLifecycle } from '@/hooks/room/processing-lifecycle'
import { useRoomUiStore } from '@/stores/room-ui-store'
import {
  BOOTSTRAP_SNAPSHOT_MS,
  REORDER_WINDOW_MS,
  RoomReducer,
  applySnapshotToStores,
} from '../room-reducer'
import { useMessageStore } from '@/stores/message-store'
import { useStreamingStore } from '@/stores/streaming-store'
import { selectCanonicalComposerAuthority, useTurnStore } from '@/stores/turn-store'
import { useTurnPresentationStore } from '@/stores/turn-presentation-store'
import {
  hitlQuestionEntityId,
  hitlRequestKey,
} from '@/lib/hitl/hitl-message-projection'

function frame(type: string, data: Record<string, unknown>, roomSeq?: number): AnySSEFrame {
  return {
    type,
    timestamp: '2026-07-02T00:00:00.000Z',
    conversation_id: 'room-1',
    data: roomSeq === undefined ? data : { ...data, room_seq: roomSeq },
  } as AnySSEFrame
}

function makeReducer(onDelta?: (f: AnySSEFrame) => Promise<void>, requestSnapshot?: () => void) {
  const reducer = new RoomReducer({
    conversationId: 'room-1',
    onDelta: onDelta ?? (async () => {}),
    requestSnapshot: requestSnapshot ?? (() => {}),
  })
  return reducer
}

function emptySnapshot(roomSeq: number) {
  return {
    room_seq: roomSeq,
    messages: [],
    tasks: [],
    hitl: { requests: [], resolved: [] },
    streaming: {},
    turn_lifecycle_schema: 1,
    turns: [],
  }
}

function snapshotTurn(state: RoomSnapshotTurn['state']): RoomSnapshotTurn {
  const terminal = state !== 'active'
  return {
    hybro_turn_id: 'run-1', run_id: 'run-1', user_message_id: 'user-1', client_request_id: 'client-1', state,
    started_at: '2030-01-01T00:00:00.000Z',
    settled_at: terminal ? '2030-01-01T00:00:01.000Z' : null,
    duration_ms: terminal ? 1000 : null,
    terminal_code: state === 'canceled' ? 'user_requested' : state === 'failed' ? 'internal_error' : null,
    terminal_summary: state === 'failed' ? 'The run failed.' : null,
    internal_turns: state === 'completed' ? [{
      internal_turn_id: 'turn-1', attempt: 1, message_ids: ['assistant-1'],
      tool_call_ids: [], status: 'completed',
    }] : [],
    activity: [], current_assistant: null,
    final_answer: state === 'completed' ? {
      message_id: 'assistant-1', internal_turn_id: 'turn-1', text: 'Done',
      status: 'completed', order: 1,
    } : null,
    final_committed: state === 'completed', hitl_interactions: [],
    active_interaction_id: null, agent_call_message_ids: [],
  }
}

function cancellingHitlReducer() {
  const lifecycle = createProcessingLifecycle((active) => {
    useRoomUiStore.getState().setProcessing('room-1', active)
  })
  lifecycle.startProcessing('user-1', 'client-1')
  // The real HITL handler retains correlation while clearing the send guard.
  lifecycle.stopProcessing({ clearMessageId: false })
  useRoomUiStore.getState().setCancelling('room-1', true)
  expect(lifecycle.isSendGuardActive()).toBe(false)
  expect(useRoomUiStore.getState().getRoomFlags('room-1')).toMatchObject({
    processing: false, cancelling: true,
  })
  const timeout = vi.fn()
  lifecycle.armCancelTimeout(timeout)
  const placeholderId = lifecycle.placeholderId('room-1')
  useMessageStore.getState().upsertMessage({
    id: placeholderId, conversationId: 'room-1', messageType: 'agent', content: '',
    senderName: 'Assistant', timestamp: '2030-01-01T00:00:00.000Z',
  }, 'sse')
  const reducer = new RoomReducer({
    conversationId: 'room-1', onDelta: async () => {}, requestSnapshot: vi.fn(),
    processingLifecycle: lifecycle,
  })
  return { reducer, lifecycle, timeout, placeholderId }
}

describe('RoomReducer', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    useRoomUiStore.getState().resetAll()
    useMessageStore.getState().clearRoom()
    useStreamingStore.getState().clearRoom('room-1')
    useTurnStore.getState().clear()
    useTurnPresentationStore.getState().clear()
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it.each(['canceled', 'failed', 'completed'] as const)(
    'unlocks a matching %s snapshot after HITL cleared the send guard',
    async (state) => {
      const { reducer, lifecycle, timeout, placeholderId } = cancellingHitlReducer()
      await reducer.handle(frame('connected', { room_seq: 5 }))
      await reducer.handle(frame('snapshot', {
        ...emptySnapshot(5), turns: [snapshotTurn(state)],
      }))

      expect(useTurnStore.getState().rooms['room-1'].turns['run-1'].state).toBe(state)
      expect(selectCanonicalComposerAuthority(useTurnStore.getState().rooms['room-1']).normalComposerBlocked).toBe(false)
      expect(useRoomUiStore.getState().getRoomFlags('room-1').cancelling).toBe(false)
      expect(lifecycle.isProcessingResolved()).toBe(true)
      expect(lifecycle.isSendGuardActive()).toBe(false)
      expect(lifecycle.getMessageId()).toBeNull()
      expect(lifecycle.getClientRequestId()).toBeNull()
      expect(lifecycle.isPlaceholderDismissed()).toBe(true)
      expect(useMessageStore.getState().entities[placeholderId]).toBeUndefined()
      vi.advanceTimersByTime(15001)
      expect(timeout).not.toHaveBeenCalled()
      lifecycle.dispose()
    },
  )

  it.each(['wrong user', 'wrong request', 'nonterminal', 'invalid', 'stale'])(
    'does not clean up the current HITL cancellation for a %s snapshot',
    async (scenario) => {
      const { reducer, lifecycle, timeout, placeholderId } = cancellingHitlReducer()
      await reducer.handle(frame('connected', { room_seq: 5 }))
      await reducer.handle(frame('snapshot', emptySnapshot(5)))
      const turn = snapshotTurn(scenario === 'nonterminal' ? 'active' : 'canceled')
      if (scenario === 'wrong user') turn.user_message_id = 'other-user'
      if (scenario === 'wrong request') turn.client_request_id = 'other-request'
      if (scenario === 'invalid') turn.active_interaction_id = 'missing-interaction'
      await reducer.handle(frame('snapshot', {
        ...emptySnapshot(scenario === 'stale' ? 4 : 6), turns: [turn],
      }))

      if (['wrong user', 'wrong request', 'nonterminal'].includes(scenario)) {
        expect(useTurnStore.getState().rooms['room-1'].turns['run-1'].state).toBe(turn.state)
      } else {
        expect(useTurnStore.getState().rooms['room-1'].order).toEqual([])
      }
      expect(useRoomUiStore.getState().getRoomFlags('room-1').cancelling).toBe(true)
      expect(lifecycle.getMessageId()).toBe('user-1')
      expect(lifecycle.getClientRequestId()).toBe('client-1')
      expect(lifecycle.isSendGuardActive()).toBe(false)
      expect(lifecycle.isProcessingResolved()).toBe(false)
      expect(lifecycle.isPlaceholderDismissed()).toBe(false)
      expect(useMessageStore.getState().entities[placeholderId]).toBeDefined()
      vi.advanceTimersByTime(15001)
      expect(timeout).toHaveBeenCalledOnce()
      lifecycle.dispose()
    },
  )

  it('preserves the unsequenced legacy fold when the handshake has no room_seq', async () => {
    const deltas: string[] = []
    const requests: string[] = []
    const reducer = makeReducer(async (f) => {
      deltas.push(f.type)
    }, () => requests.push('snapshot'))
    await reducer.handle(frame('connected', { connection_id: 'conn-1' }))
    await reducer.handle(frame('task_update', { message_id: 'm1', status: 'working' }))
    expect(reducer.enabled).toBe(false)
    expect(deltas).toEqual(['task_update'])
    expect(requests).toEqual([])
  })

  it('buffers deltas before the first snapshot and replays them in order', async () => {
    const deltas: string[] = []
    const reducer = makeReducer(async (f) => {
      deltas.push(f.type)
    })
    await reducer.handle(frame('connected', { connection_id: 'c', room_seq: 5 }))
    await reducer.handle(frame('task_update', { message_id: 'm1' }, 6))
    await reducer.handle(frame('task_update', { message_id: 'm2' }, 7))
    expect(deltas).toEqual([])

    await reducer.handle(frame('snapshot', emptySnapshot(5)))
    expect(deltas).toEqual(['task_update', 'task_update'])
  })

  it('discards deltas at or below the snapshot watermark', async () => {
    const deltas: string[] = []
    const reducer = makeReducer(async (f) => {
      deltas.push(f.type)
    })
    await reducer.handle(frame('connected', { connection_id: 'c', room_seq: 5 }))
    await reducer.handle(frame('task_update', { message_id: 'stale' }, 4))
    await reducer.handle(frame('snapshot', emptySnapshot(5)))
    expect(deltas).toEqual([])

    await reducer.handle(frame('task_update', { message_id: 'm1' }, 6))
    expect(deltas).toEqual(['task_update'])
  })

  it('buffers out-of-order deltas in the reorder window and drains in order', async () => {
    const deltas: string[] = []
    const reducer = makeReducer(async (f) => {
      deltas.push(`${f.type}:${(f.data as { room_seq?: number }).room_seq}`)
    })
    await reducer.handle(frame('connected', { connection_id: 'c', room_seq: 0 }))
    await reducer.handle(frame('snapshot', emptySnapshot(0)))

    await reducer.handle(frame('task_update', { message_id: 'm2' }, 2))
    expect(deltas).toEqual([]) // gap: seq 1 missing → buffered
    await reducer.handle(frame('task_update', { message_id: 'm1' }, 1))
    expect(deltas).toEqual(['task_update:1', 'task_update:2'])
  })

  it('requests a forced snapshot when a gap outlasts the reorder window', async () => {
    const requests: string[] = []
    const reducer = makeReducer(async () => {}, () => requests.push('force'))
    await reducer.handle(frame('connected', { connection_id: 'c', room_seq: 0 }))
    await reducer.handle(frame('snapshot', emptySnapshot(0)))

    await reducer.handle(frame('task_update', { message_id: 'm2' }, 3))
    expect(requests).toEqual([])
    vi.advanceTimersByTime(REORDER_WINDOW_MS + 1)
    expect(requests).toEqual(['force'])
  })

  it('requests a snapshot when the bootstrap window passes without one', async () => {
    const requests: string[] = []
    const reducer = makeReducer(async () => {}, () => requests.push('force'))
    await reducer.handle(frame('connected', { connection_id: 'c', room_seq: 0 }))
    expect(requests).toEqual([])
    vi.advanceTimersByTime(BOOTSTRAP_SNAPSHOT_MS + 1)
    expect(requests).toEqual(['force'])
  })

  it('requests a snapshot on the first delta before any snapshot', async () => {
    const requests: string[] = []
    const reducer = makeReducer(async () => {}, () => requests.push('force'))
    await reducer.handle(frame('connected', { connection_id: 'c', room_seq: 0 }))
    await reducer.handle(frame('task_update', { message_id: 'm1' }, 1))
    expect(requests).toEqual(['force'])
  })

  it('requests a snapshot when a heartbeat reveals one lost tail event', async () => {
    const requests: string[] = []
    const reducer = makeReducer(async () => {}, () => requests.push('force'))
    await reducer.handle(frame('connected', { connection_id: 'c', room_seq: 3 }))
    await reducer.handle(frame('snapshot', emptySnapshot(3)))

    await reducer.handle(frame('heartbeat', { room_seq: 4 }))
    vi.advanceTimersByTime(400)
    expect(requests).toEqual(['force'])
  })

  it('rejects a stale replacement snapshot before it can regress stores', async () => {
    const requests: string[] = []
    const reducer = makeReducer(async (delta) => {
      const data = delta.data as { message_id?: string }
      if (data.message_id === 'm6') {
        useMessageStore.getState().upsertMessage({
          id: 'm6', conversationId: 'room-1', messageType: 'agent', content: 'newer live value',
          senderName: 'Weather Agent', timestamp: '2030-01-01T00:00:06.000Z',
        }, 'sse')
      }
    }, () => requests.push('force'))
    await reducer.handle(frame('connected', { connection_id: 'c', room_seq: 5 }))
    await reducer.handle(frame('snapshot', emptySnapshot(5)))
    await reducer.handle(frame('task_update', { message_id: 'm6' }, 6))
    expect(useMessageStore.getState().entities.m6?.content).toBe('newer live value')

    await reducer.handle(frame('snapshot', emptySnapshot(4)))

    expect(requests).toEqual(['force'])
    expect(useMessageStore.getState().entities.m6?.content).toBe('newer live value')
    await reducer.handle(frame('task_update', { message_id: 'm7' }, 7))
    expect(requests).toEqual(['force'])
  })

  it('preserves the applied watermark across reconnects and rejects an older bootstrap snapshot', async () => {
    const requests: string[] = []
    const applied: string[] = []
    const reducer = makeReducer(async (delta) => {
      const data = delta.data as { message_id?: string }
      if (data.message_id) applied.push(data.message_id)
      if (data.message_id === 'm6') {
        useMessageStore.getState().upsertMessage({
          id: 'm6', conversationId: 'room-1', messageType: 'agent', content: 'newer live value',
          senderName: 'Weather Agent', timestamp: '2030-01-01T00:00:06.000Z',
        }, 'sse')
      }
    }, () => requests.push('force'))
    await reducer.handle(frame('connected', { connection_id: 'c1', room_seq: 5 }))
    await reducer.handle(frame('snapshot', emptySnapshot(5)))
    await reducer.handle(frame('task_update', { message_id: 'm6' }, 6))

    await reducer.handle(frame('connected', { connection_id: 'c2', room_seq: 6 }))
    await reducer.handle(frame('snapshot', emptySnapshot(5)))

    expect(requests).toEqual(['force'])
    expect(useMessageStore.getState().entities.m6?.content).toBe('newer live value')
    await reducer.handle(frame('snapshot', emptySnapshot(6)))
    await reducer.handle(frame('task_update', { message_id: 'm7' }, 7))
    expect(applied).toEqual(['m6', 'm7'])
  })

  it('requests a snapshot when a heartbeat reveals a gap while idle', async () => {
    const requests: string[] = []
    const reducer = makeReducer(async () => {}, () => requests.push('force'))
    await reducer.handle(frame('connected', { connection_id: 'c', room_seq: 3 }))
    await reducer.handle(frame('snapshot', emptySnapshot(3)))

    await reducer.handle(frame('heartbeat', { room_seq: 6 }))
    expect(requests).toEqual([])
    vi.advanceTimersByTime(400)
    expect(requests).toEqual(['force'])
  })
})

describe('applySnapshotToStores', () => {
  beforeEach(() => {
    useMessageStore.getState().clearRoom()
    useStreamingStore.getState().clearRoom('room-1')
    useTurnStore.getState().clear()
    useTurnPresentationStore.getState().clear()
  })

  it('hydrates a pure legacy snapshot without claiming canonical authority', () => {
    const snapshot = {
      room_seq: 2,
      messages: [{
        message_id: 'legacy-agent-1', agent_id: 'agent-1', agent_name: 'Weather Agent',
        content: 'Sunny', parts: null, related_message_id: 'user-1',
        client_request_id: 'client-1', status: null, task_status: 'completed',
        task_content: null, task_error: null, requires_input: false,
        requires_auth: false, step_number: null, total_steps: null,
        created_at: null, ts: '2030-01-01T00:00:01.000Z', artifacts: null,
        status_logs: [],
      }],
      tasks: [], hitl: { requests: [], resolved: [] }, streaming: {},
    }

    expect(applySnapshotToStores('room-1', snapshot, new Map())).toBe(true)
    expect(useMessageStore.getState().entities['legacy-agent-1']?.content).toBe('Sunny')
    expect(useTurnStore.getState().rooms['room-1']).toBeUndefined()
  })

  it('atomically replaces canonical Turns while preserving presentation state', () => {
    const canonicalSnapshot = {
      room_seq: 5,
      messages: [{
        message_id: 'orchestrator:run-1:inv_weather_0001',
        agent_id: null,
        content: '',
        parts: null,
        related_message_id: 'user-1',
        client_request_id: 'client-1',
        status: null,
        task_status: 'completed',
        task_content: null,
        task_error: null,
        requires_input: false,
        requires_auth: false,
        step_number: null,
        total_steps: null,
        created_at: null,
        ts: '2030-01-01T00:00:01.000Z',
        artifacts: null,
        status_logs: [],
      }],
      tasks: [],
      hitl: { requests: [], resolved: [] },
      streaming: {},
      turn_lifecycle_schema: 1 as const,
      turns: [{
        hybro_turn_id: 'run-1',
        run_id: 'run-1',
        user_message_id: 'user-1',
        client_request_id: 'client-1',
        state: 'active' as const,
        started_at: '2030-01-01T00:00:00.000Z',
        settled_at: null,
        duration_ms: null,
        terminal_code: null,
        terminal_summary: null,
        internal_turns: [],
        activity: [],
        current_assistant: null,
        final_answer: null,
        final_committed: false,
        hitl_interactions: [],
        active_interaction_id: null,
        agent_call_message_ids: ['orchestrator:run-1:inv_weather_0001'],
      }],
    }

    expect(applySnapshotToStores('room-1', canonicalSnapshot)).toBe(true)
    useTurnPresentationStore.getState().setExpanded('run-1', false)
    expect(applySnapshotToStores('room-1', { ...canonicalSnapshot, room_seq: 6 })).toBe(true)

    expect(useTurnStore.getState().rooms['room-1'].turns['run-1']).toMatchObject({
      id: 'run-1', userMessageId: 'user-1', clientRequestId: 'client-1',
      agentCallMessageIds: ['orchestrator:run-1:inv_weather_0001'],
    })
    expect(useTurnPresentationStore.getState().turns['run-1']).toMatchObject({
      expanded: false, manualAction: 'collapsed',
    })
  })

  it('restores canonical snapshot-only HITL requests before DB hydration', () => {
    const snapshot = {
      ...emptySnapshot(3),
      turn_lifecycle_schema: 1 as const,
      turns: [{
        hybro_turn_id: 'run-1', run_id: 'run-1', user_message_id: 'user-1',
        client_request_id: 'client-1', state: 'awaiting_input' as const,
        started_at: '2030-01-01T00:00:00.000Z', settled_at: null, duration_ms: null,
        terminal_code: null, terminal_summary: null, internal_turns: [], activity: [],
        current_assistant: null, final_answer: null, final_committed: false,
        hitl_interactions: [{
          interaction_id: 'interaction-1', state: 'awaiting_input' as const,
          request_ids: ['request-1', 'request-2'], requested_at: '2030-01-01T00:00:01.000Z', resumed_at: null,
          requests: [{
            request_id: 'request-1', message_id: 'hitl-message-1', question_index: 0,
            question_count: 2, prompt: 'Continue?', prompt_type: 'confirmation', choices: [],
            source: 'supervisor', agent_label: null, status: 'requested' as const, answer_ref: null,
          }, {
            request_id: 'request-2', message_id: 'hitl-message-1', question_index: 1,
            question_count: 2, prompt: 'Add details', prompt_type: 'text', choices: [],
            source: 'supervisor', agent_label: null, status: 'requested' as const, answer_ref: null,
          }],
        }],
        active_interaction_id: 'interaction-1', agent_call_message_ids: [],
      }],
      hitl: {
        requests: [{
          room_seq: 3, run_id: 'run-1', request_id: 'request-1', message_id: 'hitl-message-1',
          interaction_id: 'interaction-1', related_user_message_id: 'user-1',
          client_request_id: 'client-1', question_index: 0, question_count: 2,
          prompt: 'Continue?', prompt_type: 'confirmation', source: 'supervisor',
          ts: '2030-01-01T00:00:01.000Z',
        }, {
          room_seq: 3, run_id: 'run-1', request_id: 'request-2', message_id: 'hitl-message-1',
          interaction_id: 'interaction-1', related_user_message_id: 'user-1',
          client_request_id: 'client-1', question_index: 1, question_count: 2,
          prompt: 'Add details', prompt_type: 'text', source: 'supervisor',
          ts: '2030-01-01T00:00:01.000Z',
        }],
        resolved: [],
      },
    }

    const index = new Map<string, string>()
    expect(applySnapshotToStores('room-1', snapshot, index)).toBe(true)
    const firstKey = hitlRequestKey('interaction-1', 'request-1')
    const secondKey = hitlRequestKey('interaction-1', 'request-2')
    const firstId = hitlQuestionEntityId(
      'hitl-message-1', 'interaction-1', 'request-1', 2,
    )
    const secondId = hitlQuestionEntityId(
      'hitl-message-1', 'interaction-1', 'request-2', 2,
    )
    const entities = useMessageStore.getState().entities
    expect(entities['hitl-message-1'].hitlRequestId).toBeUndefined()
    expect(entities[firstId]).toMatchObject({
      hitlRequestId: 'request-1', hitlMessageId: 'hitl-message-1',
      hitlPrompt: 'Continue?', clientRequestId: 'client-1', relatedMessageId: 'user-1',
    })
    expect(entities[secondId]).toMatchObject({
      hitlRequestId: 'request-2', hitlMessageId: 'hitl-message-1',
      hitlPrompt: 'Add details', clientRequestId: 'client-1', relatedMessageId: 'user-1',
    })
    expect(index).toEqual(new Map([
      [firstKey, firstId],
      [secondKey, secondId],
    ]))

    const incompleteInventory = {
      ...snapshot,
      room_seq: 4,
      turns: [{
        ...snapshot.turns[0],
        hitl_interactions: [{
          ...snapshot.turns[0].hitl_interactions[0],
          requests: [snapshot.turns[0].hitl_interactions[0].requests[0]],
        }],
      }],
      hitl: { ...snapshot.hitl, requests: [snapshot.hitl.requests[0]] },
    }
    expect(applySnapshotToStores('room-1', incompleteInventory, index)).toBe(false)
    expect(index).toEqual(new Map([
      [firstKey, firstId],
      [secondKey, secondId],
    ]))
  })

  it('restores a rolling-deploy legacy HITL request against an exact canonical Turn root', () => {
    const snapshot = {
      ...emptySnapshot(9),
      turn_lifecycle_schema: 1 as const,
      turns: [{
        hybro_turn_id: 'run-1', run_id: 'run-1', user_message_id: 'user-1',
        client_request_id: 'client-1', state: 'active' as const,
        started_at: '2030-01-01T00:00:00.000Z', settled_at: null, duration_ms: null,
        terminal_code: null, terminal_summary: null,
        internal_turns: [{
          internal_turn_id: 'turn-1', attempt: 1, message_ids: [],
          tool_call_ids: ['inv_travel_0001'], status: 'active' as const,
        }],
        activity: [{
          kind: 'tool' as const, id: 'inv_travel_0001', internal_turn_id: 'turn-1',
          tool_call_id: 'inv_travel_0001', label: 'Travel Planner Agent', input: {},
          partial_result: '', result: null, is_error: null, duration_ms: null,
          status: 'suspended' as const, update_index: 1, order: 8,
          execution_kind: 'agent' as const, target_name: 'Travel Planner Agent',
          request_summary: 'Plan a trip', detail_available: false,
        }],
        current_assistant: null, final_answer: null, final_committed: false,
        hitl_interactions: [], active_interaction_id: null, agent_call_message_ids: [],
      }],
      hitl: {
        requests: [{
          request_id: 'request-1', message_id: 'orchestrator:run-1:call_private',
          interaction_id: 'interaction-1', interaction_status: 'pending',
          interaction_version: 1, related_message_id: 'user-1',
          client_request_id: 'client-1', question_index: 0, question_count: 1,
          prompt: 'Which island?', prompt_type: 'text', source: 'agent',
          source_step_id: 'private-call-record', ts: '2030-01-01T00:00:01.000Z',
        }],
        resolved: [],
      },
    }

    expect(applySnapshotToStores('room-1', snapshot, new Map())).toBe(true)
    const requestId = hitlQuestionEntityId(
      'orchestrator:run-1:call_private', 'interaction-1', 'request-1', 1,
    )
    expect(useMessageStore.getState().entities[requestId]).toMatchObject({
      senderName: 'Travel Planner Agent',
      hitlRequestId: 'request-1',
      hitlPrompt: 'Which island?',
      clientRequestId: 'client-1',
      relatedMessageId: 'user-1',
    })
    expect(useTurnStore.getState().rooms['room-1'].turns['run-1'].state).toBe('active')
  })

  it('accepts legacy snapshot HITL rows missing new correlation fields', () => {
    const snapshot = {
      ...emptySnapshot(3),
      turn_lifecycle_schema: 1 as const,
      turns: [{
        hybro_turn_id: 'run-1', run_id: 'run-1', user_message_id: 'user-1',
        client_request_id: 'client-1', state: 'active' as const,
        started_at: '2030-01-01T00:00:00.000Z', settled_at: null, duration_ms: null,
        terminal_code: null, terminal_summary: null, internal_turns: [], activity: [],
        current_assistant: null, final_answer: null, final_committed: false,
        hitl_interactions: [], active_interaction_id: null, agent_call_message_ids: [],
      }],
      hitl: {
        requests: [{
          request_id: 'legacy-request',
          message_id: 'orchestrator:run-1:legacy_private',
          interaction_id: 'legacy-interaction', question_index: 0, question_count: 1,
          prompt: 'Legacy prompt?', prompt_type: 'text', source: 'agent',
          ts: '2030-01-01T00:00:01.000Z',
        }],
        resolved: [],
      },
    }

    expect(applySnapshotToStores('room-1', snapshot, new Map())).toBe(true)
    expect(useTurnStore.getState().rooms['room-1'].turns['run-1']).toBeDefined()
  })

  it('rejects canonical snapshot HITL rows missing correlation fields', () => {
    const snapshot = {
      ...emptySnapshot(3),
      turn_lifecycle_schema: 1 as const,
      turns: [{
        hybro_turn_id: 'run-1', run_id: 'run-1', user_message_id: 'user-1',
        client_request_id: 'client-1', state: 'active' as const,
        started_at: '2030-01-01T00:00:00.000Z', settled_at: null, duration_ms: null,
        terminal_code: null, terminal_summary: null, internal_turns: [], activity: [],
        current_assistant: null, final_answer: null, final_committed: false,
        hitl_interactions: [], active_interaction_id: null, agent_call_message_ids: [],
      }],
      hitl: {
        requests: [{
          room_seq: 3, run_id: 'run-1', request_id: 'canonical-request',
          message_id: 'orchestrator:run-1:inv_request_0001',
          interaction_id: 'canonical-interaction', question_index: 0, question_count: 1,
          prompt: 'Canonical prompt?', prompt_type: 'text', source: 'agent',
          ts: '2030-01-01T00:00:01.000Z',
        }],
        resolved: [],
      },
    }

    expect(applySnapshotToStores('room-1', snapshot, new Map())).toBe(false)
    expect(useTurnStore.getState().rooms['room-1']).toBeUndefined()
  })

  it('keeps canceled rolling-deploy HITL requests out of the composer projection', () => {
    const snapshot = {
      ...emptySnapshot(9),
      turn_lifecycle_schema: 1 as const,
      turns: [{
        hybro_turn_id: 'run-1', run_id: 'run-1', user_message_id: 'user-1',
        client_request_id: 'client-1', state: 'active' as const,
        started_at: '2030-01-01T00:00:00.000Z', settled_at: null, duration_ms: null,
        terminal_code: null, terminal_summary: null, internal_turns: [], activity: [],
        current_assistant: null, final_answer: null, final_committed: false,
        hitl_interactions: [], active_interaction_id: null, agent_call_message_ids: [],
      }],
      hitl: {
        requests: [{
          request_id: 'request-1', message_id: 'orchestrator:run-1:call_private',
          interaction_id: 'interaction-1', interaction_status: 'canceled',
          related_message_id: 'user-1', client_request_id: 'client-1',
          question_index: 0, question_count: 1, prompt: 'Which island?',
          prompt_type: 'text', source: 'agent', status: 'canceled',
          ts: '2030-01-01T00:00:01.000Z',
        }],
        resolved: [],
      },
    }

    expect(applySnapshotToStores('room-1', snapshot, new Map())).toBe(true)
    const requestId = hitlQuestionEntityId(
      'orchestrator:run-1:call_private', 'interaction-1', 'request-1', 1,
    )
    expect(useMessageStore.getState().entities[requestId]).toMatchObject({
      hitlRequestId: 'request-1', hitlResolved: true,
    })
  })

  it('does not apply a canonical HITL request that contradicts an existing entity root', () => {
    useMessageStore.getState().setRoom('room-1')
    useMessageStore.getState().upsertMessage({
      id: 'orchestrator:run-1:inv_request_0001', conversationId: 'room-1',
      messageType: 'agent', content: '', senderName: 'Agent',
      timestamp: '2030-01-01T00:00:01.000Z', clientRequestId: 'client-1',
      relatedMessageId: 'user-1',
    }, 'sse')
    const snapshot = {
      ...emptySnapshot(3),
      turn_lifecycle_schema: 1 as const,
      turns: [{
        hybro_turn_id: 'run-1', run_id: 'run-1', user_message_id: 'other-user',
        client_request_id: 'other-client', state: 'active' as const,
        started_at: '2030-01-01T00:00:00.000Z', settled_at: null, duration_ms: null,
        terminal_code: null, terminal_summary: null, internal_turns: [], activity: [],
        current_assistant: null, final_answer: null, final_committed: false,
        hitl_interactions: [], active_interaction_id: null, agent_call_message_ids: [],
      }],
      hitl: {
        requests: [{
          room_seq: 3, run_id: 'run-1', request_id: 'request-1',
          message_id: 'orchestrator:run-1:inv_request_0001',
          interaction_id: 'interaction-1', related_user_message_id: 'other-user',
          client_request_id: 'other-client', question_index: 0, question_count: 1,
          prompt: 'Wrong root?', prompt_type: 'text', source: 'agent',
          ts: '2030-01-01T00:00:01.000Z',
        }],
        resolved: [],
      },
    }
    const index = new Map<string, string>()

    expect(applySnapshotToStores('room-1', snapshot, index)).toBe(false)
    expect(useMessageStore.getState().entities['orchestrator:run-1:inv_request_0001']?.hitlRequestId).toBeUndefined()
    expect(useTurnStore.getState().rooms['room-1']).toBeUndefined()
    expect(index.size).toBe(0)
  })

  it('rejects a canonical top-level HITL prompt absent from the Turn inventory', () => {
    const snapshot = {
      ...emptySnapshot(4),
      turn_lifecycle_schema: 1 as const,
      turns: [{
        hybro_turn_id: 'run-1', run_id: 'run-1', user_message_id: 'user-1',
        client_request_id: 'client-1', state: 'active' as const,
        started_at: '2030-01-01T00:00:00.000Z', settled_at: null, duration_ms: null,
        terminal_code: null, terminal_summary: null, internal_turns: [], activity: [],
        current_assistant: null, final_answer: null, final_committed: false,
        hitl_interactions: [], active_interaction_id: null, agent_call_message_ids: [],
      }],
      hitl: {
        requests: [{
          room_seq: 4, run_id: 'run-1', request_id: 'phantom-request',
          message_id: 'orchestrator:run-1:inv_agent', interaction_id: 'phantom-interaction',
          related_user_message_id: 'user-1', client_request_id: 'client-1',
          question_index: 0, question_count: 1, prompt: 'Phantom?',
          prompt_type: 'text', source: 'agent', status: 'requested',
          ts: '2030-01-01T00:00:01.000Z',
        }],
        resolved: [],
      },
    }

    expect(applySnapshotToStores('room-1', snapshot, new Map())).toBe(false)
    expect(useTurnStore.getState().rooms['room-1']).toBeUndefined()
    expect(useMessageStore.getState().entities['orchestrator:run-1:inv_agent']).toBeUndefined()
  })

  it('rejects malformed canonical snapshot capability without mutating the Turn projection', () => {
    expect(applySnapshotToStores('room-1', {
      ...emptySnapshot(2),
      turns: undefined,
    } as never)).toBe(false)
    expect(useTurnStore.getState().rooms['room-1']).toBeUndefined()
  })

  it('folds snapshot messages and streams into the stores', () => {
    applySnapshotToStores('room-1', {
      room_seq: 3,
      turn_lifecycle_schema: 1,
      turns: [],
      messages: [
        {
          message_id: 'm1',
          agent_id: 'a1',
          agent_name: 'Weather Agent',
          content: 'Hello',
          parts: null,
          related_message_id: null,
          client_request_id: 'cr-1',
          status: null,
          task_status: 'completed',
          task_content: null,
          task_error: null,
          requires_input: false,
          requires_auth: false,
          step_number: null,
          total_steps: null,
          created_at: null,
          ts: '2026-07-02T00:00:00.000Z',
          artifacts: null,
          status_logs: [],
        },
      ],
      tasks: [],
      hitl: { requests: [], resolved: [] },
      streaming: {
        m2: {
          message_id: 'm2',
          agent_id: 'a2',
          text: 'Par',
          artifacts: [],
          is_complete: false,
          client_request_id: 'cr-2',
          last_chunk: false,
        },
      },
    })

    const messages = useMessageStore.getState().entities
    expect(messages['m1']).toMatchObject({
      messageType: 'agent',
      content: 'Hello',
      agentId: 'a1',
      senderName: 'Weather Agent',
      taskStatus: 'completed',
      clientRequestId: 'cr-1',
    })
    const buffers = useStreamingStore.getState().buffers
    expect(buffers['m2']?.text).toBe('Par')
  })

  it('retains the turn terminal status when snapshot arrives before DB hydration', () => {
    applySnapshotToStores('room-1', {
      room_seq: 2,
      turn_lifecycle_schema: 1,
      turns: [],
      messages: [
        {
          message_id: 'user-1',
          agent_id: null,
          content: null,
          parts: null,
          related_message_id: null,
          client_request_id: 'cr-1',
          status: 'completed',
          task_status: null,
          task_content: null,
          task_error: null,
          requires_input: false,
          requires_auth: false,
          step_number: null,
          total_steps: null,
          created_at: null,
          ts: null,
          artifacts: null,
          status_logs: [
            {
              message: 'Planning the next actions',
              timestamp: '2026-07-02T00:00:01.000Z',
              turn_phase: 'collecting',
            },
          ],
        },
      ],
      tasks: [],
      hitl: { requests: [], resolved: [] },
      streaming: {},
    })

    expect(useMessageStore.getState().entities['user-1']).toMatchObject({
      messageType: 'user',
      clientRequestId: 'cr-1',
      turnTerminalStatus: 'completed',
    })

    useMessageStore.getState().upsertMessage({
      id: 'user-1',
      conversationId: 'room-1',
      messageType: 'user',
      content: 'Actual persisted prompt',
      senderName: 'Developer Local',
      timestamp: '2026-07-02T00:00:00.000Z',
    }, 'db')

    expect(useMessageStore.getState().entities['user-1']).toMatchObject({
      content: 'Actual persisted prompt',
      turnTerminalStatus: 'completed',
    })
  })
})

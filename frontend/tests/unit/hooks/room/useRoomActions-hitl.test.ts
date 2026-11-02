import { beforeEach, describe, expect, it, vi } from 'vitest'
import { renderHook } from '@testing-library/react'
import { ApiError } from '@/lib/api-client'
import { useRoomActions } from '@/hooks/room/useRoomActions'
import { useMessageStore } from '@/stores/message-store'
import { useRoomUiStore } from '@/stores/room-ui-store'
import { selectCanonicalComposerAuthority, useTurnStore } from '@/stores/turn-store'
import { createProcessingLifecycle } from '@/hooks/room/processing-lifecycle'
import { createSSEDispatcher } from '@/hooks/room/sse-handlers/dispatch'
import { hitlQuestionEntityId, hitlRequestKey } from '@/lib/hitl/hitl-message-projection'
import { selectPendingHitls } from '@/lib/selectors/select-hitl'
import type { AnySSEFrame } from '@/lib/types/sse'
import type { HitlRespondResponse } from '@/lib/api/hitl'

const mocks = vi.hoisted(() => ({
  submit: vi.fn(),
  cancel: vi.fn(),
  hydrate: vi.fn(),
}))

vi.mock('@/lib/api/hitl', () => ({
  respondToHitlBatch: mocks.submit,
  cancelHitl: mocks.cancel,
}))
vi.mock('@/lib/room-sync/hydrate-room', () => ({
  hydrateRoomFromDb: mocks.hydrate,
}))

function seedHitl(version = 1) {
  const store = useMessageStore.getState()
  store.clearRoom()
  store.setRoom('room-1')
  store.upsertMessage({
    id: 'hitl-1', conversationId: 'room-1', messageType: 'agent', content: '',
    senderName: 'Agent', timestamp: '2030-01-01T00:00:00.000Z',
    hitlRequestId: 'request-1', hitlPrompt: 'Question?', hitlPromptType: 'text',
    hitlChoices: [], hitlInteractionId: 'interaction-1',
    hitlInteractionVersion: version, hitlInteractionStatus: 'open',
    hitlResolved: false, relatedMessageId: 'user-1', clientRequestId: 'client-1',
  }, 'db')
}

const successfulHydration = {
  rawCount: 0,
  filteredCount: 0,
  appliedCount: 0,
  pendingHitlCount: 0,
  fetchFailed: false,
  hitlFetchFailed: false,
}

function renderActions(
  reconcile = vi.fn().mockResolvedValue(undefined),
  requestSnapshot = vi.fn(),
) {
  const lifecycle = {
    resetPlaceholder: vi.fn(), resetProcessingResolved: vi.fn(),
    setPendingRunEventAck: vi.fn(), placeholderId: vi.fn(() => 'placeholder'),
    startProcessing: vi.fn(), getMessageId: vi.fn(), setCancelTimedOut: vi.fn(),
    markProcessingResolved: vi.fn(), stopProcessing: vi.fn(), disarmCancelTimeout: vi.fn(),
    armCancelTimeout: vi.fn(),
  }
  const hitlRequestIndex = { current: new Map<string, string>() }
  const setCancelling = vi.fn()
  return {
    reconcile,
    lifecycle,
    setCancelling,
    hook: renderHook(() => useRoomActions(
      'room-1', async () => null, lifecycle as never,
      hitlRequestIndex, reconcile, setCancelling, true, vi.fn(),
      async () => 'Agent', () => 'local', requestSnapshot,
    )),
    requestSnapshot,
  }
}

describe('useRoomActions HITL conflict recovery', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    seedHitl()
    mocks.submit.mockRejectedValue(new ApiError(409, 'Conflict'))
    mocks.cancel.mockResolvedValue({
      status: 'canceled',
      interaction_id: 'interaction-1',
      interaction_version: 2,
    })
    mocks.hydrate.mockResolvedValue(successfulHydration)
  })

  it('fails closed when the authoritative pending overlay reports fetch failure', async () => {
    mocks.hydrate.mockResolvedValue({ ...successfulHydration, hitlFetchFailed: true })
    const reconcile = vi.fn().mockResolvedValue(undefined)
    const { hook, requestSnapshot } = renderActions(reconcile)

    await expect(hook.result.current.respondToHitlBatch(
      'interaction-1', [{ requestId: 'request-1', answer: 'A' }], 'client-1',
    )).rejects.toThrow('Authoritative HITL pending refresh failed.')
    expect(reconcile).toHaveBeenCalledWith('room-1')
    expect(requestSnapshot).toHaveBeenCalledTimes(1)
    expect(mocks.hydrate).toHaveBeenCalledWith(expect.objectContaining({
      conversationId: 'room-1', phase: 'hitl_overlay',
    }))

    await expect(hook.result.current.respondToHitlBatch(
      'interaction-1', [{ requestId: 'request-1', answer: 'A' }], 'client-1',
    )).rejects.toThrow('Authoritative HITL pending refresh failed.')
    expect(mocks.submit).toHaveBeenCalledTimes(2)
  })

  it('does not accept a conflict when the authoritative revision changed', async () => {
    mocks.hydrate.mockImplementation(async () => {
      useMessageStore.getState().upsertMessage({
        id: 'hitl-1', conversationId: 'room-1', messageType: 'agent', content: '',
        senderName: 'Agent', timestamp: '2030-01-01T00:00:01.000Z',
        hitlRequestId: 'request-1', hitlPrompt: 'Question?', hitlPromptType: 'text',
        hitlChoices: [], hitlInteractionId: 'interaction-1',
        hitlInteractionVersion: 2, hitlInteractionStatus: 'responded',
        hitlApplicationStatus: 'applied', hitlResolved: true,
      }, 'sse')
      return successfulHydration
    })
    const { hook, requestSnapshot } = renderActions()

    await expect(hook.result.current.respondToHitlBatch(
      'interaction-1', [{ requestId: 'request-1', answer: 'A' }], 'client-1',
    )).rejects.toMatchObject({ status: 409 })
    expect(requestSnapshot).toHaveBeenCalledTimes(1)
  })

  it('still rethrows a typed conflict after a successful overlay refresh', async () => {
    mocks.hydrate.mockImplementation(async () => {
      useMessageStore.getState().upsertMessage({
        id: 'hitl-1', conversationId: 'room-1', messageType: 'agent', content: '',
        senderName: 'Agent', timestamp: '2030-01-01T00:00:01.000Z',
        hitlRequestId: 'request-1', hitlPrompt: 'Question?', hitlPromptType: 'text',
        hitlChoices: [], hitlInteractionId: 'interaction-1',
        hitlInteractionVersion: 1, hitlInteractionStatus: 'responded',
        hitlApplicationStatus: 'applied', hitlResolved: true,
      }, 'sse')
      return successfulHydration
    })
    const { hook, requestSnapshot } = renderActions()

    await expect(hook.result.current.respondToHitlBatch(
      'interaction-1', [{ requestId: 'request-1', answer: 'A' }], 'client-1',
    )).rejects.toMatchObject({ status: 409 })
    expect(requestSnapshot).toHaveBeenCalledTimes(1)
    expect(useMessageStore.getState().entities['hitl-1']).toMatchObject({
      hitlResolved: true,
    })

    await expect(hook.result.current.respondToHitlBatch(
      'interaction-1', [{ requestId: 'request-1', answer: 'A' }], 'client-1',
    )).rejects.toMatchObject({ status: 409 })
    expect(mocks.submit).toHaveBeenCalledTimes(2)
  })
})

describe('useRoomActions HITL cancellation convergence', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    seedHitl()
    mocks.cancel.mockResolvedValue({
      status: 'canceled',
      interaction_id: 'interaction-1',
      interaction_version: 2,
    })
  })

  it('sets cancelling before the request and leaves terminal cleanup to authority', async () => {
    const { hook, lifecycle, setCancelling, reconcile, requestSnapshot } = renderActions()
    mocks.cancel.mockImplementation(async () => {
      expect(setCancelling).toHaveBeenCalledWith(true)
      expect(lifecycle.setCancelTimedOut).toHaveBeenCalledWith(false)
      return {
        status: 'canceled',
        interaction_id: 'interaction-1',
        interaction_version: 2,
      }
    })

    await hook.result.current.cancelHitlRequest('request-1', 'interaction-1')

    expect(mocks.cancel).toHaveBeenCalledTimes(1)
    expect(setCancelling).toHaveBeenCalledTimes(1)
    expect(reconcile).toHaveBeenCalledWith('room-1')
    expect(requestSnapshot).toHaveBeenCalledTimes(1)
    expect(lifecycle.markProcessingResolved).not.toHaveBeenCalled()
    expect(lifecycle.stopProcessing).not.toHaveBeenCalled()
    expect(useMessageStore.getState().entities['hitl-1']).toMatchObject({
      hitlResolved: true,
      hitlInteractionStatus: 'canceled',
      taskStatus: 'canceled',
    })
  })

  it('clears cancelling after a failed request and conflict reconciliation', async () => {
    mocks.cancel.mockRejectedValue(new ApiError(409, 'Conflict'))
    const { hook, setCancelling, reconcile, requestSnapshot } = renderActions()

    await expect(hook.result.current.cancelHitlRequest(
      'request-1', 'interaction-1',
    )).rejects.toMatchObject({ status: 409 })

    expect(reconcile).toHaveBeenCalledWith('room-1')
    expect(setCancelling.mock.calls).toEqual([[true], [false]])
    expect(requestSnapshot).not.toHaveBeenCalled()
  })

  it('clears cancelling after a non-conflict request failure', async () => {
    mocks.cancel.mockRejectedValue(new Error('network unavailable'))
    const { hook, setCancelling, reconcile } = renderActions()

    await expect(hook.result.current.cancelHitlRequest(
      'request-1', 'interaction-1',
    )).rejects.toThrow('network unavailable')

    expect(reconcile).not.toHaveBeenCalled()
    expect(setCancelling.mock.calls).toEqual([[true], [false]])
  })

  it('keeps accepted cancellation successful when recovery fails', async () => {
    const reconcile = vi.fn().mockRejectedValue(new Error('reconcile failed'))
    const requestSnapshot = vi.fn(() => {
      throw new Error('snapshot failed')
    })
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => undefined)
    const { hook, setCancelling } = renderActions(reconcile, requestSnapshot)

    await expect(hook.result.current.cancelHitlRequest(
      'request-1', 'interaction-1',
    )).resolves.toBeUndefined()

    expect(setCancelling.mock.calls).toEqual([[true]])
    expect(requestSnapshot).toHaveBeenCalledTimes(1)
    expect(consoleError).toHaveBeenCalledTimes(2)
    consoleError.mockRestore()
  })
})

const timestamp = '2030-01-01T00:00:00.000Z'
const questionMessageId = 'orchestrator:run-1:call-1'
const questionEntityId = hitlQuestionEntityId(questionMessageId, 'interaction-1', 'request-1', 1)

async function setupDeferredAck(canonical = true) {
  const lifecycle = createProcessingLifecycle(value => {
    useRoomUiStore.getState().setProcessing('room-1', value)
  })
  const hitlRequestIndex = { current: new Map<string, string>() }
  const requestSnapshot = vi.fn()
  const setCancelling = (value: boolean) => useRoomUiStore.getState().setCancelling('room-1', value)
  const dispatch = createSSEDispatcher({
    conversationId: 'room-1', lifecycle, hitlRequestIndex, setCancelling,
    getAgentName: async () => 'Agent', getAgentSource: () => 'local',
    reconcileWithDb: vi.fn(), requestSnapshotRef: { current: requestSnapshot },
  })
  let seq = 0
  const frame = async (type: string, data: Record<string, unknown>) => {
    await dispatch({
      type, conversation_id: 'room-1', timestamp,
      data: { room_seq: ++seq, ...data },
    } as AnySSEFrame)
    expect(requestSnapshot).not.toHaveBeenCalled()
  }
  const runEvent = (type: string, payload: Record<string, unknown>, root = '1') => frame('run_event', {
    event_id: `event-${seq + 1}`, run_id: `run-${root}`, seq: seq + 1,
    correlation_id: `client-${root}`, type, payload,
  })
  if (canonical) {
    await dispatch({
      type: 'connected', conversation_id: 'room-1', timestamp,
      data: { connection_id: 'connection-1', room_seq: 0 },
    })
    await dispatch({
      type: 'snapshot', conversation_id: 'room-1', timestamp,
      data: {
        room_seq: 0, messages: [], tasks: [], streaming: {},
        hitl: { requests: [], resolved: [] }, turn_lifecycle_schema: 1, turns: [],
      },
    } as AnySSEFrame)
  }
  useMessageStore.getState().upsertMessage({
    id: 'user-1', conversationId: 'room-1', messageType: 'user', content: 'Run this',
    senderName: 'You', timestamp, clientRequestId: 'client-1',
  }, 'db')
  lifecycle.startProcessing('user-1', 'client-1')
  if (canonical) {
    await runEvent('run_started', {
      hybro_turn_id: 'run-1', user_message_id: 'user-1', started_at: timestamp, mode: 'supervisor',
    })
  }
  const hitlIdentity = (interactionId: string) => ({
    ...(canonical ? { run_id: 'run-1', related_user_message_id: 'user-1' } : { related_message_id: 'user-1' }),
    request_id: 'request-1', message_id: questionMessageId, interaction_id: interactionId,
    client_request_id: 'client-1', question_index: 0, question_count: 1, source: 'agent',
  })
  const requestInput = async (interactionId: string, waiting = true) => {
    await frame('hitl_request', {
      ...hitlIdentity(interactionId), prompt: 'Question?', prompt_type: 'text',
    })
    if (canonical && waiting) {
      await runEvent('run_waiting_input', {
        interaction_id: interactionId, request_ids: ['request-1'], requested_at: timestamp,
      })
    }
  }
  await requestInput('interaction-1')
  expect(lifecycle.isProcessingResolved()).toBe(true)
  expect(lifecycle.isSendGuardActive()).toBe(false)

  let resolveAck!: (response: HitlRespondResponse) => void
  const response = new Promise<HitlRespondResponse>(resolve => { resolveAck = resolve })
  mocks.submit.mockReturnValue(response)
  const hook = renderHook(() => useRoomActions(
    'room-1', async () => null, lifecycle, hitlRequestIndex, vi.fn(), setCancelling, true, vi.fn(),
  ))
  const submission = hook.result.current.respondToHitlBatch(
    'interaction-1', [{ requestId: 'request-1', answer: 'A' }], 'client-1',
  )
  await vi.waitFor(() => expect(mocks.submit).toHaveBeenCalledOnce())
  const acknowledge = async (status = 'applied') => {
    resolveAck({ status, request_id: 'request-1', interaction_id: 'interaction-1' })
    await submission
  }
  const resume = async () => {
    await frame('hitl_response', { ...hitlIdentity('interaction-1'), status: 'responded', answer_ref: 'answer-1' })
    await runEvent('run_resumed', {
      interaction_id: 'interaction-1', resolved_request_ids: ['request-1'], resumed_at: timestamp,
    })
  }
  return { lifecycle, hitlRequestIndex, frame, runEvent, requestInput, resume, acknowledge }
}

function processingState() {
  const authority = selectCanonicalComposerAuthority(useTurnStore.getState().rooms['room-1'])
  // ComposerShell retains the OR with the legacy room processing flag.
  return authority.processing || useRoomUiStore.getState().getRoomFlags('room-1').processing
}

function expectConfirmedAnswer() {
  expect(useMessageStore.getState().entities[questionEntityId]).toMatchObject({
    hitlResolved: true, hitlUserAnswer: 'A', hitlInteractionStatus: 'applied',
  })
}

describe('useRoomActions HITL deferred ACK authority', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    useMessageStore.getState().setRoom('room-1')
    useRoomUiStore.getState().resetAll()
    useTurnStore.getState().clear()
  })

  it.each(['applied', 'responded'])('does not restart Finished/Stop when terminal SSE precedes %s ACK', async status => {
    const { lifecycle, frame, runEvent, resume, acknowledge } = await setupDeferredAck()
    await resume()
    await runEvent('turn_start', { internal_turn_id: 'turn-1', attempt: 1 })
    await runEvent('message_start', { internal_turn_id: 'turn-1', message_id: 'final-1', role: 'assistant' })
    await runEvent('message_end', {
      internal_turn_id: 'turn-1', message_id: 'final-1', text: 'Done', stop_reason: 'stop', disposition: 'final',
    })
    await frame('agent_response', {
      message_id: 'final-1', related_message_id: 'user-1', client_request_id: 'client-1', content: 'Done',
    })
    await runEvent('turn_end', { internal_turn_id: 'turn-1', message_id: 'final-1', tool_call_ids: [], status: 'completed' })
    await runEvent('run_settled', {
      started_at: timestamp, settled_at: timestamp, duration_ms: 1000, status: 'completed', final_message_id: 'final-1',
    })
    expect(processingState()).toBe(false)
    await acknowledge(status)

    expect(useTurnStore.getState().rooms['room-1'].turns['run-1'].state).toBe('completed')
    expect(processingState()).toBe(false)
    expect(lifecycle.isSendGuardActive()).toBe(false)
    expect(lifecycle.isProcessingResolved()).toBe(true)
    expect(lifecycle.isPlaceholderDismissed()).toBe(true)
    expect(lifecycle.getMessageId()).toBeNull()
    expect(lifecycle.getClientRequestId()).toBeNull()
    expect(lifecycle.getPendingRunEventAck()).toBeNull()
    expectConfirmedAnswer()
  })

  it.each(['failed', 'canceled'])('does not restart a %s canonical root after an applied ACK', async status => {
    const { lifecycle, runEvent, resume, acknowledge } = await setupDeferredAck()
    await resume()
    await runEvent('run_settled', {
      started_at: timestamp, settled_at: timestamp, duration_ms: 1000, status,
      ...(status === 'failed'
        ? { failure_code: 'hitl_error', error_summary: 'Continuation failed' }
        : { cancellation_code: 'user_requested' }),
    })
    await acknowledge()

    expect(useTurnStore.getState().rooms['room-1'].turns['run-1'].state).toBe(status)
    expect(processingState()).toBe(false)
    expect(lifecycle.isSendGuardActive()).toBe(false)
    expect(lifecycle.isProcessingResolved()).toBe(true)
    expect(lifecycle.getMessageId()).toBeNull()
    expect(lifecycle.getPendingRunEventAck()).toBeNull()
    expectConfirmedAnswer()
  })

  it.each([false, true])('preserves a follow-up HITL arriving before the old ACK (waiting control: %s)', async waiting => {
    const { lifecycle, hitlRequestIndex, requestInput, resume, acknowledge } = await setupDeferredAck()
    await resume()
    await requestInput('interaction-2', waiting)
    await acknowledge()

    const turn = useTurnStore.getState().rooms['room-1'].turns['run-1']
    expect(turn.state).toBe(waiting ? 'awaiting_input' : 'active')
    expect(useRoomUiStore.getState().getRoomFlags('room-1').processing).toBe(false)
    expect(lifecycle.isSendGuardActive()).toBe(false)
    expect(lifecycle.isProcessingResolved()).toBe(true)
    expect(lifecycle.isPlaceholderDismissed()).toBe(true)
    expect(lifecycle.getPendingRunEventAck()).toBeNull()
    expect(lifecycle.getMessageId()).toBe('user-1')
    const store = useMessageStore.getState()
    expect(selectPendingHitls('room-1', store.entities, store.orderedIds).map(hitl => hitl.interactionId))
      .toEqual(['interaction-2'])
    expect(hitlRequestIndex.current.get(hitlRequestKey('interaction-2', 'request-1')))
      .toBe(hitlQuestionEntityId(questionMessageId, 'interaction-2', 'request-1', 1))
    expectConfirmedAnswer()
  })

  it.each([false, true])('resumes the current pending interaction (run_resumed already delivered: %s)', async resumed => {
    const { lifecycle, resume, acknowledge } = await setupDeferredAck()
    if (resumed) await resume()
    await acknowledge()

    expect(processingState()).toBe(true)
    expect(lifecycle.isSendGuardActive()).toBe(true)
    expect(lifecycle.isProcessingResolved()).toBe(false)
    expect(lifecycle.isPlaceholderDismissed()).toBe(false)
    expect(lifecycle.getMessageId()).toBe('user-1')
    expect(lifecycle.getClientRequestId()).toBe('client-1')
    expect(lifecycle.getPendingRunEventAck()).toBe('client-1')
    expectConfirmedAnswer()
  })

  it.each([
    ['user-2', 'client-2'], ['user-1', 'client-2'], ['user-2', 'client-1'],
  ])('does not clear or replace a newer processing root %s/%s', async (messageId, clientId) => {
    const { lifecycle, frame, acknowledge } = await setupDeferredAck()
    lifecycle.resetProcessingResolved()
    lifecycle.startProcessing(messageId, clientId)
    await frame('run_event', {
      event_id: 'new-run-start', run_id: 'run-2', seq: 1, correlation_id: clientId,
      type: 'run_started', payload: {
        hybro_turn_id: 'run-2', user_message_id: messageId, started_at: timestamp, mode: 'supervisor',
      },
    })
    lifecycle.setPendingRunEventAck(clientId)
    const flags = useRoomUiStore.getState().getRoomFlags('room-1')
    const turns = useTurnStore.getState().rooms['room-1']
    await acknowledge()

    expect(useRoomUiStore.getState().getRoomFlags('room-1')).toEqual(flags)
    expect(useTurnStore.getState().rooms['room-1']).toBe(turns)
    expect(lifecycle.isSendGuardActive()).toBe(true)
    expect(lifecycle.isProcessingResolved()).toBe(false)
    expect(lifecycle.getMessageId()).toBe(messageId)
    expect(lifecycle.getClientRequestId()).toBe(clientId)
    expect(lifecycle.getPendingRunEventAck()).toBe(clientId)
    expectConfirmedAnswer()
  })

  it('does not write an old ACK into a newly selected room', async () => {
    const { lifecycle, acknowledge } = await setupDeferredAck()
    useMessageStore.getState().setRoom('room-2')
    lifecycle.reset()
    lifecycle.startProcessing('user-2', 'client-2')
    lifecycle.setPendingRunEventAck('client-2')
    const state = useMessageStore.getState()
    await acknowledge()

    expect(useMessageStore.getState()).toBe(state)
    expect(lifecycle.getMessageId()).toBe('user-2')
    expect(lifecycle.getClientRequestId()).toBe('client-2')
    expect(lifecycle.getPendingRunEventAck()).toBe('client-2')
    expect(lifecycle.isSendGuardActive()).toBe(true)
  })

  it('keeps a pending cancellation intact after an applied ACK', async () => {
    const { lifecycle, acknowledge } = await setupDeferredAck()
    useRoomUiStore.getState().setCancelling('room-1', true)
    await acknowledge()

    expect(useRoomUiStore.getState().getRoomFlags('room-1')).toMatchObject({ cancelling: true, processing: false })
    expect(lifecycle.isProcessingResolved()).toBe(true)
    expect(lifecycle.isSendGuardActive()).toBe(false)
    expect(lifecycle.getPendingRunEventAck()).toBeNull()
    expectConfirmedAnswer()
  })

  it('preserves legacy current-run resume without canonical authority', async () => {
    const { lifecycle, acknowledge } = await setupDeferredAck(false)
    await acknowledge()

    expect(useTurnStore.getState().rooms['room-1']).toBeUndefined()
    expect(processingState()).toBe(true)
    expect(lifecycle.isSendGuardActive()).toBe(true)
    expect(lifecycle.getMessageId()).toBe('user-1')
    expect(lifecycle.getClientRequestId()).toBe('client-1')
    expectConfirmedAnswer()
  })
})

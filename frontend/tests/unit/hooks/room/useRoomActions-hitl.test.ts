import { beforeEach, describe, expect, it, vi } from 'vitest'
import { renderHook } from '@testing-library/react'
import { ApiError } from '@/lib/api-client'
import { useRoomActions } from '@/hooks/room/useRoomActions'
import { useMessageStore } from '@/stores/message-store'

const mocks = vi.hoisted(() => ({
  submit: vi.fn(),
  hydrate: vi.fn(),
}))

vi.mock('@/lib/api/hitl', () => ({
  respondToHitlBatch: mocks.submit,
  cancelHitl: vi.fn(),
}))
vi.mock('@/lib/room-sync/hydrate-room', () => ({
  hydrateRoomFromDb: mocks.hydrate,
}))

function seedHitl(version = 1) {
  const store = useMessageStore.getState()
  store.clearRoom()
  store.setRoom('room-1')
  store.upsertMessage({
    id: 'hitl-1', roomId: 'room-1', messageType: 'agent', content: '',
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
  return {
    reconcile,
    hook: renderHook(() => useRoomActions(
      'room-1', async () => null, lifecycle as never,
      hitlRequestIndex, reconcile, vi.fn(), true, vi.fn(),
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
      roomId: 'room-1', phase: 'hitl_overlay',
    }))

    await expect(hook.result.current.respondToHitlBatch(
      'interaction-1', [{ requestId: 'request-1', answer: 'A' }], 'client-1',
    )).rejects.toThrow('Authoritative HITL pending refresh failed.')
    expect(mocks.submit).toHaveBeenCalledTimes(2)
  })

  it('does not accept a conflict when the authoritative revision changed', async () => {
    mocks.hydrate.mockImplementation(async () => {
      useMessageStore.getState().upsertMessage({
        id: 'hitl-1', roomId: 'room-1', messageType: 'agent', content: '',
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
        id: 'hitl-1', roomId: 'room-1', messageType: 'agent', content: '',
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

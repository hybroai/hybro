import { beforeEach, describe, expect, it, vi } from 'vitest'
import { renderHook } from '@testing-library/react'
import { useRoomActions } from '@/hooks/room/useRoomActions'
import { useMessageStore } from '@/stores/message-store'
import { useRoomUiStore } from '@/stores/room-ui-store'
import type { ProcessingLifecycle } from '@/hooks/room/processing-lifecycle'

const mocks = vi.hoisted(() => ({
  cancelMessage: vi.fn(),
  warning: vi.fn(),
}))

vi.mock('@/lib/api/sse', () => ({ cancelMessage: mocks.cancelMessage }))
vi.mock('@/components/ui/banner', () => ({
  banner: { warning: mocks.warning, error: vi.fn() },
}))

function createLifecycle(): ProcessingLifecycle {
  return {
    setProcessing: vi.fn(),
    startProcessing: vi.fn(),
    stopProcessing: vi.fn(),
    setPendingRunEventAck: vi.fn(),
    getPendingRunEventAck: vi.fn(() => null),
    clearPendingRunEventAck: vi.fn(),
    setSendGuard: vi.fn(),
    isSendGuardActive: vi.fn(() => true),
    setMessageId: vi.fn(),
    getMessageId: vi.fn(() => 'user-1'),
    getClientRequestId: vi.fn(() => 'client-1'),
    dismissPlaceholder: vi.fn(),
    resetPlaceholder: vi.fn(),
    isPlaceholderDismissed: vi.fn(() => false),
    markProcessingResolved: vi.fn(),
    resetProcessingResolved: vi.fn(),
    isProcessingResolved: vi.fn(() => false),
    placeholderId: vi.fn(() => 'processing-placeholder-room-1'),
    armCancelTimeout: vi.fn(),
    disarmCancelTimeout: vi.fn(),
    hasCancelTimedOut: vi.fn(() => false),
    setCancelTimedOut: vi.fn(),
    markSseDisconnection: vi.fn(),
    clearSseDisconnection: vi.fn(),
    hadSseDisconnection: vi.fn(() => false),
    reset: vi.fn(),
    dispose: vi.fn(),
  }
}

function setup(reconcileWithDb = vi.fn().mockResolvedValue(undefined)) {
  const lifecycle = createLifecycle()
  const requestSnapshot = vi.fn()
  const setCancelling = vi.fn((value: boolean) => {
    useRoomUiStore.getState().setCancelling('room-1', value)
  })
  const hook = renderHook(() => useRoomActions(
    'room-1',
    async () => null,
    lifecycle,
    { current: new Map() },
    reconcileWithDb,
    setCancelling,
    true,
    vi.fn(),
    undefined,
    undefined,
    requestSnapshot,
  ))
  return { hook, lifecycle, requestSnapshot, setCancelling }
}

describe('useRoomActions cancellation', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    useMessageStore.getState().clearRoom()
    useMessageStore.getState().setRoom('room-1')
    useRoomUiStore.getState().resetAll()
    useMessageStore.getState().upsertMessage({
      id: 'user-1',
      roomId: 'room-1',
      messageType: 'user',
      content: 'Run this',
      senderName: 'User',
      timestamp: '2030-01-01T00:00:00.000Z',
      clientRequestId: 'client-1',
    }, 'db')
    useMessageStore.getState().upsertMessage({
      id: 'agent-1',
      roomId: 'room-1',
      messageType: 'agent',
      content: '',
      senderName: 'Agent',
      timestamp: '2030-01-01T00:00:01.000Z',
      taskStatus: 'working',
    }, 'sse')
  })

  it.each([
    ['pending_reconciliation', 'cancellation_pending'],
    ['canceled', 'canceled'],
  ] as const)(
    'preserves correlation and processing until terminal Run state for %s',
    async (outcome, status) => {
      mocks.cancelMessage.mockResolvedValue({
        success: true,
        message_id: 'user-1',
        message: 'Stopping',
        status,
        outcome,
      })
    const { hook, lifecycle, requestSnapshot, setCancelling } = setup()

    await expect(hook.result.current.cancelProcessing()).resolves.toBe(true)

    expect(setCancelling).toHaveBeenLastCalledWith(true)
    expect(lifecycle.getMessageId()).toBe('user-1')
    expect(lifecycle.getClientRequestId()).toBe('client-1')
    expect(lifecycle.markProcessingResolved).not.toHaveBeenCalled()
    expect(lifecycle.stopProcessing).not.toHaveBeenCalled()
    expect(lifecycle.disarmCancelTimeout).not.toHaveBeenCalled()
    expect(lifecycle.armCancelTimeout).toHaveBeenCalledOnce()
    expect(requestSnapshot).toHaveBeenCalledOnce()
    expect(useMessageStore.getState().entities['agent-1'].taskStatus).toBe('working')
    expect(useMessageStore.getState().entities['user-1'].turnTerminalStatus).toBeUndefined()
      expect(
        useMessageStore.getState().entities['user-1'].processingStatusLogs
          ?.map(entry => entry.message),
      ).toContain('Stopping...')
    },
  )

  it('keeps an accepted Stop pending when best-effort reconciliation fails', async () => {
    mocks.cancelMessage.mockResolvedValue({
      success: true,
      message_id: 'user-1',
      message: 'pending',
      status: 'cancellation_pending',
      outcome: 'pending_reconciliation',
    })
    const reconcileWithDb = vi.fn().mockRejectedValue(new Error('offline'))
    const { hook, lifecycle, setCancelling } = setup(reconcileWithDb)

    await expect(hook.result.current.cancelProcessing()).resolves.toBe(true)

    expect(setCancelling).not.toHaveBeenCalledWith(false)
    expect(lifecycle.stopProcessing).not.toHaveBeenCalled()
    expect(lifecycle.getMessageId()).toBe('user-1')
  })
})

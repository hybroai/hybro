import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, renderHook, waitFor } from '@testing-library/react'
import { useProcessingRestore } from '@/hooks/room/useProcessingRestore'
import type { ProcessingLifecycle } from '@/hooks/room/processing-lifecycle'
import { inquiryActiveRuns } from '@/lib/api/room'
import { useMessageStore } from '@/stores/message-store'
import { useRoomUiStore } from '@/stores/room-ui-store'

vi.mock('@/lib/api/room', () => ({
  inquiryActiveRuns: vi.fn(),
}))

function createLifecycle({
  placeholderDismissed,
  processingResolved,
  messageId = null,
}: {
  placeholderDismissed: boolean
  processingResolved: boolean
  messageId?: string | null
}): ProcessingLifecycle {
  return {
    setProcessing: vi.fn(),
    startProcessing: vi.fn(),
    stopProcessing: vi.fn(),
    setPendingRunEventAck: vi.fn(),
    getPendingRunEventAck: vi.fn(() => null),
    clearPendingRunEventAck: vi.fn(),
    setSendGuard: vi.fn(),
    isSendGuardActive: vi.fn(() => false),
    setMessageId: vi.fn(),
    getMessageId: vi.fn(() => messageId),
    getClientRequestId: vi.fn(() => null),
    dismissPlaceholder: vi.fn(),
    resetPlaceholder: vi.fn(),
    isPlaceholderDismissed: vi.fn(() => placeholderDismissed),
    markProcessingResolved: vi.fn(),
    resetProcessingResolved: vi.fn(),
    isProcessingResolved: vi.fn(() => processingResolved),
    placeholderId: vi.fn((roomId: string) => `processing-placeholder-${roomId}`),
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

describe('useProcessingRestore', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    useMessageStore.getState().clearRoom()
    useMessageStore.getState().setRoom('room-1')
    useMessageStore.getState().markDbSynced()
    useRoomUiStore.getState().resetAll()
    useMessageStore.getState().upsertMessage({
      id: 'msg-active',
      roomId: 'room-1',
      messageType: 'user',
      content: 'Continue work',
      senderName: 'User',
      timestamp: new Date().toISOString(),
      clientRequestId: 'client-request-active',
    }, 'db')
  })

  afterEach(() => {
    cleanup()
  })

  it('restores the initial processing log when placeholder was dismissed but processing is not resolved', async () => {
    const lifecycle = createLifecycle({
      placeholderDismissed: true,
      processingResolved: false,
    })

    renderHook(() => useProcessingRestore(
      'room-1',
      { active_runs: [{ state: 'running', trigger_message_id: 'msg-active' }] },
      false,
      lifecycle,
      undefined,
    ))

    await waitFor(() => {
      expect(useMessageStore.getState().entities['msg-active'].processingStatusLogs?.map((entry) => entry.message)).toEqual([
        'Thinking...',
      ])
    })
    expect(lifecycle.startProcessing).toHaveBeenCalledWith(
      'msg-active',
      'client-request-active',
    )
  })

  it('restores a canceling run with stopping state and durable correlation', async () => {
    const lifecycle = createLifecycle({
      placeholderDismissed: false,
      processingResolved: false,
    })

    renderHook(() => useProcessingRestore(
      'room-1',
      { active_runs: [{ state: 'canceling', trigger_message_id: 'msg-active' }] },
      false,
      lifecycle,
      undefined,
    ))

    await waitFor(() => {
      expect(lifecycle.startProcessing).toHaveBeenCalledWith(
        'msg-active',
        'client-request-active',
      )
      expect(useRoomUiStore.getState().getRoomFlags('room-1').cancelling).toBe(true)
      expect(
        useMessageStore.getState().entities['msg-active'].processingStatusLogs
          ?.map(entry => entry.message),
      ).toContain('Stopping...')
    })
    expect(lifecycle.stopProcessing).not.toHaveBeenCalled()
  })

  it('does not stop a live SSE lifecycle when the room active run snapshot is stale', async () => {
    useMessageStore.getState().upsertMessage({
      id: 'msg-live-sse',
      roomId: 'room-1',
      messageType: 'user',
      content: 'Live SSE turn',
      senderName: 'User',
      timestamp: new Date().toISOString(),
      processingStatusLogs: [{
        id: 'processing-log-live',
        message: 'Working...',
        timestamp: new Date().toISOString(),
      }],
    }, 'sse')

    const lifecycle = createLifecycle({
      placeholderDismissed: false,
      processingResolved: false,
      messageId: 'msg-live-sse',
    })

    renderHook(() => useProcessingRestore(
      'room-1',
      { active_runs: [] },
      false,
      lifecycle,
      undefined,
    ))

    await waitFor(() => {
      expect(lifecycle.stopProcessing).not.toHaveBeenCalled()
    })
  })

  it('reconciles and stops a failed live lifecycle when backend has no active run', async () => {
    useMessageStore.getState().upsertMessage({
      id: 'msg-live-failed',
      roomId: 'room-1',
      messageType: 'user',
      content: 'What content in this pdf?',
      senderName: 'User',
      timestamp: new Date().toISOString(),
      processingStatusLogs: [{
        id: 'processing-log-live',
        message: 'Planning...',
        timestamp: new Date().toISOString(),
      }],
    }, 'sse')

    const lifecycle = createLifecycle({
      placeholderDismissed: false,
      processingResolved: false,
      messageId: 'msg-live-failed',
    })
    const getToken = vi.fn().mockResolvedValue('token')
    const reconcileWithDb = vi.fn(async () => {
      const entity = useMessageStore.getState().entities['msg-live-failed']
      useMessageStore.getState().upsertMessage({
        id: entity.id,
        roomId: entity.roomId,
        messageType: 'user',
        content: entity.content,
        senderName: entity.senderName,
        timestamp: entity.timestamp,
        turnTerminalStatus: 'failed',
      }, 'db')
    })
    vi.mocked(inquiryActiveRuns).mockResolvedValue({
      room_id: 'room-1',
      active_runs: [],
      success: true,
      status_code: 200,
    })

    renderHook(() => useProcessingRestore(
      'room-1',
      { active_runs: [] },
      false,
      lifecycle,
      getToken,
      reconcileWithDb,
    ))

    await waitFor(() => {
      expect(reconcileWithDb).toHaveBeenCalledWith('room-1')
      expect(lifecycle.stopProcessing).toHaveBeenCalled()
    })
    expect(inquiryActiveRuns).toHaveBeenCalledWith(
      'room-1',
      getToken,
      undefined,
      'msg-live-failed',
    )
    expect(lifecycle.markProcessingResolved).toHaveBeenCalled()
  })

  it('does not let a stale reconciliation stop a newer lifecycle', async () => {
    useMessageStore.getState().upsertMessage({
      id: 'msg-live-failed',
      roomId: 'room-1',
      messageType: 'user',
      content: 'What content is in this PDF?',
      senderName: 'User',
      timestamp: new Date().toISOString(),
      processingStatusLogs: [{
        id: 'processing-log-live',
        message: 'Planning...',
        timestamp: new Date().toISOString(),
      }],
    }, 'sse')

    const lifecycle = createLifecycle({
      placeholderDismissed: false,
      processingResolved: false,
      messageId: 'msg-live-failed',
    })
    const getToken = vi.fn().mockResolvedValue('token')
    let finishReconciliation: (() => void) | undefined
    let reconciliationCompleted = false
    const reconciliationGate = new Promise<void>((resolve) => {
      finishReconciliation = resolve
    })
    const reconcileWithDb = vi.fn(async () => {
      const entity = useMessageStore.getState().entities['msg-live-failed']
      useMessageStore.getState().upsertMessage({
        id: entity.id,
        roomId: entity.roomId,
        messageType: 'user',
        content: entity.content,
        senderName: entity.senderName,
        timestamp: entity.timestamp,
        turnTerminalStatus: 'failed',
      }, 'db')
      await reconciliationGate
      reconciliationCompleted = true
    })
    vi.mocked(inquiryActiveRuns).mockResolvedValue({
      room_id: 'room-1',
      active_runs: [],
      success: true,
      status_code: 200,
    })

    renderHook(() => useProcessingRestore(
      'room-1',
      { active_runs: [] },
      false,
      lifecycle,
      getToken,
      reconcileWithDb,
    ))

    await waitFor(() => {
      expect(reconcileWithDb).toHaveBeenCalledWith('room-1')
    })

    vi.mocked(lifecycle.getMessageId).mockReturnValue('msg-new-turn')
    finishReconciliation?.()

    await waitFor(() => {
      expect(reconciliationCompleted).toBe(true)
    })
    expect(lifecycle.markProcessingResolved).not.toHaveBeenCalled()
    expect(lifecycle.stopProcessing).not.toHaveBeenCalled()
  })
})

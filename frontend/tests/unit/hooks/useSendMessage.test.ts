import { act, cleanup, renderHook } from '@testing-library/react'
import { QueryClient } from '@tanstack/react-query'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { RoomHistoryResponse } from '@/lib/api/room'
import { roomHistoryQueryKey } from '@/lib/room-history-query'
import { MAX_QUOTE_TEXT_LENGTH, type QuoteData } from '@/lib/types/quote'
import { useMessageStore } from '@/stores/message-store'
import { useRoomUiStore } from '@/stores/room-ui-store'
import type { ProcessingLifecycle } from '@/hooks/room/processing-lifecycle'
import type { PendingAttachment } from '@/lib/types/attachments'
import { useSendMessage } from '@/hooks/room/useSendMessage'

const { sendMessageMock } = vi.hoisted(() => ({
  sendMessageMock: vi.fn(),
}))

let activeQueryClient: QueryClient

vi.mock('@/components/providers/query-provider', () => ({
  getActiveQueryClient: () => activeQueryClient,
}))

vi.mock('@/lib/api/room', async importOriginal => {
  const original = await importOriginal<typeof import('@/lib/api/room')>()
  return {
    ...original,
    SendMessage: (...args: unknown[]) => sendMessageMock(...args),
  }
})

vi.mock('sonner', () => ({
  toast: { error: vi.fn(), info: vi.fn(), success: vi.fn(), warning: vi.fn() },
}))

vi.mock('@/lib/api/files', () => ({
  uploadFile: vi.fn(async (file: File) => ({
    file_id: `file-${file.name}`,
    file_url: `/files/${file.name}`,
    mime_type: file.type,
    file_name: file.name,
    size_bytes: file.size,
  })),
}))

vi.mock('@/hooks/room/sse-handlers/pending-turn-buffer', () => ({
  clearPendingSseForClientRequest: vi.fn(),
  resolveClientRequestMessageId: vi.fn(),
}))

function createLifecycle(): ProcessingLifecycle {
  return {
    isSendGuardActive: vi.fn(() => false),
    getMessageId: vi.fn(),
    resetPlaceholder: vi.fn(),
    resetProcessingResolved: vi.fn(),
    setPendingRunEventAck: vi.fn(),
    placeholderId: vi.fn((conversationId: string) => `placeholder:${conversationId}`),
    startProcessing: vi.fn(),
    setSendGuard: vi.fn(),
    stopProcessing: vi.fn(),
    setMessageId: vi.fn(),
    isProcessingResolved: vi.fn(() => false),
    setCancelTimedOut: vi.fn(),
    clearSseDisconnection: vi.fn(),
    disarmCancelTimeout: vi.fn(),
  } as unknown as ProcessingLifecycle
}

const initialHistory: RoomHistoryResponse = {
  items: [
    {
      conversation_id: 'room-1',
      title: 'Room 1',
      last_activity_at: '2026-08-01T00:00:00.000Z',
      is_pinned: false,
      pin_order: null,
      status: 'idle',
    },
    {
      conversation_id: 'room-2',
      title: 'Room 2',
      last_activity_at: '2026-08-02T00:00:00.000Z',
      is_pinned: false,
      pin_order: null,
      status: 'awaiting_input',
    },
  ],
}

describe('useSendMessage room-history rollback', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    activeQueryClient = new QueryClient()
    activeQueryClient.setQueryData(roomHistoryQueryKey('user-1'), initialHistory)
    useMessageStore.getState().clearRoom()
    useMessageStore.getState().setRoom('room-1')
    useRoomUiStore.getState().resetAll()
  })

  afterEach(() => {
    cleanup()
    activeQueryClient.clear()
  })

  async function send(overrides: { quoteData?: QuoteData; pendingAttachments?: PendingAttachment[] } = {}) {
    const lifecycle = createLifecycle()
    const { result } = renderHook(() => useSendMessage(
      'room-1',
      'user-1',
      'User',
      { conversation_id: 'room-1' },
      async () => 'token',
      false,
      true,
      lifecycle,
      vi.fn(),
      vi.fn(),
      vi.fn().mockResolvedValue(undefined),
    ))

    let sent: boolean | undefined
    await act(async () => {
      sent = await result.current.sendUserMessage({
        userInput: 'Hello',
        quoteData: overrides.quoteData,
        pendingAttachments: overrides.pendingAttachments,
        mode: 'direct',
        agentScope: { source: 'room_default' },
      })
    })
    return sent
  }

  it('cancels an in-flight history fetch and refreshes after a successful send', async () => {
    const cancelSpy = vi.spyOn(activeQueryClient, 'cancelQueries')
    const invalidateSpy = vi.spyOn(activeQueryClient, 'invalidateQueries')
    sendMessageMock.mockResolvedValueOnce({ success: true, message_id: 'message-1' })

    expect(await send()).toBe(true)

    const history = activeQueryClient.getQueryData<RoomHistoryResponse>(
      roomHistoryQueryKey('user-1'),
    )
    expect(history?.items.find(item => item.conversation_id === 'room-1')).toMatchObject({
      status: 'processing',
    })
    expect(cancelSpy).toHaveBeenCalledWith({
      queryKey: roomHistoryQueryKey('user-1'),
      exact: true,
    })
    expect(invalidateSpy).toHaveBeenCalledWith({
      queryKey: roomHistoryQueryKey('user-1'),
      exact: true,
    })
  })

  it('restores history when sending throws', async () => {
    sendMessageMock.mockRejectedValueOnce(new Error('network failure'))

    expect(await send()).toBe(false)
    expect(activeQueryClient.getQueryData(roomHistoryQueryKey('user-1'))).toEqual(initialHistory)
  })

  it('rolls back only activity fields when another room update happens concurrently', async () => {
    sendMessageMock.mockImplementationOnce(async () => {
      activeQueryClient.setQueryData<RoomHistoryResponse>(
        roomHistoryQueryKey('user-1'),
        history => history ? {
          items: history.items.map(item => item.conversation_id === 'room-1'
            ? { ...item, title: 'Renamed concurrently' }
            : item),
        } : history,
      )
      throw new Error('network failure')
    })

    expect(await send()).toBe(false)

    expect(activeQueryClient.getQueryData<RoomHistoryResponse>(
      roomHistoryQueryKey('user-1'),
    )?.items.find(item => item.conversation_id === 'room-1')).toEqual({
      ...initialHistory.items[0],
      title: 'Renamed concurrently',
    })
  })

  it('keeps attachment previews on a failed send so the composer can restore them', async () => {
    sendMessageMock.mockRejectedValueOnce(new Error('network failure'))
    // jsdom has no revokeObjectURL; install a recorder for this test only.
    const originalRevoke = URL.revokeObjectURL
    const revokeSpy = vi.fn()
    URL.revokeObjectURL = revokeSpy
    const attachment: PendingAttachment = {
      id: 'att-1',
      file: new File(['data'], 'photo.png', { type: 'image/png' }),
      previewUrl: 'blob:photo',
      status: 'pending',
    }

    try {
      expect(await send({ pendingAttachments: [attachment] })).toBe(false)
      expect(revokeSpy).not.toHaveBeenCalled()
    } finally {
      URL.revokeObjectURL = originalRevoke
    }
  })

  it('restores history when the response has no message id', async () => {
    sendMessageMock.mockResolvedValueOnce({ success: true })

    expect(await send()).toBe(false)
    expect(activeQueryClient.getQueryData(roomHistoryQueryKey('user-1'))).toEqual(initialHistory)
  })

  it('sends every quote as a structured quote without plain display fields', async () => {
    sendMessageMock.mockResolvedValueOnce({ success: true, message_id: 'message-1' })

    expect(await send({
      quoteData: {
        content: '  Sunny, 21C  ',
        messageId: 'orchestrator:run-1:inv_weather_0001',
        senderName: 'Weather Agent',
        sourceKind: 'agent',
      },
    })).toBe(true)

    const params = sendMessageMock.mock.calls[0][0]
    expect(params.structuredQuote).toEqual({
      text: 'Sunny, 21C',
      source_message_id: 'orchestrator:run-1:inv_weather_0001',
      source_kind: 'agent',
      sender_display_name: 'Weather Agent',
      source_agent_id: null,
    })
    expect(params).not.toHaveProperty('quotedText')
    expect(params).not.toHaveProperty('quotedSenderName')
    expect(params).not.toHaveProperty('relatedMessageId')
  })

  it('restores history when the quote is too long', async () => {
    expect(await send({
      quoteData: {
        content: 'x'.repeat(MAX_QUOTE_TEXT_LENGTH + 1),
        messageId: 'quoted-message',
        senderName: 'Agent',
      },
    })).toBe(false)
    expect(sendMessageMock).not.toHaveBeenCalled()
    expect(activeQueryClient.getQueryData(roomHistoryQueryKey('user-1'))).toEqual(initialHistory)
  })
})

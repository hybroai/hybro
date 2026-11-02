import { describe, expect, it, beforeEach } from 'vitest'
import { applyDbMessages } from '@/lib/room-sync/apply-db-messages'
import { useMessageStore } from '@/stores/message-store'
import { useStreamingStore } from '@/stores/streaming-store'
import { createUserMessage, resetCounters } from '../../../fixtures'

describe('applyDbMessages', () => {
  beforeEach(() => {
    resetCounters()
    useMessageStore.getState().clearRoom()
    useStreamingStore.setState({ buffers: {} })
  })

  it('returns null when store conversationId does not match', () => {
    useMessageStore.getState().setRoom('room-a')
    const result = applyDbMessages('room-b', [
      createUserMessage({ id: 'u1', conversationId: 'room-b' }),
    ])
    expect(result).toBeNull()
  })

  it('upserts messages and returns applied ids', () => {
    useMessageStore.getState().setRoom('room-1')
    const result = applyDbMessages('room-1', [
      createUserMessage({ id: 'u1', conversationId: 'room-1', content: 'hello' }),
    ])
    expect(result?.appliedCount).toBe(1)
    expect(useMessageStore.getState().entities['u1']?.content).toBe('hello')
  })
})

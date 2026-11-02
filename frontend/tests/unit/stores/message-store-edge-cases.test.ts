import { describe, it, expect, beforeEach, vi, afterEach } from 'vitest'
import { useMessageStore } from '@/stores/message-store'
import { TASK_STATE } from '@/lib/types/sse'
import {
  createMessage,
  createTaskMessage,
  createWorkingTask,
  createCompletedTask,
  createMessageBatch,
  createEphemeralMessage,
  resetCounters,
} from '../../fixtures'

describe('useMessageStore - Edge Cases', () => {
  beforeEach(() => {
    useMessageStore.getState().clearRoom()
    resetCounters()
  })

  describe('concurrent upserts from different sources', () => {
    it('should handle SSE update arriving before DB hydration', () => {
      const store = useMessageStore.getState()

      store.upsertMessage(
        createTaskMessage(TASK_STATE.COMPLETED, {
          id: 'msg-1',
          content: 'SSE completed content',
        }),
        'sse'
      )

      store.upsertMessage(
        createTaskMessage(TASK_STATE.WORKING, {
          id: 'msg-1',
          content: 'DB stale content',
        }),
        'db'
      )

      const state = useMessageStore.getState()
      expect(state.entities['msg-1'].taskStatus).toBe(TASK_STATE.COMPLETED)
      expect(state.entities['msg-1'].content).toBe('SSE completed content')
    })

    it('should handle optimistic update followed by SSE confirmation', () => {
      const store = useMessageStore.getState()

      store.upsertMessage(
        createMessage({
          id: 'msg-1',
          content: 'Optimistic content',
          isEphemeral: true,
        }),
        'optimistic'
      )

      store.upsertMessage(
        createMessage({
          id: 'msg-1',
          content: 'Confirmed content',
          isEphemeral: false,
        }),
        'sse'
      )

      const state = useMessageStore.getState()
      expect(state.entities['msg-1'].content).toBe('Confirmed content')
      expect(state.entities['msg-1'].isEphemeral).toBe(false)
    })

    it('should preserve SSE task status when DB returns stale data', () => {
      const store = useMessageStore.getState()

      store.upsertMessage(
        createTaskMessage(TASK_STATE.WORKING, { id: 'task-1' }),
        'sse'
      )

      store.upsertMessage(
        createTaskMessage(TASK_STATE.COMPLETED, {
          id: 'task-1',
          content: 'Final result',
        }),
        'sse'
      )

      store.upsertMessage(
        createTaskMessage(TASK_STATE.WORKING, { id: 'task-1' }),
        'db'
      )

      const state = useMessageStore.getState()
      expect(state.entities['task-1'].taskStatus).toBe(TASK_STATE.COMPLETED)
    })
  })

  describe('large message batches', () => {
    it('should handle batch of 100 messages correctly', () => {
      const store = useMessageStore.getState()
      const messages = createMessageBatch(100)

      store.upsertMany(messages, 'db')

      const state = useMessageStore.getState()
      expect(Object.keys(state.entities)).toHaveLength(100)
      expect(state.orderedIds).toHaveLength(100)
    })

    it('should maintain correct order for large batches', () => {
      const store = useMessageStore.getState()
      const messages = createMessageBatch(50)

      store.upsertMany(messages, 'db')

      const state = useMessageStore.getState()
      for (let i = 1; i < state.orderedIds.length; i++) {
        const prevMsg = state.entities[state.orderedIds[i - 1]]
        const currMsg = state.entities[state.orderedIds[i]]
        expect(new Date(prevMsg.timestamp).getTime())
          .toBeLessThanOrEqual(new Date(currMsg.timestamp).getTime())
      }
    })

    it('should handle mixed insert and update in large batch', () => {
      const store = useMessageStore.getState()

      const initialMessages = createMessageBatch(25)
      store.upsertMany(initialMessages, 'db')

      const mixedBatch = [
        ...initialMessages.slice(0, 10).map(m => ({
          ...m,
          content: 'Updated content',
        })),
        ...createMessageBatch(15),
      ]

      store.upsertMany(mixedBatch, 'sse')

      const state = useMessageStore.getState()
      expect(Object.keys(state.entities)).toHaveLength(40)
    })
  })

  describe('stale task detection edge cases', () => {
    it('should not mark ephemeral tasks as stale', () => {
      const store = useMessageStore.getState()

      store.upsertMessage(
        createEphemeralMessage({
          id: 'ephemeral-task',
          taskStatus: TASK_STATE.WORKING,
        }),
        'optimistic'
      )

      store.cancelAllNonTerminal('room-1')

      const state = useMessageStore.getState()
      expect(state.entities['ephemeral-task'].taskStatus).toBe(TASK_STATE.WORKING)
    })

    it('should handle task state transitions correctly', () => {
      const store = useMessageStore.getState()

      store.upsertMessage(
        createTaskMessage(TASK_STATE.SUBMITTED, { id: 'task-1' }),
        'sse'
      )
      expect(useMessageStore.getState().entities['task-1'].taskStatus).toBe(TASK_STATE.SUBMITTED)

      store.upsertMessage(
        createTaskMessage(TASK_STATE.WORKING, { id: 'task-1' }),
        'sse'
      )
      expect(useMessageStore.getState().entities['task-1'].taskStatus).toBe(TASK_STATE.WORKING)

      store.upsertMessage(
        createTaskMessage(TASK_STATE.COMPLETED, {
          id: 'task-1',
          content: 'Done',
        }),
        'sse'
      )
      expect(useMessageStore.getState().entities['task-1'].taskStatus).toBe(TASK_STATE.COMPLETED)
    })

    it('should cancel only non-terminal tasks in specific room', () => {
      const store = useMessageStore.getState()

      store.upsertMessage(
        createTaskMessage(TASK_STATE.WORKING, {
          id: 'task-r1-1',
          conversationId: 'room-1',
        }),
        'sse'
      )
      store.upsertMessage(
        createTaskMessage(TASK_STATE.WORKING, {
          id: 'task-r2-1',
          conversationId: 'room-2',
        }),
        'sse'
      )
      store.upsertMessage(
        createCompletedTask({
          id: 'task-r1-2',
          conversationId: 'room-1',
        }),
        'sse'
      )

      store.cancelAllNonTerminal('room-1')

      const state = useMessageStore.getState()
      expect(state.entities['task-r1-1'].taskStatus).toBe(TASK_STATE.CANCELED)
      expect(state.entities['task-r2-1'].taskStatus).toBe(TASK_STATE.WORKING)
      expect(state.entities['task-r1-2'].taskStatus).toBe(TASK_STATE.COMPLETED)
    })
  })

  describe('message ordering edge cases', () => {
    it('should handle messages with same timestamp', () => {
      const store = useMessageStore.getState()
      const sameTimestamp = new Date().toISOString()

      store.upsertMany([
        createMessage({ id: 'msg-a', timestamp: sameTimestamp }),
        createMessage({ id: 'msg-b', timestamp: sameTimestamp }),
        createMessage({ id: 'msg-c', timestamp: sameTimestamp }),
      ], 'db')

      const state = useMessageStore.getState()
      expect(state.orderedIds).toHaveLength(3)
    })

    it('should handle out-of-order message insertion', () => {
      const store = useMessageStore.getState()

      const now = Date.now()
      store.upsertMessage(
        createMessage({
          id: 'msg-3',
          timestamp: new Date(now + 2000).toISOString(),
        }),
        'sse'
      )
      store.upsertMessage(
        createMessage({
          id: 'msg-1',
          timestamp: new Date(now).toISOString(),
        }),
        'sse'
      )
      store.upsertMessage(
        createMessage({
          id: 'msg-2',
          timestamp: new Date(now + 1000).toISOString(),
        }),
        'sse'
      )

      const state = useMessageStore.getState()
      expect(state.orderedIds).toEqual(['msg-1', 'msg-2', 'msg-3'])
    })
  })

  describe('version tracking', () => {
    it('should increment version on actual changes', () => {
      const store = useMessageStore.getState()

      const v0 = useMessageStore.getState().version
      store.upsertMessage(createMessage({ id: 'msg-1' }), 'sse')
      const v1 = useMessageStore.getState().version

      expect(v1).toBe(v0 + 1)
    })

    it('should not increment version on no-op updates', () => {
      const store = useMessageStore.getState()

      const msg = createMessage({ id: 'msg-1', content: 'Same content' })
      store.upsertMessage(msg, 'sse')
      const v1 = useMessageStore.getState().version

      store.upsertMessage({ ...msg }, 'sse')
      const v2 = useMessageStore.getState().version

      expect(v2).toBe(v1)
    })

    it('should increment version once for batch operations', () => {
      const store = useMessageStore.getState()

      const v0 = useMessageStore.getState().version
      store.upsertMany(createMessageBatch(10), 'db')
      const v1 = useMessageStore.getState().version

      expect(v1).toBe(v0 + 1)
    })
  })

  describe('optimistic id replacement', () => {
    it('replaces optimistic id with real id and rewires relatedMessageId links', () => {
      const store = useMessageStore.getState()

      store.upsertMessage(
        createMessage({
          id: 'cr:req-1',
          content: 'hello',
          clientRequestId: 'req-1',
          messageType: 'user',
        }),
        'optimistic'
      )
      store.upsertMessage(
        createTaskMessage(TASK_STATE.WORKING, {
          id: 'agent-1',
          relatedMessageId: 'cr:req-1',
        }),
        'sse'
      )

      const beforeVersion = useMessageStore.getState().version
      store.replaceMessageId('cr:req-1', 'msg-real-1')

      const state = useMessageStore.getState()
      expect(state.entities['cr:req-1']).toBeUndefined()
      expect(state.entities['msg-real-1']).toBeDefined()
      expect(state.entities['msg-real-1'].clientRequestId).toBe('req-1')
      expect(state.entities['agent-1'].relatedMessageId).toBe('msg-real-1')
      expect(state.orderedIds).toContain('msg-real-1')
      expect(state.orderedIds).not.toContain('cr:req-1')
      expect(state.version).toBeGreaterThan(beforeVersion)
    })

    it('merges when real id already exists and keeps clientRequestId correlation', () => {
      const store = useMessageStore.getState()
      store.upsertMessage(
        createMessage({
          id: 'cr:req-2',
          content: 'optimistic user',
          clientRequestId: 'req-2',
          messageType: 'user',
          turnTerminalStatus: 'completed',
        }),
        'optimistic'
      )
      store.upsertMessage(
        createMessage({
          id: 'msg-real-2',
          content: 'real user',
          messageType: 'user',
          // Simulate SSE entity created before alias reconciliation, no clientRequestId.
          clientRequestId: undefined,
        }),
        'sse'
      )

      store.replaceMessageId('cr:req-2', 'msg-real-2')

      const state = useMessageStore.getState()
      expect(state.entities['cr:req-2']).toBeUndefined()
      expect(state.entities['msg-real-2']).toBeDefined()
      expect(state.entities['msg-real-2'].clientRequestId).toBe('req-2')
      expect(state.entities['msg-real-2'].turnTerminalStatus).toBe('completed')
      expect(state.orderedIds.filter(id => id === 'msg-real-2')).toHaveLength(1)
    })

    it('preserves the optimistic turn terminal status when real id already exists during patch replacement', () => {
      const store = useMessageStore.getState()
      store.upsertMessage(
        createMessage({
          id: 'cr:req-3',
          content: 'optimistic user',
          clientRequestId: 'req-3',
          messageType: 'user',
          turnTerminalStatus: 'failed',
        }),
        'optimistic'
      )
      store.upsertMessage(
        createMessage({
          id: 'msg-real-3',
          content: 'real user',
          messageType: 'user',
        }),
        'sse'
      )

      store.replaceAndPatchMessageId('cr:req-3', 'msg-real-3', {})

      const state = useMessageStore.getState()
      expect(state.entities['cr:req-3']).toBeUndefined()
      expect(state.entities['msg-real-3'].turnTerminalStatus).toBe('failed')
      expect(state.orderedIds.filter(id => id === 'msg-real-3')).toHaveLength(1)
    })
  })

  describe('hydration', () => {
    it('should track hydration state', () => {
      const store = useMessageStore.getState()

      expect(useMessageStore.getState().hydratedFromDb).toBe(false)

      store.markDbSynced()

      expect(useMessageStore.getState().hydratedFromDb).toBe(true)
      expect(useMessageStore.getState().lastDbSyncAt).toBeGreaterThan(0)
    })

    it('should reset hydration state on room change', () => {
      const store = useMessageStore.getState()

      store.markDbSynced()
      expect(useMessageStore.getState().hydratedFromDb).toBe(true)

      store.setRoom('new-room')
      expect(useMessageStore.getState().hydratedFromDb).toBe(false)
    })
  })
})

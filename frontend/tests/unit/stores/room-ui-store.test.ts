import { describe, it, expect, beforeEach } from 'vitest'
import { useRoomUiStore } from '@/stores/room-ui-store'

const flags = (roomId = 'room-1') => useRoomUiStore.getState().getRoomFlags(roomId)

describe('RoomUiStore', () => {
  beforeEach(() => {
    useRoomUiStore.getState().resetAll()
  })

  describe('boolean flags', () => {
    it('should start with default values', () => {
      const f = flags()
      expect(f.sending).toBe(false)
      expect(f.processing).toBe(false)
      expect(f.cancelling).toBe(false)
      expect(f.sseEnabled).toBe(true)
      expect(f.sseConnected).toBe(false)
      expect(f.sseError).toBeNull()
    })

    it('should update sending flag', () => {
      useRoomUiStore.getState().setSending('room-1', true)
      expect(flags().sending).toBe(true)
      useRoomUiStore.getState().setSending('room-1', false)
      expect(flags().sending).toBe(false)
    })

    it('should update processing flag', () => {
      useRoomUiStore.getState().setProcessing('room-1', true)
      expect(flags().processing).toBe(true)
    })

    it('should update cancelling flag', () => {
      useRoomUiStore.getState().setCancelling('room-1', true)
      expect(flags().cancelling).toBe(true)
    })

    it('should update SSE flags', () => {
      useRoomUiStore.getState().setSseEnabled('room-1', false)
      expect(flags().sseEnabled).toBe(false)

      useRoomUiStore.getState().setSseConnected('room-1', true)
      expect(flags().sseConnected).toBe(true)

      useRoomUiStore.getState().setSseError('room-1', 'Connection failed')
      expect(flags().sseError).toBe('Connection failed')
    })
  })

  describe('room isolation', () => {
    it('flags set on one room do not affect another', () => {
      useRoomUiStore.getState().setSending('room-1', true)
      useRoomUiStore.getState().setProcessing('room-1', true)
      expect(flags('room-1').sending).toBe(true)
      expect(flags('room-1').processing).toBe(true)
      expect(flags('room-2').sending).toBe(false)
      expect(flags('room-2').processing).toBe(false)
    })
  })

  describe('resetRoom', () => {
    it('deletes a single room entry, leaving others untouched', () => {
      useRoomUiStore.getState().setSending('room-1', true)
      useRoomUiStore.getState().setProcessing('room-2', true)

      useRoomUiStore.getState().resetRoom('room-1')

      // room-1 returns defaults
      expect(flags('room-1').sending).toBe(false)
      // room-2 untouched
      expect(flags('room-2').processing).toBe(true)
    })
  })

  describe('resetAll', () => {
    it('should reset all state to defaults', () => {
      const store = useRoomUiStore.getState()
      store.setSending('room-1', true)
      store.setProcessing('room-1', true)
      store.setCancelling('room-1', true)
      store.setSseEnabled('room-1', false)
      store.setSseConnected('room-1', true)
      store.setSseError('room-1', 'error')
      store.setPendingRoomData('room-1', { initialMessage: 'hi' })

      store.resetAll()

      const f = flags()
      expect(f.sending).toBe(false)
      expect(f.processing).toBe(false)
      expect(f.cancelling).toBe(false)
      expect(f.sseEnabled).toBe(true)
      expect(f.sseConnected).toBe(false)
      expect(f.sseError).toBeNull()
      expect(useRoomUiStore.getState().pendingRoomData).toEqual({})
      expect(useRoomUiStore.getState().rooms).toEqual({})
    })
  })

  describe('localSendSeq', () => {
    it('increments from 0 to 1 to 2', () => {
      const store = useRoomUiStore.getState()
      expect(store.localSendSeqByRoom['room-1'] ?? 0).toBe(0)
      store.markLocalSend('room-1')
      expect(useRoomUiStore.getState().localSendSeqByRoom['room-1']).toBe(1)
      useRoomUiStore.getState().markLocalSend('room-1')
      expect(useRoomUiStore.getState().localSendSeqByRoom['room-1']).toBe(2)
    })

    it('is room-isolated', () => {
      useRoomUiStore.getState().markLocalSend('room-1')
      useRoomUiStore.getState().markLocalSend('room-1')
      useRoomUiStore.getState().markLocalSend('room-2')
      expect(useRoomUiStore.getState().localSendSeqByRoom['room-1']).toBe(2)
      expect(useRoomUiStore.getState().localSendSeqByRoom['room-2']).toBe(1)
    })

    it('is deleted by resetRoom', () => {
      useRoomUiStore.getState().markLocalSend('room-1')
      useRoomUiStore.getState().markLocalSend('room-2')
      useRoomUiStore.getState().resetRoom('room-1')
      expect(useRoomUiStore.getState().localSendSeqByRoom['room-1']).toBeUndefined()
      expect(useRoomUiStore.getState().localSendSeqByRoom['room-2']).toBe(1)
    })

    it('is cleared by resetAll', () => {
      useRoomUiStore.getState().markLocalSend('room-1')
      useRoomUiStore.getState().markLocalSend('room-2')
      useRoomUiStore.getState().resetAll()
      expect(useRoomUiStore.getState().localSendSeqByRoom).toEqual({})
    })
  })

  describe('initialHydrationSeq', () => {
    it('increments from 0 to 1 to 2', () => {
      const store = useRoomUiStore.getState()
      expect(store.initialHydrationSeqByRoom['room-1'] ?? 0).toBe(0)
      store.markInitialHydrated('room-1')
      expect(useRoomUiStore.getState().initialHydrationSeqByRoom['room-1']).toBe(1)
      useRoomUiStore.getState().markInitialHydrated('room-1')
      expect(useRoomUiStore.getState().initialHydrationSeqByRoom['room-1']).toBe(2)
    })

    it('is room-isolated', () => {
      useRoomUiStore.getState().markInitialHydrated('room-1')
      useRoomUiStore.getState().markInitialHydrated('room-1')
      useRoomUiStore.getState().markInitialHydrated('room-2')
      expect(useRoomUiStore.getState().initialHydrationSeqByRoom['room-1']).toBe(2)
      expect(useRoomUiStore.getState().initialHydrationSeqByRoom['room-2']).toBe(1)
    })

    it('is deleted by resetRoom', () => {
      useRoomUiStore.getState().markInitialHydrated('room-1')
      useRoomUiStore.getState().markInitialHydrated('room-2')
      useRoomUiStore.getState().resetRoom('room-1')
      expect(useRoomUiStore.getState().initialHydrationSeqByRoom['room-1']).toBeUndefined()
      expect(useRoomUiStore.getState().initialHydrationSeqByRoom['room-2']).toBe(1)
    })

    it('is cleared by resetAll', () => {
      useRoomUiStore.getState().markInitialHydrated('room-1')
      useRoomUiStore.getState().markInitialHydrated('room-2')
      useRoomUiStore.getState().resetAll()
      expect(useRoomUiStore.getState().initialHydrationSeqByRoom).toEqual({})
    })
  })

  describe('conversationScroll', () => {
    it('persists and reads scroll snapshots per room', () => {
      useRoomUiStore.getState().saveConversationScroll('room-1', { scrollTop: 120, atBottom: false })
      expect(useRoomUiStore.getState().getConversationScroll('room-1')).toEqual({
        scrollTop: 120,
        atBottom: false,
      })
      expect(useRoomUiStore.getState().getConversationScroll('room-2')).toBeUndefined()
    })

    it('survives resetRoom so revisits can restore position', () => {
      useRoomUiStore.getState().saveConversationScroll('room-1', { scrollTop: 300, atBottom: false })
      useRoomUiStore.getState().resetRoom('room-1')
      expect(useRoomUiStore.getState().getConversationScroll('room-1')).toEqual({
        scrollTop: 300,
        atBottom: false,
      })
    })

    it('is cleared by resetAll', () => {
      useRoomUiStore.getState().saveConversationScroll('room-1', { scrollTop: 300, atBottom: false })
      useRoomUiStore.getState().resetAll()
      expect(useRoomUiStore.getState().getConversationScroll('room-1')).toBeUndefined()
    })
  })

  describe('detailPaneScroll', () => {
    it('persists and reads scroll snapshots per message', () => {
      useRoomUiStore.getState().saveDetailPaneScroll('msg-1', { scrollTop: 180, atBottom: false })
      expect(useRoomUiStore.getState().getDetailPaneScroll('msg-1')).toEqual({
        scrollTop: 180,
        atBottom: false,
      })
      expect(useRoomUiStore.getState().getDetailPaneScroll('msg-2')).toBeUndefined()
    })

    it('survives resetRoom', () => {
      useRoomUiStore.getState().saveDetailPaneScroll('msg-1', { scrollTop: 180, atBottom: false })
      useRoomUiStore.getState().resetRoom('room-1')
      expect(useRoomUiStore.getState().getDetailPaneScroll('msg-1')).toEqual({
        scrollTop: 180,
        atBottom: false,
      })
    })

    it('is cleared by resetAll', () => {
      useRoomUiStore.getState().saveDetailPaneScroll('msg-1', { scrollTop: 180, atBottom: false })
      useRoomUiStore.getState().resetAll()
      expect(useRoomUiStore.getState().getDetailPaneScroll('msg-1')).toBeUndefined()
    })

    it('evicts oldest entries when the LRU cap is exceeded', () => {
      const store = useRoomUiStore.getState()
      for (let i = 0; i < 33; i += 1) {
        store.saveDetailPaneScroll(`msg-${i}`, { scrollTop: i, atBottom: false })
      }
      expect(store.getDetailPaneScroll('msg-0')).toBeUndefined()
      expect(store.getDetailPaneScroll('msg-32')).toEqual({ scrollTop: 32, atBottom: false })
      expect(Object.keys(useRoomUiStore.getState().detailScrollByMessageId)).toHaveLength(32)
    })

    it('refreshes LRU order when an existing message is saved again', () => {
      const store = useRoomUiStore.getState()
      for (let i = 0; i < 32; i += 1) {
        store.saveDetailPaneScroll(`msg-${i}`, { scrollTop: i, atBottom: false })
      }
      store.saveDetailPaneScroll('msg-0', { scrollTop: 999, atBottom: true })
      store.saveDetailPaneScroll('msg-new', { scrollTop: 1, atBottom: false })
      expect(store.getDetailPaneScroll('msg-0')).toEqual({ scrollTop: 999, atBottom: true })
      expect(store.getDetailPaneScroll('msg-1')).toBeUndefined()
      expect(store.getDetailPaneScroll('msg-new')).toEqual({ scrollTop: 1, atBottom: false })
    })
  })

  describe('selectedAgentMessageId', () => {
    it('openAgentDetail sets messageId for a room', () => {
      useRoomUiStore.getState().openAgentDetail('room-1', 'agent-msg-1')
      expect(useRoomUiStore.getState().selectedAgentMessageIdByRoom['room-1']).toBe('agent-msg-1')
    })

    it('closeAgentDetail removes messageId for a room', () => {
      useRoomUiStore.getState().openAgentDetail('room-1', 'agent-msg-1')
      useRoomUiStore.getState().closeAgentDetail('room-1')
      expect(useRoomUiStore.getState().selectedAgentMessageIdByRoom['room-1']).toBeUndefined()
    })

    it('is room-isolated', () => {
      useRoomUiStore.getState().openAgentDetail('room-1', 'agent-msg-1')
      useRoomUiStore.getState().openAgentDetail('room-2', 'agent-msg-2')
      expect(useRoomUiStore.getState().selectedAgentMessageIdByRoom['room-1']).toBe('agent-msg-1')
      expect(useRoomUiStore.getState().selectedAgentMessageIdByRoom['room-2']).toBe('agent-msg-2')
    })

    it('is deleted by resetRoom', () => {
      useRoomUiStore.getState().openAgentDetail('room-1', 'agent-msg-1')
      useRoomUiStore.getState().openAgentDetail('room-2', 'agent-msg-2')
      useRoomUiStore.getState().resetRoom('room-1')
      expect(useRoomUiStore.getState().selectedAgentMessageIdByRoom['room-1']).toBeUndefined()
      expect(useRoomUiStore.getState().selectedAgentMessageIdByRoom['room-2']).toBe('agent-msg-2')
    })

    it('is cleared by resetAll', () => {
      useRoomUiStore.getState().openAgentDetail('room-1', 'agent-msg-1')
      useRoomUiStore.getState().openAgentDetail('room-2', 'agent-msg-2')
      useRoomUiStore.getState().resetAll()
      expect(useRoomUiStore.getState().selectedAgentMessageIdByRoom).toEqual({})
    })
  })

  describe('pendingRoomData', () => {
    it('should store pending data for a room', () => {
      useRoomUiStore.getState().setPendingRoomData('room-1', {
        initialMessage: 'Hello',
        mode: 'direct',
        agentScope: { source: 'room_default' },
        clientRequestId: 'request-1',
      })

      const data = useRoomUiStore.getState().pendingRoomData['room-1']
      expect(data).toEqual({
        initialMessage: 'Hello',
        mode: 'direct',
        agentScope: { source: 'room_default' },
        clientRequestId: 'request-1',
      })
    })

    it('should store data for multiple rooms independently', () => {
      const store = useRoomUiStore.getState()
      store.setPendingRoomData('room-1', { initialMessage: 'msg1' })
      store.setPendingRoomData('room-2', { initialMessage: 'msg2' })

      expect(useRoomUiStore.getState().pendingRoomData['room-1']?.initialMessage).toBe('msg1')
      expect(useRoomUiStore.getState().pendingRoomData['room-2']?.initialMessage).toBe('msg2')
    })

    it('should consume (read and delete) pending data', () => {
      useRoomUiStore.getState().setPendingRoomData('room-1', { initialMessage: 'test' })

      const consumed = useRoomUiStore.getState().consumePendingRoomData('room-1')
      expect(consumed).toEqual({ initialMessage: 'test' })
      expect(useRoomUiStore.getState().pendingRoomData['room-1']).toBeUndefined()
    })

    it('should return null when consuming non-existent data', () => {
      const consumed = useRoomUiStore.getState().consumePendingRoomData('room-999')
      expect(consumed).toBeNull()
    })

    it('should not affect other rooms when consuming', () => {
      const store = useRoomUiStore.getState()
      store.setPendingRoomData('room-1', { initialMessage: 'keep' })
      store.setPendingRoomData('room-2', { initialMessage: 'consume' })

      useRoomUiStore.getState().consumePendingRoomData('room-2')

      expect(useRoomUiStore.getState().pendingRoomData['room-1']?.initialMessage).toBe('keep')
      expect(useRoomUiStore.getState().pendingRoomData['room-2']).toBeUndefined()
    })

    it('should overwrite pending data for same room', () => {
      const store = useRoomUiStore.getState()
      store.setPendingRoomData('room-1', { initialMessage: 'old' })
      store.setPendingRoomData('room-1', { initialMessage: 'new', mode: 'supervisor', agentScope: { source: 'saved_group', group_id: 'custom' }, clientRequestId: 'request-2' })

      const data = useRoomUiStore.getState().pendingRoomData['room-1']
      expect(data).toEqual({ initialMessage: 'new', mode: 'supervisor', agentScope: { source: 'saved_group', group_id: 'custom' }, clientRequestId: 'request-2' })
    })
  })
})

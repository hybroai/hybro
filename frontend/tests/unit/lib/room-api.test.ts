import { describe, it, expect, beforeEach } from 'vitest'
import { http, HttpResponse } from 'msw'
import { server } from '../../setup/msw-server'
import { errorHandlers } from '../../setup/msw-handlers'
import { getApiUrl } from '@/lib/utils'
import {
  createNewRoom,
  inquiryActiveRuns,
  inquiryRoomSetting,
  SendMessage,
  inquiryRoomMessagesByRoomId,
  updateRoomAgentSet,
  suggestAgents,
  listRoomHistory,
  updateRoomHistoryItem,
  reorderPinnedRooms,
  deleteRoomHistoryItem,
} from '@/lib/api/room'

const roomCenter = getApiUrl('roomCenter')

describe('Room API', () => {
  beforeEach(() => {
    server.resetHandlers()
  })

  describe('createNewRoom', () => {
    it('should create a new room with correct request body', async () => {
      let capturedBody: Record<string, unknown> | null = null
      server.use(
        http.post(`${roomCenter}/createNewRoom`, async ({ request }) => {
          capturedBody = await request.json() as Record<string, unknown>
          return HttpResponse.json({
            success: true,
            room_id: 'test-room-id',
            room: {
              room_id: 'test-room-id',
              room_name: capturedBody.room_name,
              room_owner_id: capturedBody.room_owner_id,
              room_owner_name: capturedBody.room_owner_name,
              room_agent_set: capturedBody.room_agent_set || {},
              room_created_at: new Date().toISOString(),
            },
          })
        })
      )

      const result = await createNewRoom('Test Room', 'user-1', 'Test User')

      expect(result.success).toBe(true)
      expect(result.room?.room_name).toBe('Test Room')
      expect(capturedBody).toMatchObject({
        room_name: 'Test Room',
        room_owner_id: 'user-1',
        room_owner_name: 'Test User',
      })
    })

    it('should include agent set in request body when provided', async () => {
      let capturedBody: Record<string, unknown> | null = null
      server.use(
        http.post(`${roomCenter}/createNewRoom`, async ({ request }) => {
          capturedBody = await request.json() as Record<string, unknown>
          return HttpResponse.json({ success: true, room_id: 'test-room-id' })
        })
      )

      const agentSet = { 'agent-1': 'Agent One' }
      await createNewRoom('Test Room', 'user-1', 'Test User', undefined, agentSet)

      expect(capturedBody).toMatchObject({
        room_agent_set: { 'agent-1': 'Agent One' },
      })
    })
  })

  describe('inquiryRoomSetting', () => {
    it('should fetch room settings with correct room_id', async () => {
      let capturedBody: Record<string, unknown> | null = null
      server.use(
        http.post(`${roomCenter}/inquiryRoomSetting`, async ({ request }) => {
          capturedBody = await request.json() as Record<string, unknown>
          return HttpResponse.json({
            success: true,
            room_id: capturedBody.room_id,
            room: { room_id: capturedBody.room_id, room_name: 'Test Room' },
          })
        })
      )

      const result = await inquiryRoomSetting('room-42')

      expect(result.success).toBe(true)
      expect(capturedBody).toMatchObject({ room_id: 'room-42' })
    })
  })

  describe('inquiryActiveRuns', () => {
    it('should fetch active runs with correct room_id', async () => {
      let capturedBody: Record<string, unknown> | null = null
      server.use(
        http.post(`${roomCenter}/inquiryActiveRuns`, async ({ request }) => {
          capturedBody = await request.json() as Record<string, unknown>
          return HttpResponse.json({
            success: true,
            room_id: capturedBody.room_id,
            active_runs: [{ state: 'processing', trigger_message_id: 'm1' }],
          })
        })
      )

      const result = await inquiryActiveRuns('room-42')

      expect(result.success).toBe(true)
      expect(result.active_runs).toHaveLength(1)
      expect(capturedBody).toMatchObject({ room_id: 'room-42' })
    })
  })

  describe('room history', () => {
    it('lists lightweight history items', async () => {
      server.use(
        http.get(getApiUrl('roomCenter/history'), () => HttpResponse.json({
          items: [{
            room_id: 'room-1',
            title: 'Room 1',
            last_activity_at: '2026-08-10T00:00:00Z',
            is_pinned: false,
            pin_order: null,
            status: 'processing',
          }],
        }))
      )

      const result = await listRoomHistory()
      expect(result.items[0]).toMatchObject({ room_id: 'room-1', status: 'processing' })
    })

    it('updates, reorders, and deletes history items with REST methods', async () => {
      const requests: Array<{ method: string; body?: unknown }> = []
      server.use(
        http.patch(getApiUrl('roomCenter/history/room-1'), async ({ request }) => {
          requests.push({ method: request.method, body: await request.json() })
          return HttpResponse.json({
            room_id: 'room-1', title: 'Renamed', last_activity_at: '2026-08-10T00:00:00Z',
            is_pinned: true, pin_order: 1, status: 'idle',
          })
        }),
        http.put(getApiUrl('roomCenter/history/pinned-order'), async ({ request }) => {
          requests.push({ method: request.method, body: await request.json() })
          return HttpResponse.json({ success: true })
        }),
        http.delete(getApiUrl('roomCenter/history/room-1'), ({ request }) => {
          requests.push({ method: request.method })
          return HttpResponse.json({ success: true })
        }),
      )

      await updateRoomHistoryItem('room-1', { title: 'Renamed', is_pinned: true })
      await reorderPinnedRooms(['room-1', 'room-2'])
      await deleteRoomHistoryItem('room-1')

      expect(requests).toEqual([
        { method: 'PATCH', body: { title: 'Renamed', is_pinned: true } },
        { method: 'PUT', body: { room_ids: ['room-1', 'room-2'] } },
        { method: 'DELETE' },
      ])
    })
  })

  describe('SendMessage', () => {
    const baseSendParams = {
      roomId: 'room-1',
      userInput: 'Hello',
      userId: 'user-1',
      userName: 'Test User',
      clientRequestId: 'cr-uuid-123',
      mode: 'direct' as const,
      agentScope: { source: 'room_default' as const },
    }

    it('sends room_default routing with client_request_id and no legacy routing field', async () => {
      let capturedBody: Record<string, unknown> | null = null
      server.use(
        http.post(`${roomCenter}/sendMessage`, async ({ request }) => {
          capturedBody = await request.json() as Record<string, unknown>
          return HttpResponse.json({
            success: true,
            room_id: capturedBody.room_id,
            message_id: 'msg-new',
          })
        })
      )

      const result = await SendMessage(baseSendParams)

      expect(result.success).toBe(true)
      expect(result.message_id).toBe('msg-new')
      expect(capturedBody).toMatchObject({
        room_id: 'room-1',
        user_input: 'Hello',
        user_id: 'user-1',
        user_name: 'Test User',
        mode: 'direct',
        agent_scope: { source: 'room_default' },
        client_request_id: 'cr-uuid-123',
      })
      expect(capturedBody).not.toHaveProperty('target_group')
    })

    it('sends all_agents routing with client_request_id and no legacy routing field', async () => {
      let capturedBody: Record<string, unknown> | null = null
      server.use(
        http.post(`${roomCenter}/sendMessage`, async ({ request }) => {
          capturedBody = await request.json() as Record<string, unknown>
          return HttpResponse.json({ success: true, message_id: 'msg-1' })
        })
      )

      await SendMessage({
        ...baseSendParams,
        userInput: 'Hello all agents',
        clientRequestId: 'cr-all-agents-123',
        mode: 'supervisor',
        agentScope: { source: 'all_agents' },
      })

      expect(capturedBody).toHaveProperty('client_request_id', 'cr-all-agents-123')
      expect(capturedBody).toHaveProperty('mode', 'supervisor')
      expect(capturedBody).toHaveProperty('agent_scope', { source: 'all_agents' })
      expect(capturedBody).not.toHaveProperty('mentioned_agent_ids')
      expect(capturedBody).not.toHaveProperty('target_group_id')
      expect(capturedBody).not.toHaveProperty('target_group')
    })

    it('should include quoted text in extend_info', async () => {
      let capturedBody: Record<string, unknown> | null = null
      server.use(
        http.post(`${roomCenter}/sendMessage`, async ({ request }) => {
          capturedBody = await request.json() as Record<string, unknown>
          return HttpResponse.json({ success: true, message_id: 'msg-1' })
        })
      )

      await SendMessage({
        ...baseSendParams,
        userInput: 'Reply',
        relatedMessageId: 'related-msg-1',
        quotedText: 'Quoted text here',
      })

      expect(capturedBody).not.toBeNull()
      const body = capturedBody as unknown as Record<string, unknown>
      const message = body.message as Record<string, unknown>
      expect(message.related_message_id).toBe('related-msg-1')
      expect(message.extend_info).toMatchObject({ quoted_text: 'Quoted text here' })
    })

    it('should include quoted sender name in extend_info', async () => {
      let capturedBody: Record<string, unknown> | null = null
      server.use(
        http.post(`${roomCenter}/sendMessage`, async ({ request }) => {
          capturedBody = await request.json() as Record<string, unknown>
          return HttpResponse.json({ success: true, message_id: 'msg-1' })
        })
      )

      await SendMessage({
        ...baseSendParams,
        userInput: 'Reply',
        relatedMessageId: 'related-msg-1',
        quotedText: 'Quoted text here',
        quotedSenderName: 'Spec Agent',
      })

      expect(capturedBody).not.toBeNull()
      const body = capturedBody as unknown as Record<string, unknown>
      const message = body.message as Record<string, unknown>
      expect(message.extend_info).toMatchObject({
        quoted_text: 'Quoted text here',
        quoted_sender_name: 'Spec Agent',
      })
    })

    it('should send structured quote payload when structuredQuote is provided', async () => {
      let capturedBody: Record<string, unknown> | null = null
      server.use(
        http.post(`${roomCenter}/sendMessage`, async ({ request }) => {
          capturedBody = await request.json() as Record<string, unknown>
          return HttpResponse.json({ success: true, message_id: 'msg-1' })
        })
      )

      await SendMessage({
        ...baseSendParams,
        userInput: 'Get details',
        relatedMessageId: null,
        quotedText: null,
        structuredQuote: {
          text: 'The highlighted content',
          source_message_id: 'agent-msg-42',
          source_kind: 'agent',
          sender_display_name: 'Research Agent',
          source_agent_id: 'agent-42',
        },
      })

      expect(capturedBody).not.toBeNull()
      const body = capturedBody as unknown as Record<string, unknown>
      const message = body.message as Record<string, unknown>
      expect(message.quote).toMatchObject({
        text: 'The highlighted content',
        source_message_id: 'agent-msg-42',
        source_kind: 'agent',
        sender_display_name: 'Research Agent',
        source_agent_id: 'agent-42',
      })
      expect(message.extend_info).toMatchObject({
        quoted_text: 'The highlighted content',
        quoted_sender_name: 'Research Agent',
      })
      expect(message.related_message_id).toBeNull()
    })

    it('should use legacy extend_info when structuredQuote is null but quoted_text provided', async () => {
      let capturedBody: Record<string, unknown> | null = null
      server.use(
        http.post(`${roomCenter}/sendMessage`, async ({ request }) => {
          capturedBody = await request.json() as Record<string, unknown>
          return HttpResponse.json({ success: true, message_id: 'msg-1' })
        })
      )

      await SendMessage({
        ...baseSendParams,
        userInput: 'Reply',
        relatedMessageId: 'related-msg-1',
        quotedText: 'Legacy quoted text',
        quotedSenderName: 'Agent Name',
        structuredQuote: null,
      })

      const body = capturedBody as unknown as Record<string, unknown>
      const message = body.message as Record<string, unknown>
      expect(message.quote).toBeUndefined()
      expect(message.extend_info).toMatchObject({ quoted_text: 'Legacy quoted text', quoted_sender_name: 'Agent Name' })
      expect(message.related_message_id).toBe('related-msg-1')
    })

    it('sends mentioned_agent_ids without message_target_mode or target_group_id', async () => {
      let capturedBody: Record<string, unknown> | null = null
      server.use(
        http.post(`${roomCenter}/sendMessage`, async ({ request }) => {
          capturedBody = await request.json() as Record<string, unknown>
          return HttpResponse.json({ success: true, message_id: 'msg-1' })
        })
      )

      await SendMessage({
        ...baseSendParams,
        userInput: 'Hello @agent',
        clientRequestId: 'cr-mention-123',
        mode: 'supervisor',
        agentScope: { source: 'mention', agent_ids: ['agent-a', 'agent-b'] },
      })

      expect(capturedBody).toHaveProperty('mode', 'supervisor')
      expect(capturedBody).toHaveProperty('agent_scope', { source: 'mention', agent_ids: ['agent-a', 'agent-b'] })
      expect(capturedBody).toHaveProperty('client_request_id', 'cr-mention-123')
      expect(capturedBody).not.toHaveProperty('message_target_mode')
      expect(capturedBody).not.toHaveProperty('target_group_id')
      expect(capturedBody).not.toHaveProperty('target_group')
    })

    it('sends saved_group routing without mentions or legacy routing field', async () => {
      let capturedBody: Record<string, unknown> | null = null
      server.use(
        http.post(`${roomCenter}/sendMessage`, async ({ request }) => {
          capturedBody = await request.json() as Record<string, unknown>
          return HttpResponse.json({ success: true, message_id: 'msg-1' })
        })
      )

      await SendMessage({
        ...baseSendParams,
        userInput: 'Hello saved group',
        clientRequestId: 'cr-saved-group-123',
        mode: 'supervisor',
        agentScope: { source: 'saved_group', group_id: 'grp-123' },
      })

      expect(capturedBody).toHaveProperty('client_request_id', 'cr-saved-group-123')
      expect(capturedBody).toHaveProperty('mode', 'supervisor')
      expect(capturedBody).toHaveProperty('agent_scope', { source: 'saved_group', group_id: 'grp-123' })
      expect(capturedBody).not.toHaveProperty('mentioned_agent_ids')
      expect(capturedBody).not.toHaveProperty('target_group')
    })

    it('never emits legacy target fields', async () => {
      let capturedBody: Record<string, unknown> | null = null
      server.use(
        http.post(`${roomCenter}/sendMessage`, async ({ request }) => {
          capturedBody = await request.json() as Record<string, unknown>
          return HttpResponse.json({ success: true, message_id: 'msg-1' })
        })
      )

      await SendMessage(baseSendParams)
      for (const field of [
        'selected_agent_ids',
        'candidate_scope_mode',
        'message_target_mode',
        'target_group_id',
        'mentioned_agent_ids',
      ]) {
        expect(capturedBody).not.toHaveProperty(field)
      }
    })
  })

  describe('inquiryRoomMessagesByRoomId', () => {
    it('should fetch room messages with correct room_id', async () => {
      let capturedBody: Record<string, unknown> | null = null
      server.use(
        http.post(`${roomCenter}/inquiryRoomMessagesByRoomId`, async ({ request }) => {
          capturedBody = await request.json() as Record<string, unknown>
          return HttpResponse.json({ success: true, room_id: 'room-1', message_list: [] })
        })
      )

      const result = await inquiryRoomMessagesByRoomId('room-1')

      expect(result.success).toBe(true)
      expect(result.message_list).toBeDefined()
      expect(capturedBody).toEqual({ room_id: 'room-1' })
    })

    it('optionally sends timeline pagination fields', async () => {
      let capturedBody: Record<string, unknown> | null = null
      server.use(
        http.post(`${roomCenter}/inquiryRoomMessagesByRoomId`, async ({ request }) => {
          capturedBody = await request.json() as Record<string, unknown>
          return HttpResponse.json({
            success: true,
            room_id: 'room-1',
            message_list: [],
            has_more: true,
            next_cursor: 'next-token'
          })
        })
      )

      const result = await inquiryRoomMessagesByRoomId(
        'room-1',
        undefined,
        undefined,
        { limit: 37, cursor: 'cursor-token' }
      )

      expect(capturedBody).toEqual({
        room_id: 'room-1',
        limit: 37,
        cursor: 'cursor-token'
      })
      expect(result.has_more).toBe(true)
      expect(result.next_cursor).toBe('next-token')
    })
  })

  describe('updateRoomAgentSet', () => {
    it('should send agent set in request body', async () => {
      let capturedBody: Record<string, unknown> | null = null
      server.use(
        http.post(`${roomCenter}/updateRoomAgentSet`, async ({ request }) => {
          capturedBody = await request.json() as Record<string, unknown>
          return HttpResponse.json({ success: true })
        })
      )

      await updateRoomAgentSet('room-1', { 'a-1': 'Agent One', 'a-2': 'Agent Two' })

      expect(capturedBody).toMatchObject({
        room_id: 'room-1',
        room_agent_set: { 'a-1': 'Agent One', 'a-2': 'Agent Two' },
      })
    })
  })

  describe('suggestAgents', () => {
    it('should send message_text and top_k in request body', async () => {
      let capturedBody: Record<string, unknown> | null = null
      server.use(
        http.post(`${roomCenter}/suggestAgents`, async ({ request }) => {
          capturedBody = await request.json() as Record<string, unknown>
          return HttpResponse.json({
            success: true,
            routing_strategy: 'single',
            suggested_agents: [{ agent_id: 'a-1', name: 'Agent', reason: 'Best' }],
          })
        })
      )

      const result = await suggestAgents('Help me with coding', 5)

      expect(result.success).toBe(true)
      expect(result.suggested_agents).toHaveLength(1)
      expect(capturedBody).toMatchObject({
        message_text: 'Help me with coding',
        top_k: 5,
      })
    })
  })

  describe('error handling', () => {
    const errorSendParams = {
      roomId: 'room-1',
      userInput: 'Hello',
      userId: 'user-1',
      userName: 'Test User',
      clientRequestId: 'cr-error-test',
      mode: 'direct' as const,
      agentScope: { source: 'room_default' as const },
    }

    it('should handle network errors', async () => {
      server.use(errorHandlers.networkError)
      await expect(
        SendMessage(errorSendParams)
      ).rejects.toThrow()
    })

    it('should handle server errors (500)', async () => {
      server.use(errorHandlers.serverError)
      await expect(
        SendMessage(errorSendParams)
      ).rejects.toThrow()
    })

    it('should handle auth errors (401)', async () => {
      server.use(errorHandlers.authError)
      await expect(
        SendMessage(errorSendParams)
      ).rejects.toThrow()
    })

    it('should handle rate limit errors (429)', async () => {
      server.use(errorHandlers.rateLimitError)
      await expect(
        SendMessage(errorSendParams)
      ).rejects.toThrow()
    })
  })
})

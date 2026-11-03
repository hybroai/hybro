import { describe, it, expect, beforeEach, vi } from 'vitest'
import { http, HttpResponse } from 'msw'
import { server } from '../../setup/msw-server'
import { errorHandlers } from '../../setup/msw-handlers'
import { getApiUrl } from '@/lib/utils'
import { recentLogs } from '@/lib/log'
import {
  createNewRoom,
  inquiryActiveRuns,
  inquiryRoomSetting,
  SendMessage,
  inquiryRoomMessagesByConversationId,
  suggestAgents,
  listRoomHistory,
  updateRoomHistoryItem,
  reorderPinnedRooms,
  deleteRoomHistoryItem,
} from '@/lib/api/room'

const rooms = getApiUrl('rooms')

describe('Room API', () => {
  beforeEach(() => {
    server.resetHandlers()
  })

  describe('createNewRoom', () => {
    it('should POST rooms with correct request body', async () => {
      let capturedBody: Record<string, unknown> | null = null
      server.use(
        http.post(rooms, async ({ request }) => {
          capturedBody = await request.json() as Record<string, unknown>
          return HttpResponse.json({
            success: true,
            conversation_id: 'test-room-id',
            room: {
              conversation_id: 'test-room-id',
              room_name: capturedBody.room_name,
              room_owner_name: capturedBody.room_owner_name,
              room_created_at: new Date().toISOString(),
            },
          })
        })
      )

      const result = await createNewRoom('Test Room', 'Test User')

      expect(result.success).toBe(true)
      expect(result.room?.room_name).toBe('Test Room')
      expect(capturedBody).toMatchObject({
        room_name: 'Test Room',
        room_owner_name: 'Test User',
      })
      expect(capturedBody).not.toHaveProperty('room_owner_id')
      expect(capturedBody).not.toHaveProperty('room_agent_set')
      expect(capturedBody).not.toHaveProperty('applied_from_group')
    })

    it('should include extend_info and membership in request body when provided', async () => {
      let capturedBody: Record<string, unknown> | null = null
      server.use(
        http.post(rooms, async ({ request }) => {
          capturedBody = await request.json() as Record<string, unknown>
          return HttpResponse.json({ success: true, conversation_id: 'test-room-id' })
        })
      )

      await createNewRoom('Test Room', 'Test User', undefined, { use_supervisor: true }, {
        membership_seed_input: 'manual',
        room_agent_ids: ['agent-1'],
      })

      expect(capturedBody).toEqual({
        room_name: 'Test Room',
        room_owner_name: 'Test User',
        extend_info: { use_supervisor: true },
        membership_seed_input: 'manual',
        room_agent_ids: ['agent-1'],
      })
    })
  })

  describe('inquiryRoomSetting', () => {
    it('should GET the room by id', async () => {
      let capturedConversationId: string | null = null
      server.use(
        http.get(`${rooms}/:conversationId`, ({ params }) => {
          capturedConversationId = params.conversationId as string
          return HttpResponse.json({
            success: true,
            conversation_id: params.conversationId,
            room: { conversation_id: params.conversationId, room_name: 'Test Room' },
          })
        })
      )

      const result = await inquiryRoomSetting('room-42')

      expect(result.success).toBe(true)
      expect(capturedConversationId).toBe('room-42')
    })
  })

  describe('inquiryActiveRuns', () => {
    it('should GET active runs for the room', async () => {
      let capturedConversationId: string | null = null
      server.use(
        http.get(`${rooms}/:conversationId/active-runs`, ({ params }) => {
          capturedConversationId = params.conversationId as string
          return HttpResponse.json({
            success: true,
            conversation_id: params.conversationId,
            active_runs: [{ state: 'processing', trigger_message_id: 'm1' }],
          })
        })
      )

      const result = await inquiryActiveRuns('room-42')

      expect(result.success).toBe(true)
      expect(result.active_runs).toHaveLength(1)
      expect(capturedConversationId).toBe('room-42')
    })
  })

  describe('room history', () => {
    it('lists lightweight history items', async () => {
      server.use(
        http.get(rooms, () => HttpResponse.json({
          items: [{
            conversation_id: 'room-1',
            title: 'Room 1',
            last_activity_at: '2026-08-10T00:00:00Z',
            is_pinned: false,
            pin_order: null,
            status: 'processing',
          }],
        }))
      )

      const result = await listRoomHistory()
      expect(result.items[0]).toMatchObject({ conversation_id: 'room-1', status: 'processing' })
    })

    it('updates, reorders, and deletes history items with REST methods', async () => {
      const requests: Array<{ method: string; body?: unknown }> = []
      server.use(
        http.patch(`${rooms}/room-1`, async ({ request }) => {
          requests.push({ method: request.method, body: await request.json() })
          return HttpResponse.json({
            conversation_id: 'room-1', title: 'Renamed', last_activity_at: '2026-08-10T00:00:00Z',
            is_pinned: true, pin_order: 1, status: 'idle',
          })
        }),
        http.put(`${rooms}/pinned-order`, async ({ request }) => {
          requests.push({ method: request.method, body: await request.json() })
          return HttpResponse.json({ success: true })
        }),
        http.delete(`${rooms}/room-1`, ({ request }) => {
          requests.push({ method: request.method })
          return HttpResponse.json({ success: true })
        }),
      )

      await updateRoomHistoryItem('room-1', { title: 'Renamed', is_pinned: true })
      await reorderPinnedRooms(['room-1', 'room-2'])
      await deleteRoomHistoryItem('room-1')

      expect(requests).toEqual([
        { method: 'PATCH', body: { title: 'Renamed', is_pinned: true } },
        { method: 'PUT', body: { conversation_ids: ['room-1', 'room-2'] } },
        { method: 'DELETE' },
      ])
    })
  })

  describe('SendMessage', () => {
    const baseSendParams = {
      conversationId: 'room-1',
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
        http.post(`${rooms}/:conversationId/messages`, async ({ request }) => {
          capturedBody = await request.json() as Record<string, unknown>
          return HttpResponse.json({
            success: true,
            conversation_id: 'room-1',
            message_id: 'msg-new',
          })
        })
      )

      const result = await SendMessage(baseSendParams)

      expect(result.success).toBe(true)
      expect(result.message_id).toBe('msg-new')
      expect(capturedBody).not.toHaveProperty('conversation_id')
      expect((capturedBody as unknown as { message: { conversation_id: string } }).message.conversation_id).toBe('room-1')
      expect(capturedBody).toMatchObject({
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
        http.post(`${rooms}/:conversationId/messages`, async ({ request }) => {
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

    it('should send structured quote payload when structuredQuote is provided', async () => {
      let capturedBody: Record<string, unknown> | null = null
      server.use(
        http.post(`${rooms}/:conversationId/messages`, async ({ request }) => {
          capturedBody = await request.json() as Record<string, unknown>
          return HttpResponse.json({ success: true, message_id: 'msg-1' })
        })
      )

      await SendMessage({
        ...baseSendParams,
        userInput: 'Get details',
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
      // The backend derives the display fields after persisting the quote.
      expect(message.extend_info).toBeNull()
      expect(message.related_message_id).toBeNull()
    })

    it('sends no quote and no client display metadata without a structured quote', async () => {
      let capturedBody: Record<string, unknown> | null = null
      server.use(
        http.post(`${rooms}/:conversationId/messages`, async ({ request }) => {
          capturedBody = await request.json() as Record<string, unknown>
          return HttpResponse.json({ success: true, message_id: 'msg-1' })
        })
      )

      await SendMessage({ ...baseSendParams, userInput: 'Reply', structuredQuote: null })

      const body = capturedBody as unknown as Record<string, unknown>
      const message = body.message as Record<string, unknown>
      expect(message).not.toHaveProperty('quote')
      expect(message.extend_info).toBeNull()
      expect(message.related_message_id).toBeNull()
    })

    it('sends mentioned_agent_ids without message_target_mode or target_group_id', async () => {
      let capturedBody: Record<string, unknown> | null = null
      server.use(
        http.post(`${rooms}/:conversationId/messages`, async ({ request }) => {
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
        http.post(`${rooms}/:conversationId/messages`, async ({ request }) => {
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
        http.post(`${rooms}/:conversationId/messages`, async ({ request }) => {
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

  describe('inquiryRoomMessagesByConversationId', () => {
    it('should GET room messages without query parameters by default', async () => {
      let capturedUrl: URL | null = null
      server.use(
        http.get(`${rooms}/:conversationId/messages`, ({ request }) => {
          capturedUrl = new URL(request.url)
          return HttpResponse.json({ success: true, conversation_id: 'room-1', message_list: [] })
        })
      )

      const result = await inquiryRoomMessagesByConversationId('room-1')

      expect(result.success).toBe(true)
      expect(result.message_list).toBeDefined()
      const url = capturedUrl as unknown as URL
      expect(url.pathname).toMatch(/\/rooms\/room-1\/messages$/)
      expect(url.search).toBe('')
    })

    it('optionally sends timeline pagination query parameters', async () => {
      let capturedUrl: URL | null = null
      server.use(
        http.get(`${rooms}/:conversationId/messages`, ({ request }) => {
          capturedUrl = new URL(request.url)
          return HttpResponse.json({
            success: true,
            conversation_id: 'room-1',
            message_list: [],
            has_more: true,
            next_cursor: 'next-token'
          })
        })
      )

      const result = await inquiryRoomMessagesByConversationId(
        'room-1',
        undefined,
        undefined,
        { limit: 37, cursor: 'cursor-token' }
      )

      const params = (capturedUrl as unknown as URL).searchParams
      expect(Object.fromEntries(params)).toEqual({ limit: '37', cursor: 'cursor-token' })
      expect(result.has_more).toBe(true)
      expect(result.next_cursor).toBe('next-token')
    })

    it('omits a null cursor', async () => {
      let capturedUrl: URL | null = null
      server.use(
        http.get(`${rooms}/:conversationId/messages`, ({ request }) => {
          capturedUrl = new URL(request.url)
          return HttpResponse.json({ success: true, conversation_id: 'room-1', message_list: [] })
        })
      )

      await inquiryRoomMessagesByConversationId('room-1', undefined, undefined, { limit: 10, cursor: null })

      const params = (capturedUrl as unknown as URL).searchParams
      expect(Object.fromEntries(params)).toEqual({ limit: '10' })
    })
  })

  describe('suggestAgents', () => {
    it('should send message_text and top_k in request body', async () => {
      let capturedBody: Record<string, unknown> | null = null
      server.use(
        http.post(getApiUrl('agent-suggestions'), async ({ request }) => {
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
      conversationId: 'room-1',
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

  describe('X-Request-ID', () => {
    it('sends a fresh backend-valid request id on every call', async () => {
      const captured: Array<string | null> = []
      server.use(
        http.get(`${rooms}/:conversationId`, ({ request, params }) => {
          captured.push(request.headers.get('x-request-id'))
          return HttpResponse.json({
            success: true,
            conversation_id: params.conversationId,
            room: { conversation_id: params.conversationId, room_name: 'Test Room' },
          })
        })
      )

      await inquiryRoomSetting('room-42')
      await inquiryRoomSetting('room-42')

      expect(captured).toHaveLength(2)
      for (const id of captured) {
        expect(id).toMatch(/^[0-9a-f]{32}$/)
        expect(id).toMatch(/^[A-Za-z0-9._:-]{1,128}$/)
      }
      expect(captured[0]).not.toBe(captured[1])
    })
  })

  describe('apiClient logging', () => {
    const SECRET = 'user-secret-text'

    function entriesFor(conversationId: string) {
      return recentLogs().filter((entry) => String(entry.fields.url ?? '').endsWith(`/${conversationId}`))
    }

    it('logs a 4xx as api_request_client_error without the response body', async () => {
      vi.spyOn(console, 'warn').mockImplementation(() => undefined)
      server.use(
        http.get(`${rooms}/:conversationId`, () =>
          HttpResponse.json({ detail: [{ msg: 'bad', input: SECRET }] }, { status: 422 }))
      )

      await expect(inquiryRoomSetting('log-4xx')).rejects.toThrow()

      const entries = entriesFor('log-4xx')
      expect(entries.map((entry) => entry.event)).toEqual(['api_request_client_error'])
      expect(entries[0].fields).toMatchObject({ method: 'GET', status: 422 })
      expect(entries[0].fields.request_id).toMatch(/^[0-9a-f]{32}$/)
      expect(JSON.stringify(entries)).not.toContain(SECRET)
      vi.restoreAllMocks()
    })

    it('logs a 5xx as api_request_failed without the response body', async () => {
      vi.spyOn(console, 'error').mockImplementation(() => undefined)
      server.use(
        http.get(`${rooms}/:conversationId`, () =>
          HttpResponse.json({ error: SECRET }, { status: 500 }))
      )

      await expect(inquiryRoomSetting('log-5xx')).rejects.toThrow()

      const entries = entriesFor('log-5xx')
      expect(entries.map((entry) => entry.event)).toEqual(['api_request_failed'])
      expect(entries[0].fields).toMatchObject({ method: 'GET', status: 500 })
      expect(JSON.stringify(entries)).not.toContain(SECRET)
      vi.restoreAllMocks()
    })

    it('logs only the error name when a 200 body is not JSON', async () => {
      vi.spyOn(console, 'error').mockImplementation(() => undefined)
      server.use(
        http.get(`${rooms}/:conversationId`, () =>
          new HttpResponse(`${SECRET} not json`, { status: 200 }))
      )

      await expect(inquiryRoomSetting('log-parse')).rejects.toThrow()

      const entries = entriesFor('log-parse')
      expect(entries.map((entry) => entry.event)).toEqual(['api_request_failed'])
      expect(entries[0].fields.error_name).toBe('SyntaxError')
      expect(JSON.stringify(entries)).not.toContain(SECRET)
      vi.restoreAllMocks()
    })
  })
})

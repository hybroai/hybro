import { http, HttpResponse } from 'msw'
import { getApiUrl } from '@/lib/utils'

const roomCenter = getApiUrl('roomCenter')
const agent = getApiUrl('agent')
const orchestrationCenter = getApiUrl('orchestrationCenter')
const task = getApiUrl('task')
const sseBase = getApiUrl('sse')

export const handlers = [
  // Room Center API handlers
  http.post(`${roomCenter}/createNewRoom`, async ({ request }) => {
    const body = await request.json() as Record<string, unknown>
    return HttpResponse.json({
      success: true,
      room_id: 'test-room-id',
      room: {
        room_id: 'test-room-id',
        room_name: body.room_name || 'Test Room',
        room_owner_id: body.room_owner_id || 'test-user',
        room_owner_name: body.room_owner_name || 'Test User',
        room_agent_set: body.room_agent_set || {},
        room_created_at: new Date().toISOString(),
      },
    })
  }),

  http.post(`${roomCenter}/inquiryRoomSetting`, async ({ request }) => {
    const body = await request.json() as Record<string, unknown>
    return HttpResponse.json({
      success: true,
      room_id: body.room_id,
      room: {
        room_id: body.room_id,
        room_name: 'Test Room',
        room_owner_id: 'test-user',
        room_owner_name: 'Test User',
        room_agent_set: {},
        room_created_at: new Date().toISOString(),
      },
    })
  }),

  http.post(`${roomCenter}/inquiryActiveRuns`, async ({ request }) => {
    const body = await request.json() as Record<string, unknown>
    return HttpResponse.json({
      success: true,
      room_id: body.room_id,
      active_runs: [],
    })
  }),

  http.post(`${roomCenter}/sendMessage`, async ({ request }) => {
    const body = await request.json() as Record<string, unknown>
    return HttpResponse.json({
      success: true,
      room_id: body.room_id,
      message_id: `msg-${Date.now()}`,
      user_id: body.user_id,
      user_name: body.user_name,
      message: {
        room_id: body.room_id,
        message_id: `msg-${Date.now()}`,
        message_type: 'user',
        message_content: {
          message_text: body.user_input,
        },
        message_created_at: new Date().toISOString(),
      },
    })
  }),

  http.post(`${roomCenter}/inquiryRoomMessagesByRoomId`, async ({ request }) => {
    const body = await request.json() as Record<string, unknown>
    return HttpResponse.json({
      success: true,
      room_id: body.room_id,
      message_list: [],
    })
  }),

  http.post(`${roomCenter}/updateRoomAgentSet`, async ({ request }) => {
    const body = await request.json() as Record<string, unknown>
    return HttpResponse.json({
      success: true,
      room_id: body.room_id,
    })
  }),

  http.post(`${roomCenter}/suggestAgents`, async () => {
    return HttpResponse.json({
      success: true,
      routing_strategy: 'single',
      reasoning: 'Test reasoning',
      suggested_agents: [
        {
          agent_id: 'agent-1',
          name: 'Test Agent',
          reason: 'Best match for the query',
        },
      ],
    })
  }),

  // SSE API handlers
  http.get(`${sseBase}/room/:roomId/status`, async () => {
    return HttpResponse.json({
      room_id: 'test-room',
      active_connections: 1,
      status: 'active',
    })
  }),

  http.post(`${sseBase}/message/:messageId/cancel`, async ({ params }) => {
    return HttpResponse.json({
      success: true,
      message_id: params.messageId,
      message: 'Message cancelled successfully',
    })
  }),

  // Agent API handlers
  http.post(`${agent}/registerAgent`, async () => {
    return HttpResponse.json({ success: true, agents: [] })
  }),

  http.get(`${agent}/getAgent/me`, async () => {
    return HttpResponse.json({ success: true, agents: [] })
  }),

  http.post(`${agent}/deleteAgent`, async () => {
    return HttpResponse.json({ success: true })
  }),

  http.post(`${agent}/getAgentCardFromUrl`, async () => {
    return HttpResponse.json({ success: true, agents: [] })
  }),

  http.get(`${agent}/getAgent/:agentId`, async () => {
    return HttpResponse.json({
      success: true,
      agents: [{ agent_id: 'agent-1', agent_status: 'active' }],
    })
  }),

  http.get(`${agent}/getAllAgents`, async () => {
    return HttpResponse.json({ success: true, agents: [] })
  }),

  // Orchestration Center API handlers
  http.post(`${orchestrationCenter}/decomposeTask`, async () => {
    return HttpResponse.json({ success: true })
  }),

  http.post(`${orchestrationCenter}/assignAgentsToMetaTasks`, async () => {
    return HttpResponse.json({ success: true })
  }),

  http.post(`${orchestrationCenter}/assignAgentToMetaTask`, async () => {
    return HttpResponse.json({ success: true })
  }),

  http.post(`${orchestrationCenter}/runWorkflow`, async () => {
    return HttpResponse.json({ success: true })
  }),

  http.post(`${orchestrationCenter}/retryMetaTask`, async () => {
    return HttpResponse.json({ success: true })
  }),

  http.post(`${orchestrationCenter}/summarizeMetaTaskForBaseTask`, async () => {
    return HttpResponse.json({ success: true })
  }),

  http.post(`${orchestrationCenter}/processRoomUserMessage`, async () => {
    return HttpResponse.json({ success: true })
  }),

  // Task Center API handlers
  http.get(`${task}/queryTask/:taskId`, async () => {
    return HttpResponse.json({ success: true, task: {} })
  }),

  http.get(`${task}/queryBaseTask/:taskId`, async () => {
    return HttpResponse.json({ success: true, task: {} })
  }),

  http.get(`${task}/getAllSessions/:userName`, async () => {
    return HttpResponse.json({ success: true, sessions: [] })
  }),

  http.get(`${task}/getBaseTasksBySessionId/:sessionId`, async () => {
    return HttpResponse.json({ success: true, tasks: [] })
  }),

  http.get(`${task}/getMetaTasksByParentTaskId/:parentTaskId`, async () => {
    return HttpResponse.json({ success: true, tasks: [] })
  }),

  // Health check
  http.get(`${getApiUrl('health')}`, async () => {
    return HttpResponse.json({
      status: 'healthy',
      timestamp: new Date().toISOString(),
    })
  }),
]

// Error response handlers for testing error scenarios
export const errorHandlers = {
  networkError: http.post(`${roomCenter}/sendMessage`, () => {
    return HttpResponse.error()
  }),

  serverError: http.post(`${roomCenter}/sendMessage`, () => {
    return HttpResponse.json(
      { success: false, error: 'Internal server error', status_code: 500 },
      { status: 500 }
    )
  }),

  authError: http.post(`${roomCenter}/sendMessage`, () => {
    return HttpResponse.json(
      { success: false, error: 'Unauthorized', status_code: 401 },
      { status: 401 }
    )
  }),

  rateLimitError: http.post(`${roomCenter}/sendMessage`, () => {
    return HttpResponse.json(
      {
        success: false,
        error: 'Rate limit exceeded',
        status_code: 429,
        retry_after_seconds: 60,
      },
      { status: 429 }
    )
  }),
}

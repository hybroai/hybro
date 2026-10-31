import { http, HttpResponse } from 'msw'
import { getApiUrl } from '@/lib/utils'
import { createAgent } from '../fixtures'

const rooms = getApiUrl('rooms')
const agent = getApiUrl('agent')
const orchestrationCenter = getApiUrl('orchestrationCenter')
const task = getApiUrl('task')
const sseBase = getApiUrl('sse')
const sessions = getApiUrl('sessions')

const sessionTimes = {
  opened_at: '2026-10-14T09:00:00Z',
  last_activity_at: '2026-10-14T09:05:00Z',
}

export const handlers = [
  // Setup status: a working provider, so no banner unless a test overrides it.
  http.get(getApiUrl('setup/status'), () => HttpResponse.json({
    state: 'configured', provider: 'openai', auth: 'api_key', text_model: 'gpt-5-mini',
    image_model: 'gpt-image-1', image_size: 'auto', last_error: null,
  })),

  // Session API handlers
  http.get(sessions, () => HttpResponse.json({
    sessions: [{
      session_id: 'sess-1',
      client_label: 'Buyer agent',
      agents: [{ agent_id: 'agent-1', name: 'Travel Planner' }],
      state: 'waiting_on_owner',
      paused_agent_id: 'agent-1',
      conversation_id: 'conv-1',
      closed_at: null,
      ...sessionTimes,
    }],
  })),

  http.get(`${sessions}/:id/record`, ({ params }) => HttpResponse.json({
    session_id: params.id,
    state: 'waiting_on_owner',
    paused_agent_id: 'agent-1',
    reason: null,
    agents: [{ agent_id: 'agent-1', name: 'Travel Planner' }],
    client_label: 'Buyer agent',
    status: 'open',
    conversation_id: 'conv-1',
    closed_at: null,
    ...sessionTimes,
  })),

  http.post(`${sessions}/:id/close`, ({ params }) => HttpResponse.json({
    session_id: params.id,
    status: 'closed',
  })),

  // Room API handlers
  http.post(rooms, async ({ request }) => {
    const body = await request.json() as Record<string, unknown>
    return HttpResponse.json({
      success: true,
      conversation_id: 'test-room-id',
      room: {
        conversation_id: 'test-room-id',
        room_name: body.room_name || 'Test Room',
        room_owner_id: 'test-user',
        room_owner_name: body.room_owner_name || 'Test User',
        room_agent_set: {},
        room_created_at: new Date().toISOString(),
      },
    })
  }),

  http.get(`${rooms}/:conversationId`, ({ params }) => {
    return HttpResponse.json({
      success: true,
      conversation_id: params.conversationId,
      room: {
        conversation_id: params.conversationId,
        room_name: 'Test Room',
        room_owner_id: 'test-user',
        room_owner_name: 'Test User',
        room_agent_set: {},
        room_created_at: new Date().toISOString(),
      },
    })
  }),

  http.get(`${rooms}/:conversationId/active-runs`, ({ params }) => {
    return HttpResponse.json({
      success: true,
      conversation_id: params.conversationId,
      active_runs: [],
    })
  }),

  http.post(`${rooms}/:conversationId/messages`, async ({ request, params }) => {
    const body = await request.json() as Record<string, unknown>
    return HttpResponse.json({
      success: true,
      conversation_id: params.conversationId,
      message_id: `msg-${Date.now()}`,
      user_id: body.user_id,
      user_name: body.user_name,
      message: {
        conversation_id: params.conversationId,
        message_id: `msg-${Date.now()}`,
        message_type: 'user',
        message_content: {
          message_text: body.user_input,
        },
        message_created_at: new Date().toISOString(),
      },
    })
  }),

  http.get(`${rooms}/:conversationId/messages`, ({ params }) => {
    return HttpResponse.json({
      success: true,
      conversation_id: params.conversationId,
      message_list: [],
    })
  }),

  http.post(getApiUrl('agent-suggestions'), async () => {
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
  http.get(`${sseBase}/room/:conversationId/status`, async () => {
    return HttpResponse.json({
      conversation_id: 'test-room',
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
    return HttpResponse.json({ success: true, agent: createAgent() })
  }),

  http.get(`${agent}/getAgent/me`, async () => {
    return HttpResponse.json({ success: true, agents: [] })
  }),

  http.post(`${agent}/deleteAgent`, async () => {
    return HttpResponse.json({ success: true })
  }),

  http.post(`${agent}/getAgentCardFromUrl`, async () => {
    const agent = createAgent()
    const card = {
      name: agent.name,
      description: agent.description,
      version: agent.version,
      provider_organization: agent.provider_organization,
      icon_url: agent.icon_url,
      documentation_url: agent.documentation_url,
      skills: agent.skills,
      capabilities: agent.capabilities,
      default_input_modes: agent.default_input_modes,
      default_output_modes: agent.default_output_modes,
    }
    return HttpResponse.json({ success: true, card })
  }),

  http.get(`${agent}/getAgent/:agentId`, async () => {
    return HttpResponse.json({ success: true, agent: createAgent() })
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
  networkError: http.post(`${rooms}/:conversationId/messages`, () => {
    return HttpResponse.error()
  }),

  serverError: http.post(`${rooms}/:conversationId/messages`, () => {
    return HttpResponse.json(
      { success: false, error: 'Internal server error', status_code: 500 },
      { status: 500 }
    )
  }),

  authError: http.post(`${rooms}/:conversationId/messages`, () => {
    return HttpResponse.json(
      { success: false, error: 'Unauthorized', status_code: 401 },
      { status: 401 }
    )
  }),

  rateLimitError: http.post(`${rooms}/:conversationId/messages`, () => {
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

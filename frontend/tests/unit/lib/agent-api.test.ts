import { describe, it, expect, beforeEach } from 'vitest'
import { http, HttpResponse, delay } from 'msw'
import { server } from '../../setup/msw-server'
import { getApiUrl } from '@/lib/utils'
import {
  discoverLocalAgents,
  registerAgent,
  getAgentsByProviderId,
  deleteAgent,
  getAgentCardFromUrl,
  getAgent,
  getAllAgents,
} from '@/lib/api/agent'

const BASE = getApiUrl('agent')
const LOCAL_AGENTS_BASE = getApiUrl('local-agents')

describe('Agent API', () => {
  beforeEach(() => {
    server.resetHandlers()
  })

  // ─── discoverLocalAgents ─────────────────────────────────────

  it('should POST to the local agent discovery endpoint', async () => {
    server.use(
      http.post(`${LOCAL_AGENTS_BASE}/discovery`, () =>
        HttpResponse.json({ trigger: 'manual', agents_found: 2 }),
      ),
    )

    const result = await discoverLocalAgents()

    expect(result.trigger).toBe('manual')
    expect(result.agents_found).toBe(2)
  })

  // ─── registerAgent ───────────────────────────────────────────

  describe('registerAgent', () => {
    it('should POST to /registerAgent with the request body', async () => {
      let capturedBody: Record<string, unknown> | null = null
      server.use(
        http.post(`${BASE}/registerAgent`, async ({ request }) => {
          capturedBody = (await request.json()) as Record<string, unknown>
          return HttpResponse.json({ success: true, agents: [] })
        }),
      )

      const req = { agent_name: 'My Agent', url: 'http://example.com' }
      const result = await registerAgent(req as never)

      expect(result.success).toBe(true)
      expect(capturedBody).toMatchObject(req)
    })

    it('should handle server errors', async () => {
      server.use(
        http.post(`${BASE}/registerAgent`, () =>
          HttpResponse.json({ error: 'fail' }, { status: 500 }),
        ),
      )

      await expect(registerAgent({} as never)).rejects.toThrow()
    })

    it('should handle network errors', async () => {
      server.use(
        http.post(`${BASE}/registerAgent`, () => HttpResponse.error()),
      )

      await expect(registerAgent({} as never)).rejects.toThrow()
    })
  })

  // ─── getAgentsByProviderId ───────────────────────────────────

  describe('getAgentsByProviderId', () => {
    it('should GET /getAgent/me and return agents', async () => {
      const agents = [{ agent_id: 'a-1' }, { agent_id: 'a-2' }]
      server.use(
        http.get(`${BASE}/getAgent/me`, () =>
          HttpResponse.json({ success: true, agents }),
        ),
      )

      const result = await getAgentsByProviderId()

      expect(result.success).toBe(true)
      expect(result.agents).toHaveLength(2)
    })

    it('should handle server errors', async () => {
      server.use(
        http.get(`${BASE}/getAgent/me`, () =>
          HttpResponse.json({ error: 'fail' }, { status: 500 }),
        ),
      )

      await expect(getAgentsByProviderId()).rejects.toThrow()
    })
  })

  // ─── deleteAgent ─────────────────────────────────────────────

  describe('deleteAgent', () => {
    it('should POST to /deleteAgent with the request body', async () => {
      let capturedBody: Record<string, unknown> | null = null
      server.use(
        http.post(`${BASE}/deleteAgent`, async ({ request }) => {
          capturedBody = (await request.json()) as Record<string, unknown>
          return HttpResponse.json({ success: true })
        }),
      )

      const req = { agent_id: 'agent-42' }
      const result = await deleteAgent(req as never)

      expect(result.success).toBe(true)
      expect(capturedBody).toMatchObject(req)
    })

    it('should handle server errors', async () => {
      server.use(
        http.post(`${BASE}/deleteAgent`, () =>
          HttpResponse.json({ error: 'fail' }, { status: 500 }),
        ),
      )

      await expect(deleteAgent({} as never)).rejects.toThrow()
    })
  })

  // ─── getAgentCardFromUrl ─────────────────────────────────────

  describe('getAgentCardFromUrl', () => {
    it('should POST to /getAgentCardFromUrl with the request body', async () => {
      let capturedBody: Record<string, unknown> | null = null
      server.use(
        http.post(`${BASE}/getAgentCardFromUrl`, async ({ request }) => {
          capturedBody = (await request.json()) as Record<string, unknown>
          return HttpResponse.json({
            success: true,
            agents: [{ agent_id: 'found-1' }],
          })
        }),
      )

      const req = { url: 'http://agent.example.com' }
      const result = await getAgentCardFromUrl(req as never)

      expect(result.success).toBe(true)
      expect(capturedBody).toMatchObject(req)
    })

    it('should handle server errors', async () => {
      server.use(
        http.post(`${BASE}/getAgentCardFromUrl`, () =>
          HttpResponse.json({ error: 'fail' }, { status: 500 }),
        ),
      )

      await expect(getAgentCardFromUrl({} as never)).rejects.toThrow()
    })
  })

  // ─── getAgent ────────────────────────────────────────────────

  describe('getAgent', () => {
    it('should GET /getAgent/:agentId and return agent data', async () => {
      let capturedUrl = ''
      server.use(
        http.get(`${BASE}/getAgent/:agentId`, ({ request }) => {
          capturedUrl = request.url
          return HttpResponse.json({
            success: true,
            agents: [{ agent_id: 'agent-99' }],
          })
        }),
      )

      const result = await getAgent('agent-99')

      expect(result.success).toBe(true)
      expect(capturedUrl).toContain('/getAgent/agent-99')
    })

    it('should support abort signal', async () => {
      const controller = new AbortController()
      server.use(
        http.get(`${BASE}/getAgent/:agentId`, async () => {
          controller.abort()
          await delay('infinite')
          return HttpResponse.json({ success: true })
        }),
      )

      await expect(
        getAgent('agent-1', controller.signal),
      ).rejects.toThrow()
    })

    it('should handle server errors', async () => {
      server.use(
        http.get(`${BASE}/getAgent/:agentId`, () =>
          HttpResponse.json({ error: 'fail' }, { status: 500 }),
        ),
      )

      await expect(getAgent('agent-1')).rejects.toThrow()
    })
  })

  // ─── getAllAgents ────────────────────────────────────────────

  describe('getAllAgents', () => {
    it('should GET /getAllAgents and return the full list', async () => {
      const agents = [{ agent_id: 'a-1' }, { agent_id: 'a-2' }]
      server.use(
        http.get(`${BASE}/getAllAgents`, () =>
          HttpResponse.json({ success: true, agents }),
        ),
      )

      const result = await getAllAgents()

      expect(result.success).toBe(true)
      expect(result.agents).toHaveLength(2)
    })

    it('should support abort signal', async () => {
      const controller = new AbortController()
      server.use(
        http.get(`${BASE}/getAllAgents`, async () => {
          controller.abort()
          await delay('infinite')
          return HttpResponse.json({ success: true, agents: [] })
        }),
      )

      await expect(
        getAllAgents({ signal: controller.signal }),
      ).rejects.toThrow()
    })

    it('requests only active agents through the shared endpoint', async () => {
      let activeOnly: string | null = null
      const agents = [{ agent_id: 'a-1', agent_status: 'active' }]
      server.use(
        http.get(`${BASE}/getAllAgents`, ({ request }) => {
          activeOnly = new URL(request.url).searchParams.get('active_only')
          return HttpResponse.json({ success: true, agents })
        }),
      )

      const result = await getAllAgents({ activeOnly: true })

      expect(activeOnly).toBe('true')
      expect(result.success).toBe(true)
      expect(result.agents).toHaveLength(1)
    })

    it('should handle server errors', async () => {
      server.use(
        http.get(`${BASE}/getAllAgents`, () =>
          HttpResponse.json({ error: 'fail' }, { status: 500 }),
        ),
      )

      await expect(getAllAgents()).rejects.toThrow()
    })
  })
})

import { describe, expect, it } from 'vitest'
import { buildAccessMaterial } from '@/components/connect/access-material'
import { buildMcpConfig } from '@/lib/mcp'

const scope = { id: 'g1', name: 'Research' }

describe('access materials', () => {
  it('omits group_id for whole-network discovery', () => {
    const material = buildAccessMaterial('api', 'https://hybro.example', '/custom/api')
    expect(material.configuration).toContain('https://hybro.example/custom/api/agents/discovery')
    expect(material.configuration).not.toContain('group_id')
    expect(material.configuration).not.toMatch(/authorization|bearer|access_token/i)
  })

  it('builds requests from an empty same-origin base without parsing it as a URL', () => {
    const material = buildAccessMaterial('api', '', '/api/v1', scope)
    expect(material.configuration).toContain("'/api/v1/agents/discovery?group_id=g1'")
    expect(material.toolCalls).toContain("'/api/v1/agents/messages'")
    expect(material.scopeCall).toBe('GET /api/v1/agents/discovery?group_id=g1')
  })

  it('safely quotes the configured API address for the shell', () => {
    const material = buildAccessMaterial('api', "https://hybro.example/it's", '/api/v1')
    expect(material.configuration).toContain(`it'"'"'s`)
  })

  it('never puts the scope in the MCP connection configuration', () => {
    const url = 'http://127.0.0.1:8001/mcp'
    expect(buildAccessMaterial('mcp', url, '/api/v1', scope).configuration).toBe(buildMcpConfig(url))
    expect(buildAccessMaterial('mcp', url, '/api/v1', scope).configuration).not.toContain('g1')
  })

  it('scopes the MCP prompt and tool calls per call', () => {
    const material = buildAccessMaterial('mcp', 'http://127.0.0.1:8001/mcp', '/api/v1', scope)
    expect(material.prompt).toContain('Research')
    expect(material.prompt).toContain('subnet_id "g1"')
    expect(material.prompt).toContain('every find_agents, discover_agents and send_message call')
    expect(material.prompt).toContain('within a session')
    expect(material.toolCalls).toContain('find_agents({"query": "<capability keywords>", "subnet_id": "g1"})')
    expect(material.toolCalls).toContain('send_message({')
    expect(material.toolCalls).toContain('"subnet_id": "g1"')
    expect(material.toolCalls).not.toContain('group_id')
  })

  it('shows how to continue and check a session', () => {
    const material = buildAccessMaterial('mcp', 'http://127.0.0.1:8001/mcp', '/api/v1')
    expect(material.toolCalls).toContain('check_session({"session_id": "<session_id from send_message>"})')
    expect(material.prompt).toContain('session_id')
  })

  it('keeps the whole-network MCP prompt unscoped', () => {
    const material = buildAccessMaterial('mcp', 'http://127.0.0.1:8001/mcp', '/api/v1')
    expect(material.prompt).toContain('list_subnets({})')
    expect(material.prompt).toContain('find_agents({"query": "<capability keywords>"})')
    expect(material.toolCalls).not.toContain('subnet_id')
  })

  it('scopes API discovery and the message body', () => {
    const material = buildAccessMaterial('api', 'https://hybro.example', '/api/v1', scope)
    expect(material.configuration).toContain('/api/v1/agents/discovery?group_id=g1')
    expect(material.toolCalls).toContain('/api/v1/agents/messages')
    expect(material.toolCalls).toContain('"group_id": "g1"')
    expect(material.prompt).toContain('group_id=g1')
  })

  it('builds an API search request with optional subnet scope', () => {
    const plain = buildAccessMaterial('api', 'https://hybro.example', '/api/v1')
    expect(plain.searchRequest).toBe("curl --get \\\n  --data-urlencode 'query=<capability keywords>' \\\n  'https://hybro.example/api/v1/agents/search'")
    const scoped = buildAccessMaterial('api', 'https://hybro.example', '/api/v1', scope)
    expect(scoped.searchRequest).toContain("--data-urlencode 'group_id=g1'")
  })
})

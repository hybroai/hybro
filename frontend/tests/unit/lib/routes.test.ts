import { describe, expect, it } from 'vitest'
import { routes } from '@/lib/routes'

describe('canonical portal routes', () => {
  it('encodes room and agent identifiers as single path segments', () => {
    expect(routes.room('room/id')).toBe('/room/room%2Fid')
    expect(routes.networkAgent('agent/id')).toBe('/network/agents/agent%2Fid')
  })

  it('exposes the new information architecture', () => {
    expect(routes.overview).toBe('/')
    expect(routes.network).toBe('/network')
    expect(routes.registerAgent).toBe('/network/agents/new')
    expect(routes.connectMcp).toBe('/connect/mcp')
    expect(routes.connectApi).toBe('/connect/api')
    expect(routes.rules).toBe('/rules')
  })

  it('builds workspace links with and without a query', () => {
    expect(routes.networkWith('')).toBe('/network')
    expect(routes.networkWith('select=agent%3Aa')).toBe('/network?select=agent%3Aa')
  })
})

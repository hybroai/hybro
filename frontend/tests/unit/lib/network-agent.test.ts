import { describe, expect, it } from 'vitest'
import { projectAgent } from '@/lib/network-agent'
import { createAgent } from '../../fixtures'

describe('projectAgent', () => {
  it('projects card fields, tags, source, and room call count', () => {
    const agent = createAgent({
      agent_id: 'a',
      status: 'active',
      source: 'local',
      call_count: 7,
      name: 'Planner',
      description: 'Plans',
      version: '1.2.0',
      icon_url: 'https://x/icon.png',
      provider_organization: 'Acme',
      skills: [{ id: 's', name: 'Plan', description: '', tags: ['trip', 'travel'], examples: [] }],
    })
    expect(projectAgent(agent)).toEqual({
      id: 'a', name: 'Planner', description: 'Plans', skills: ['Plan'], tags: ['trip', 'travel'],
      status: 'active', iconUrl: 'https://x/icon.png', provider: 'Acme', version: '1.2.0', source: 'local',
      roomCalls: 7,
    })
  })

  it('projects a bundled agent and the setup it waits for', () => {
    const agent = createAgent({ agent_id: 'i', source: 'bundled', status: 'inactive', inactive_reason: 'image_model_not_configured' })

    expect(projectAgent(agent)).toMatchObject({ source: 'bundled', status: 'inactive', inactiveReason: 'image_model_not_configured' })
    expect(projectAgent(createAgent({ inactive_reason: null })).inactiveReason).toBeUndefined()
  })

  it('falls back to the provider name and agent id when the card is sparse', () => {
    const agent = createAgent({ agent_id: 'b', name: '', provider_name: 'Owner', version: null })

    expect(projectAgent(agent)).toMatchObject({ name: 'b', provider: 'Owner', version: undefined })
  })
})

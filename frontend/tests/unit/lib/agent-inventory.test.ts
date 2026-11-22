import { describe, expect, it } from 'vitest'
import { mergeAgentDirectory, mergeAgents } from '@/lib/agent-inventory'
import type { Agent } from '@/lib/types/response'

const agent = (agent_id: string, status: Agent['status'], source: Agent['source']) =>
  ({ agent_id, name: agent_id, status, source, skills: [] }) as unknown as Agent

describe('agent inventory', () => {
  const discovered = [agent('local_on', 'active', 'local'), agent('local_off', 'inactive', 'local'), agent('gone', 'deleted', 'cloud')]
  const registered = [agent('remote_off', 'inactive', 'cloud')]

  it('keeps every agent that is not deleted in the directory, running or not', () => {
    expect(mergeAgentDirectory(discovered, registered).map(item => item.agent_id)).toEqual(['local_on', 'local_off', 'remote_off'])
  })

  it('still hides a bundled agent that is not running from the visible list', () => {
    expect(mergeAgents(discovered, registered).map(item => item.agent_id)).toEqual(['local_on', 'remote_off'])
  })
})

import type { Agent } from '@/lib/types/response'
import type { NetworkAgent } from '@/components/networks/types'

export type ProjectableAgent = Pick<
  Agent,
  | 'agent_id'
  | 'name'
  | 'description'
  | 'skills'
  | 'status'
  | 'inactive_reason'
  | 'source'
  | 'icon_url'
  | 'provider_organization'
  | 'provider_name'
  | 'version'
  | 'call_count'
>

export function projectAgent(agent: ProjectableAgent): NetworkAgent {
  return {
    id: agent.agent_id,
    name: agent.name || agent.agent_id,
    description: agent.description ?? '',
    skills: agent.skills.map((skill) => skill.name),
    tags: [...new Set(agent.skills.flatMap((skill) => skill.tags))],
    status: agent.status,
    inactiveReason: agent.inactive_reason ?? undefined,
    iconUrl: agent.icon_url || undefined,
    provider: agent.provider_organization || agent.provider_name || undefined,
    version: agent.version || undefined,
    source: agent.source,
    roomCalls: agent.call_count,
  }
}

export function placeholderAgent(id: string, description: string, status: NetworkAgent['status']): NetworkAgent {
  return { id, name: id, description, skills: [], tags: [], status, roomCalls: 0 }
}

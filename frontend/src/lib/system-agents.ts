/**
 * Built-in system agents that are not real A2A agents in the database.
 * These are synthetic agent IDs created by the backend's RoomCoordinatorService
 * to orchestrate and author system messages.
 */

export interface SystemAgentInfo {
  name: string
  description: string
}

export const SYSTEM_AGENTS: Record<string, SystemAgentInfo> = {
  'system:hybro': {
    name: 'HYBRO AI',
    description:
      'The built-in platform agent that orchestrates workflows, synthesizes results, and asks clarifying questions.',
  },
  'system:clarifier': {
    name: 'HYBRO AI',
    description:
      'A built-in agent that facilitates human-in-the-loop interactions, collecting clarifications and confirmations from the user.',
  },
}

export function isSystemAgent(agentId: string | undefined): boolean {
  return !!agentId && (agentId.startsWith('system:') || agentId in SYSTEM_AGENTS)
}

export function isSummarySystemAgent(agentId: string | undefined): boolean {
  return agentId === 'system:hybro' || agentId === 'summary' || agentId === 'debate_summary'
}

/** True for supervisor agents that issue HITL clarification questions (not synthesis). */
export function isSupervisorClarifyAgent(agentId: string | undefined): boolean {
  return agentId === 'system:clarifier'
}

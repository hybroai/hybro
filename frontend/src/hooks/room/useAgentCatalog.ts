import { useCallback, useEffect, useMemo, useRef } from 'react'
import { skipToken, useQuery } from '@tanstack/react-query'
import { getAllAgents } from '@/lib/api/agent'
import { SYSTEM_AGENTS } from '@/lib/system-agents'
import type { Agent } from '@/lib/types/agent'

const AGENT_CATALOG_KEY = ['agents', 'all'] as const

export function useCachedAgentCatalog(): Agent[] | undefined {
  return useQuery<Agent[]>({
    queryKey: AGENT_CATALOG_KEY,
    queryFn: skipToken,
  }).data
}

export function useAgentCatalog(userId?: string, getToken?: () => Promise<string | null>) {
  const agentNameCache = useRef<{ [agentId: string]: string }>({})

  const allAgentsQuery = useQuery<Agent[], Error>({
    queryKey: AGENT_CATALOG_KEY,
    staleTime: 1000 * 60 * 60 * 24,
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
    retry: 0,
    enabled: !!userId,
    queryFn: async ({ signal }): Promise<Agent[]> => {
      try {
        const res = await getAllAgents(signal, 15000, getToken)
        if (!res.success || !res.agents) {
          throw new Error(res.error || 'Failed to load agents')
        }
        return res.agents
      } catch (error: unknown) {
        if (error instanceof Error && error.name === 'AbortError') {
          return []
        }
        console.error('❌ Failed to load agents:', error)
        throw error
      }
    },
  })

  const availableAgents = useMemo(
    () => (allAgentsQuery.data || []).filter(a => a.agent_status === 'active'),
    [allAgentsQuery.data],
  )

  const getAgentName = useCallback(async (agentId: string): Promise<string> => {
    if (SYSTEM_AGENTS[agentId]) {
      return SYSTEM_AGENTS[agentId].name
    }
    if (agentNameCache.current[agentId]) {
      return agentNameCache.current[agentId]
    }
    const agents = allAgentsQuery.data
    if (agents) {
      const found = agents.find(a => a.agent_id === agentId)
      if (found?.agent_card?.name) {
        const name = found.agent_card.name
        agentNameCache.current[agentId] = name
        return name
      }
    }
    return `Agent ${agentId.slice(0, 6)}`
  }, [allAgentsQuery.data])

  const getAgentSource = useCallback((agentId: string | undefined): 'cloud' | 'local' | 'hub' | undefined => {
    if (!agentId) return undefined
    const agents = allAgentsQuery.data
    if (!agents) return undefined
    const found = agents.find(a => a.agent_id === agentId)
    if (found) {
      if (found.source === 'hub' || found.hub_id) return 'hub'
      return (found.source as 'cloud' | 'local' | 'hub') || 'cloud'
    }
    return undefined
  }, [allAgentsQuery.data])

  const getAgentIconUrl = useCallback((agentId: string | undefined): string | null => {
    if (!agentId) return null
    const agents = allAgentsQuery.data
    if (!agents) return null
    const found = agents.find(a => a.agent_id === agentId)
    return found?.agent_card?.iconUrl ?? null
  }, [allAgentsQuery.data])

  // Refresh agent name cache when agent catalog loads
  useEffect(() => {
    if (allAgentsQuery.data) {
      allAgentsQuery.data.forEach((agent: Agent) => {
        if (agent.agent_id && agent.agent_card?.name) {
          agentNameCache.current[agent.agent_id] = agent.agent_card.name
        }
      })
    }
  }, [allAgentsQuery.data])

  const primeAgentNameCache = useCallback((entries: Record<string, string>) => {
    Object.assign(agentNameCache.current, entries)
  }, [])

  const resetAgentNameCache = useCallback(() => {
    agentNameCache.current = {}
  }, [])

  return {
    availableAgents,
    allAgentsData: allAgentsQuery.data,
    getAgentName,
    getAgentSource,
    getAgentIconUrl,
    primeAgentNameCache,
    resetAgentNameCache,
  }
}

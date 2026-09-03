'use client'

import { useMemo, useState } from 'react'
import { Bot, Plus, RefreshCw, Search } from 'lucide-react'
import { useRouter } from 'next/navigation'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { ConsumerAgentCard } from '@/components/consumer-agent-card'
import { Button } from '@/components/ui/button'
import { banner } from '@/components/ui/banner'
import { Input } from '@/components/ui/input'
import { useAuth } from '@/lib/auth'
import { discoverLocalAgents, getAgentsByProviderId, getAllAgents } from '@/lib/api/agent'
import { routes } from '@/lib/routes'
import type { Agent, AgentCenterResponse } from '@/lib/types'

function isVisibleAgent(agent: Agent): boolean {
  if (agent.agent_status === 'deleted') return false

  if (agent.source === 'hub') {
    return agent.agent_status === 'active' && agent.is_hub_online === true
  }
  if (agent.source === 'local') {
    return agent.agent_status === 'active'
  }

  return true
}

function mergeAgents(discovered: Agent[], registered: Agent[]): Agent[] {
  const agentsById = new Map<string, Agent>()

  for (const agent of discovered) {
    agentsById.set(agent.agent_id, agent)
  }

  // The registered response contains Remote agents that may be inactive and
  // therefore absent from public discovery. It is the authoritative copy.
  for (const agent of registered) {
    agentsById.set(agent.agent_id, agent)
  }

  return [...agentsById.values()].filter(isVisibleAgent)
}

export default function AgentsPage() {
  const router = useRouter()
  const queryClient = useQueryClient()
  const { getToken } = useAuth()
  const [searchTerm, setSearchTerm] = useState('')

  const discoveredQuery = useQuery<AgentCenterResponse>({
    queryKey: ['agents', 'discovered'],
    queryFn: () => getAllAgents({ getToken }),
    refetchInterval: 30_000,
    refetchOnWindowFocus: true,
  })

  const registeredQuery = useQuery<AgentCenterResponse>({
    queryKey: ['agents', 'registered'],
    queryFn: () => getAgentsByProviderId(getToken),
    refetchInterval: 30_000,
    refetchOnWindowFocus: true,
  })

  const discoveryMutation = useMutation({
    mutationFn: () => discoverLocalAgents(getToken),
    onSuccess: async (result) => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['agents', 'discovered'] }),
        queryClient.invalidateQueries({ queryKey: ['agents', 'registered'] }),
      ])
      banner.success(
        result.agents_found === 1
          ? 'Found 1 local agent'
          : `Found ${result.agents_found} local agents`,
      )
    },
    onError: (error) => {
      banner.error('Failed to discover local agents', {
        description: error instanceof Error ? error.message : undefined,
      })
    },
  })

  const agents = useMemo(() => {
    const discovered = discoveredQuery.data?.success
      ? discoveredQuery.data.agents ?? []
      : []
    const registered = registeredQuery.data?.success
      ? registeredQuery.data.agents ?? []
      : []

    return mergeAgents(discovered, registered)
  }, [discoveredQuery.data, registeredQuery.data])

  const filteredAgents = useMemo(() => {
    const query = searchTerm.trim().toLowerCase()
    if (!query) return agents

    return agents.filter((agent) => {
      const card = agent.agent_card
      return (
        card.name.toLowerCase().includes(query) ||
        card.description.toLowerCase().includes(query) ||
        card.skills.some((skill) =>
          skill.name.toLowerCase().includes(query) ||
          skill.tags.some((tag) => tag.toLowerCase().includes(query))
        )
      )
    })
  }, [agents, searchTerm])

  const isLoading = discoveredQuery.isLoading || registeredQuery.isLoading

  return (
    <div className="page-container">
      <div className="page-content space-y-5">
        <div className="flex items-start justify-between gap-4">
          <h1 className="text-2xl font-bold">Agents</h1>
          <div className="flex shrink-0 flex-wrap justify-end gap-2">
            <Button
              className="bg-[hsl(var(--color-hybro-hy))] text-white shadow-sm hover:bg-[hsl(var(--color-hybro-hy-strong))] dark:text-slate-950"
              disabled={discoveryMutation.isPending}
              onClick={() => discoveryMutation.mutate()}
            >
              <RefreshCw
                className={`h-4 w-4 mr-2 ${discoveryMutation.isPending ? 'animate-spin' : ''}`}
              />
              {discoveryMutation.isPending ? 'Discovering...' : 'Discover Local Agents'}
            </Button>
            <Button
              className="btn-brand-gradient shrink-0"
              onClick={() => router.push(routes.registerAgent)}
            >
              <Plus className="h-4 w-4 mr-2" />
              Register Agent
            </Button>
          </div>
        </div>

        <div className="relative">
          <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground" />
          <Input
            aria-label="Search agents"
            placeholder="Search agents..."
            value={searchTerm}
            onChange={(event) => setSearchTerm(event.target.value)}
            className="h-10 pl-9 text-sm"
          />
        </div>

        {isLoading && agents.length === 0 ? (
          <div className="flex items-center justify-center gap-3 py-16 text-muted-foreground">
            <RefreshCw className="h-5 w-5 animate-spin" />
            <span>Loading agents...</span>
          </div>
        ) : filteredAgents.length > 0 ? (
          <div className="grid grid-auto-fill-cards gap-4">
            {filteredAgents.map((agent) => (
              <ConsumerAgentCard key={agent.agent_id} agent={agent} />
            ))}
          </div>
        ) : (
          <div className="flex flex-col items-center justify-center py-16 gap-3 text-center">
            <Bot className="h-10 w-10 text-muted-foreground/40" />
            <p className="text-sm text-muted-foreground">
              {agents.length === 0
                ? 'No Remote or Local agents are available yet.'
                : 'No agents match your search.'}
            </p>
            {agents.length === 0 ? (
              <Button variant="outline" onClick={() => router.push(routes.registerAgent)}>
                <Plus className="h-4 w-4 mr-2" />
                Register your first agent
              </Button>
            ) : (
              <Button variant="outline" size="sm" onClick={() => setSearchTerm('')}>
                Clear search
              </Button>
            )}
          </div>
        )}
      </div>
    </div>
  )
}

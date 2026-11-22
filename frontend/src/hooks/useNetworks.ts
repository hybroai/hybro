'use client'

import { useCallback, useMemo } from 'react'
import { useIsMutating, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useAuth } from '@/lib/auth'
import { getAgentsByProviderId, getAllAgents } from '@/lib/api/agent'
import { mergeAgentDirectory, mergeAgents } from '@/lib/agent-inventory'
import { createAgentGroup, deleteAgentGroup, listAgentGroups, updateAgentGroup } from '@/lib/api/agent-group'
import { isBuiltinGroup } from '@/lib/types/agent-group'
import type {
  AgentGroup,
  AgentGroupCreateRequest,
  AgentGroupResponse,
  AgentGroupUpdateRequest
} from '@/lib/types/agent-group'
import type { Agent } from '@/lib/types/response'
import { placeholderAgent, projectAgent } from '@/lib/network-agent'
import type { NetworkFormValues } from '@/components/networks/types'

const EMPTY_NETWORKS: AgentGroup[] = []
const EMPTY_AGENTS: Agent[] = []
const networksKey = (ownerId: string | undefined) => ['networks', ownerId, 'groups'] as const

function isNetwork(group: AgentGroup) {
  return group.type !== 'builtin' && !isBuiltinGroup(group.group_id)
}

function isAgentGroup(value: unknown): value is AgentGroup {
  if (!value || typeof value !== 'object') return false
  const group = value as Partial<AgentGroup>
  return (
    typeof group.group_id === 'string' &&
    typeof group.name === 'string' &&
    (group.type === 'user' || group.type === 'builtin') &&
    (group.owner_id === null || typeof group.owner_id === 'string') &&
    Array.isArray(group.agents) &&
    group.agents.every((id) => typeof id === 'string')
  )
}

function confirmedGroup(response: AgentGroupResponse): AgentGroup {
  if (!response.success || !isAgentGroup(response.group)) {
    throw new Error(response.error || 'The server did not confirm the network changes.')
  }
  return response.group
}

type SaveRequest =
  | { kind: 'create'; request: AgentGroupCreateRequest }
  | { kind: 'update'; request: AgentGroupUpdateRequest }

type SaveVariables = SaveRequest & { ownerId: string }

export function useNetworks() {
  const { userId, getToken, isLoaded, isSignedIn } = useAuth()
  const queryClient = useQueryClient()
  const enabled = isLoaded && isSignedIn && Boolean(userId)
  const mutationKey = ['networks', userId, 'mutation'] as const
  const pending = useIsMutating({ mutationKey }) > 0

  const groupsQuery = useQuery({
    queryKey: networksKey(userId),
    enabled,
    retry: false,
    queryFn: async ({ signal }) => {
      // This client has no signal parameter. Consuming the query signal still
      // discards cancelled results on unmount, owner changes, and confirmed writes.
      signal.throwIfAborted()
      const response = await listAgentGroups(userId, getToken)
      signal.throwIfAborted()
      if (!response.success || !Array.isArray(response.groups) || !response.groups.every(isAgentGroup)) {
        throw new Error(response.error || 'Unable to load networks.')
      }
      return response.groups.filter(isNetwork)
    }
  })

  const discoveredQuery = useQuery({
    queryKey: ['networks', userId, 'agents', 'discovered'],
    enabled,
    retry: false,
    refetchOnMount: 'always',
    refetchOnWindowFocus: true,
    queryFn: async ({ signal }) => {
      const response = await getAllAgents({ getToken, signal })
      if (!response.success || !Array.isArray(response.agents)) {
        throw new Error(response.error || 'Unable to load discovered agents.')
      }
      return response.agents
    }
  })

  const registeredQuery = useQuery({
    queryKey: ['networks', userId, 'agents', 'registered'],
    enabled,
    retry: false,
    refetchOnMount: 'always',
    refetchOnWindowFocus: true,
    queryFn: async ({ signal }) => {
      signal.throwIfAborted()
      const response = await getAgentsByProviderId(getToken)
      signal.throwIfAborted()
      if (!response.success || !Array.isArray(response.agents)) {
        throw new Error(response.error || 'Unable to load registered agents.')
      }
      return response.agents
    }
  })

  const saveMutation = useMutation({
    mutationKey,
    scope: { id: `networks:${userId}` },
    retry: false,
    onMutate: ({ ownerId }: SaveVariables) =>
      queryClient.cancelQueries({ queryKey: networksKey(ownerId), exact: true }),
    mutationFn: async (variables: SaveVariables) =>
      confirmedGroup(
        variables.kind === 'create'
          ? await createAgentGroup(variables.request, getToken)
          : await updateAgentGroup(variables.request, getToken)
      ),
    onSuccess: async (group, { ownerId }) => {
      // A focus/manual refresh may have started while the write was pending.
      await queryClient.cancelQueries({ queryKey: networksKey(ownerId), exact: true })
      queryClient.setQueryData<AgentGroup[]>(networksKey(ownerId), (current) => {
        const groups = current ?? EMPTY_NETWORKS
        const existing = groups.some((item) => item.group_id === group.group_id)
        if (!isNetwork(group)) return groups.filter((item) => item.group_id !== group.group_id)
        return existing ? groups.map((item) => (item.group_id === group.group_id ? group : item)) : [...groups, group]
      })
    }
  })

  const removeMutation = useMutation({
    mutationKey,
    scope: { id: `networks:${userId}` },
    retry: false,
    onMutate: ({ ownerId }: { ownerId: string; id: string }) =>
      queryClient.cancelQueries({ queryKey: networksKey(ownerId), exact: true }),
    mutationFn: async ({ id }: { ownerId: string; id: string }) => {
      const response = await deleteAgentGroup(id, getToken)
      if (!response.success) {
        throw new Error(response.error || 'The server did not confirm the network was deleted.')
      }
    },
    onSuccess: async (_, { ownerId, id }) => {
      await queryClient.cancelQueries({ queryKey: networksKey(ownerId), exact: true })
      queryClient.setQueryData<AgentGroup[]>(networksKey(ownerId), (current) =>
        current?.filter((group) => group.group_id !== id)
      )
    }
  })

  const networks = groupsQuery.data ?? EMPTY_NETWORKS
  const catalogConfirmed = discoveredQuery.isSuccess && registeredQuery.isSuccess
  const agents = useMemo(() => {
    const projected = mergeAgents(discoveredQuery.data ?? EMPTY_AGENTS, registeredQuery.data ?? EMPTY_AGENTS).map(
      projectAgent
    )
    const knownIds = new Set(projected.map((agent) => agent.id))
    for (const network of networks) {
      for (const id of network.agents) {
        if (knownIds.has(id)) continue
        knownIds.add(id)
        projected.push(
          placeholderAgent(
            id,
            catalogConfirmed
              ? 'This agent is not in the currently visible directory.'
              : 'This agent’s details could not be verified.',
            catalogConfirmed ? 'unavailable' : 'unknown'
          )
        )
      }
    }
    return projected
  }, [discoveredQuery.data, registeredQuery.data, catalogConfirmed, networks])

  // Every agent that is not deleted, running or not; no network placeholders.
  const directory = useMemo(
    () => mergeAgentDirectory(discoveredQuery.data ?? EMPTY_AGENTS, registeredQuery.data ?? EMPTY_AGENTS).map(projectAgent),
    [discoveredQuery.data, registeredQuery.data]
  )

  const requireOwner = useCallback(() => {
    if (!enabled || !userId) throw new Error('Sign in to manage networks.')
    return userId
  }, [enabled, userId])
  const { mutateAsync: save } = saveMutation
  const { mutateAsync: deleteNetwork } = removeMutation

  const create = useCallback(
    async (values: NetworkFormValues): Promise<AgentGroup> => {
      const ownerId = requireOwner()
      return save({
        ownerId,
        kind: 'create',
        request: { owner_id: ownerId, name: values.name, description: values.description, agents: values.agentIds }
      })
    },
    [requireOwner, save]
  )

  const update = useCallback(
    async (id: string, fields: { name: string; description: string; agents?: string[] }): Promise<AgentGroup> =>
      save({
        ownerId: requireOwner(),
        kind: 'update',
        request: { group_id: id, ...fields }
      }),
    [requireOwner, save]
  )

  const setMembers = useCallback(
    async (id: string, ids: string[]): Promise<AgentGroup> =>
      save({
        ownerId: requireOwner(),
        kind: 'update',
        request: { group_id: id, agents: ids }
      }),
    [requireOwner, save]
  )

  const remove = useCallback(
    async (id: string): Promise<void> => {
      await deleteNetwork({ ownerId: requireOwner(), id })
    },
    [deleteNetwork, requireOwner]
  )

  const { refetch: refetchGroups } = groupsQuery
  const { refetch: refetchDiscovered } = discoveredQuery
  const { refetch: refetchRegistered } = registeredQuery
  const refreshAgents = useCallback(async (): Promise<void> => {
    if (!enabled) return
    await Promise.all([refetchDiscovered({ throwOnError: false }), refetchRegistered({ throwOnError: false })])
  }, [enabled, refetchDiscovered, refetchRegistered])
  const refresh = useCallback(async (): Promise<void> => {
    if (!enabled) return
    await Promise.all([refetchGroups({ throwOnError: false }), refreshAgents()])
  }, [enabled, refetchGroups, refreshAgents])

  return {
    networks,
    agents,
    directory,
    loading: !isLoaded || groupsQuery.isLoading,
    error: groupsQuery.error?.message ?? (isLoaded && !enabled ? 'Sign in to view networks.' : null),
    agentsLoading: !isLoaded || discoveredQuery.isLoading || registeredQuery.isLoading,
    agentsError:
      discoveredQuery.error?.message ??
      registeredQuery.error?.message ??
      (isLoaded && !enabled ? 'Sign in to view agents.' : null),
    refreshing: groupsQuery.isRefetching || discoveredQuery.isFetching || registeredQuery.isFetching,
    pending,
    refresh,
    refreshAgents,
    create,
    update,
    setMembers,
    remove
  }
}

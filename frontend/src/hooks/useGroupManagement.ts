"use client"

import { useState, useEffect, useCallback, useMemo } from "react"
import { listAgentGroups } from "@/lib/api/agent-group"
import { getAllAgents } from "@/lib/api/agent"
import type { AgentGroup, TargetModeDispatchInput } from "@/lib/types/agent-group"
import type { Agent } from "@/lib/types/agent"
import {
  BUILTIN_GROUP_ALL_AGENTS,
  BUILTIN_GROUP_ROOM_TEAM,
  resolveSelectedGroupDispatch,
} from "@/lib/types/agent-group"

interface UseGroupManagementOptions {
  userId?: string
  getToken: () => Promise<string | null>
  isLoaded: boolean
  /**
   * Selection id when no override is active.
   * Use `room_team` for room membership snapshots, `all_agents` for network
   * broadcast, or a saved team id for source-team provenance.
   */
  defaultGroup?: string
  /** Display label for room membership / source-team provenance. */
  defaultGroupName?: string
  /** Dispatch scope used when no explicit override is active. */
  defaultTargetMode?: TargetModeDispatchInput
  /** Room ID for localStorage persistence (room page only) */
  roomId?: string
  /** Called when an action requires authentication but user is not signed in */
  onRequireAuth?: () => void
}

interface GroupManagementState {
  // Group state
  groups: AgentGroup[]
  loadingGroups: boolean
  selectedGroup: string
  selectedGroupName?: string
  isOverride: boolean
  resolvedTargetMode: TargetModeDispatchInput
  /** When set, the selector should offer this as the room-membership menu row. */
  roomMembershipLabel?: string
  // Modal state
  groupManagementOpen: boolean
  groupAction: { type: 'create' | 'edit' | 'delete'; group?: AgentGroup } | null
  // Agent state (for modal & mentions)
  availableAgents: Agent[]
  loadingAgents: boolean
  agentsError: string | null
}

interface GroupManagementActions {
  // Group management
  handleGroupsChange: () => Promise<void>
  handleCreateGroup: () => void
  handleEditGroup: (group: AgentGroup) => void
  handleDeleteGroup: (group: AgentGroup) => void
  handleGroupCreated: (group: AgentGroup) => void
  handleGroupChange: (groupId: string, groupName?: string) => void
  handleClearOverride: () => void
  setGroupManagementOpen: (open: boolean) => void
  setGroupAction: (action: { type: 'create' | 'edit' | 'delete'; group?: AgentGroup } | null) => void
  // Agent loading
  loadAvailableAgents: () => Promise<void>
  setAvailableAgents: (agents: Agent[]) => void
}

function isBuiltinSelection(groupId: string): boolean {
  return groupId === BUILTIN_GROUP_ALL_AGENTS || groupId === BUILTIN_GROUP_ROOM_TEAM
}

export function useGroupManagement(
  options: UseGroupManagementOptions
): GroupManagementState & GroupManagementActions {
  const {
    userId,
    getToken,
    isLoaded,
    defaultGroup,
    defaultGroupName,
    defaultTargetMode,
    roomId,
    onRequireAuth,
  } = options

  // Group state
  const [groups, setGroups] = useState<AgentGroup[]>([])
  const [loadingGroups, setLoadingGroups] = useState(false)
  const [groupsLoadStatus, setGroupsLoadStatus] = useState<'idle' | 'loading' | 'success' | 'error'>('idle')
  const [overrideGroup, setOverrideGroup] = useState<string | null>(null)
  const [overrideGroupName, setOverrideGroupName] = useState<string | null>(null)

  useEffect(() => {
    if (!roomId) {
      setOverrideGroup(null)
      setOverrideGroupName(null)
      return
    }

    setOverrideGroup(localStorage.getItem(`room-${roomId}-override-group`))
    setOverrideGroupName(localStorage.getItem(`room-${roomId}-override-group-name`))
  }, [roomId])

  const groupExists = useCallback(
    (groupId: string) => groups.some(group => group.type === 'user' && group.group_id === groupId),
    [groups],
  )

  // Missing saved teams fall back to room membership when that is the room's
  // default dispatch; otherwise fall back to network broadcast.
  const missingGroupFallback = defaultTargetMode?.message_target_mode === 'room_default'
    ? BUILTIN_GROUP_ROOM_TEAM
    : BUILTIN_GROUP_ALL_AGENTS

  // Do not mistake an unloaded or unavailable catalog for a deleted team.
  // Only a successful catalog response can invalidate a source team or override.
  const validateGroup = useCallback((groupId: string | undefined) => {
    if (!groupId) return missingGroupFallback
    if (groupId === BUILTIN_GROUP_ROOM_TEAM) return BUILTIN_GROUP_ROOM_TEAM
    if (groupId === BUILTIN_GROUP_ALL_AGENTS) return BUILTIN_GROUP_ALL_AGENTS
    if (groupsLoadStatus !== 'success') return groupId
    return groupExists(groupId) ? groupId : missingGroupFallback
  }, [groupExists, groupsLoadStatus, missingGroupFallback])

  const validatedDefaultGroup = validateGroup(defaultGroup)
  const validatedOverrideGroup = overrideGroup === null
    ? null
    : validateGroup(overrideGroup)
  const isOverride = overrideGroup !== null
    && validatedOverrideGroup === overrideGroup
  const selectedGroup = isOverride ? overrideGroup : validatedDefaultGroup
  const selectedGroupRecord = groups.find(group => group.group_id === selectedGroup)

  // Builtin catalog names ("All Agents" / "Room Team") never override provenance.
  const catalogDisplayName = isBuiltinSelection(selectedGroup)
    ? undefined
    : selectedGroupRecord?.name

  const selectedGroupName = catalogDisplayName
    ?? (isOverride && !isBuiltinSelection(selectedGroup)
      ? overrideGroupName ?? 'Selected Team'
      : selectedGroup === BUILTIN_GROUP_ROOM_TEAM
        ? defaultGroupName ?? 'Room Team'
        : selectedGroup === validatedDefaultGroup || selectedGroup === defaultGroup
          ? defaultGroupName
          : undefined)

  const roomMembershipLabel =
    defaultTargetMode?.message_target_mode === 'room_default'
    && validatedDefaultGroup === BUILTIN_GROUP_ROOM_TEAM
      ? (defaultGroupName ?? 'Room Team')
      : undefined

  const clearOverrideState = useCallback(() => {
    setOverrideGroup(null)
    setOverrideGroupName(null)
    if (roomId) {
      localStorage.removeItem(`room-${roomId}-override-group`)
      localStorage.removeItem(`room-${roomId}-override-group-name`)
    }
  }, [roomId])

  // Modal state
  const [groupManagementOpen, setGroupManagementOpen] = useState(false)
  const [groupAction, setGroupAction] = useState<{
    type: 'create' | 'edit' | 'delete'
    group?: AgentGroup
  } | null>(null)

  // Agent state
  const [availableAgents, setAvailableAgents] = useState<Agent[]>([])
  const [loadingAgents, setLoadingAgents] = useState(false)
  const [agentsError, setAgentsError] = useState<string | null>(null)

  // Load user's groups
  useEffect(() => {
    const loadGroups = async () => {
      if (!userId) return
      setLoadingGroups(true)
      setGroupsLoadStatus('loading')
      try {
        const response = await listAgentGroups(userId, getToken)
        if (response.success && response.groups) {
          setGroups(response.groups)
          setGroupsLoadStatus('success')
        } else {
          setGroupsLoadStatus('error')
        }
      } catch (error) {
        console.error('Failed to load groups:', error)
        setGroupsLoadStatus('error')
      } finally {
        setLoadingGroups(false)
      }
    }

    if (isLoaded && userId) {
      loadGroups()
    }
  }, [isLoaded, userId, getToken])

  // Remove stale persisted overrides only after the catalog confirms deletion.
  useEffect(() => {
    if (
      groupsLoadStatus !== 'success'
      || overrideGroup === null
      || isBuiltinSelection(overrideGroup)
      || groupExists(overrideGroup)
    ) {
      return
    }
    clearOverrideState()
  }, [clearOverrideState, groupExists, groupsLoadStatus, overrideGroup])

  // Load available agents
  const loadAvailableAgents = useCallback(async () => {
    if (availableAgents.length > 0) return
    setLoadingAgents(true)
    setAgentsError(null)
    try {
      const response = await getAllAgents({ activeOnly: true, getToken })
      if (response.success && response.agents) {
        setAvailableAgents(response.agents)
      } else {
        setAgentsError(response.error || 'Failed to load agents')
      }
    } catch (error) {
      console.error('Failed to load agents:', error)
      setAgentsError(error instanceof Error ? error.message : 'Failed to load agents')
    } finally {
      setLoadingAgents(false)
    }
  }, [availableAgents.length, getToken])

  // Load agents on mount for mention suggestions
  useEffect(() => {
    if (isLoaded && userId && availableAgents.length === 0) {
      loadAvailableAgents()
    }
  }, [isLoaded, userId, loadAvailableAgents, availableAgents.length])

  // Refresh groups after changes in modal
  const handleGroupsChange = useCallback(async () => {
    if (!userId) return
    try {
      const response = await listAgentGroups(userId, getToken)
      if (response.success && response.groups) {
        setGroups(response.groups)
        setGroupsLoadStatus('success')
      }
    } catch (error) {
      console.error('Failed to refresh groups:', error)
    }
  }, [userId, getToken])

  // Group management entry points
  const handleCreateGroup = useCallback(() => {
    if (!userId) {
      onRequireAuth?.()
      return
    }
    loadAvailableAgents()
    setGroupAction({ type: 'create' })
    setGroupManagementOpen(true)
  }, [userId, onRequireAuth, loadAvailableAgents])

  const handleEditGroup = useCallback((group: AgentGroup) => {
    if (!userId) {
      onRequireAuth?.()
      return
    }
    loadAvailableAgents()
    setGroupAction({ type: 'edit', group })
    setGroupManagementOpen(true)
  }, [userId, onRequireAuth, loadAvailableAgents])

  const handleDeleteGroup = useCallback((group: AgentGroup) => {
    if (!userId) {
      onRequireAuth?.()
      return
    }
    loadAvailableAgents()
    setGroupAction({ type: 'delete', group })
    setGroupManagementOpen(true)
  }, [userId, onRequireAuth, loadAvailableAgents])

  const persistOverride = useCallback((groupId: string, groupName: string | null) => {
    setOverrideGroup(groupId)
    setOverrideGroupName(groupName)
    if (roomId) {
      localStorage.setItem(`room-${roomId}-override-group`, groupId)
      if (groupName) {
        localStorage.setItem(`room-${roomId}-override-group-name`, groupName)
      } else {
        localStorage.removeItem(`room-${roomId}-override-group-name`)
      }
    }
  }, [roomId])

  const handleGroupCreated = useCallback((group: AgentGroup) => {
    setGroups(prev => {
      const exists = prev.some(g => g.group_id === group.group_id)
      return exists
        ? prev.map(g => g.group_id === group.group_id ? group : g)
        : [...prev, group]
    })
    persistOverride(group.group_id, group.name)
  }, [persistOverride])

  // Handle group change (override). Selecting the current default clears the override.
  // all_agents always means network broadcast; room_team always means room membership.
  const handleGroupChange = useCallback((groupId: string, groupName?: string) => {
    const isConfirmedMissing = groupsLoadStatus === 'success'
      && !isBuiltinSelection(groupId)
      && !groupExists(groupId)
    if (groupId === validatedDefaultGroup || isConfirmedMissing) {
      clearOverrideState()
      return
    }

    if (groupId === BUILTIN_GROUP_ROOM_TEAM) {
      persistOverride(BUILTIN_GROUP_ROOM_TEAM, groupName ?? defaultGroupName ?? 'Room Team')
      return
    }

    if (groupId === BUILTIN_GROUP_ALL_AGENTS) {
      persistOverride(BUILTIN_GROUP_ALL_AGENTS, null)
      return
    }

    const resolvedName = groupName
      ?? groups.find(group => group.group_id === groupId)?.name
      ?? null
    persistOverride(groupId, resolvedName)
  }, [
    clearOverrideState,
    defaultGroupName,
    groupExists,
    groups,
    groupsLoadStatus,
    persistOverride,
    validatedDefaultGroup,
  ])

  // Handle clear override - revert to derived default
  const handleClearOverride = clearOverrideState

  const resolvedTargetMode: TargetModeDispatchInput = useMemo(() => {
    if (!isOverride) {
      return defaultTargetMode ?? resolveSelectedGroupDispatch(selectedGroup)
    }
    return resolveSelectedGroupDispatch(selectedGroup)
  }, [defaultTargetMode, isOverride, selectedGroup])

  return {
    // State
    groups,
    loadingGroups,
    selectedGroup,
    selectedGroupName,
    isOverride,
    resolvedTargetMode,
    roomMembershipLabel,
    groupManagementOpen,
    groupAction,
    availableAgents,
    loadingAgents,
    agentsError,
    // Actions
    handleGroupsChange,
    handleCreateGroup,
    handleEditGroup,
    handleDeleteGroup,
    handleGroupCreated,
    handleGroupChange,
    handleClearOverride,
    setGroupManagementOpen,
    setGroupAction,
    loadAvailableAgents,
    setAvailableAgents,
  }
}

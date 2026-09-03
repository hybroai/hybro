import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act, cleanup, waitFor } from '@testing-library/react'

const mockListAgentGroups = vi.fn()
const mockGetAllAgents = vi.fn()

vi.mock('@/lib/api/agent-group', () => ({
  listAgentGroups: (...args: unknown[]) => mockListAgentGroups(...args),
}))

vi.mock('@/lib/api/agent', () => ({
  getAllAgents: (...args: unknown[]) => mockGetAllAgents(...args),
}))

import { useGroupManagement } from '@/hooks/useGroupManagement'
import { BUILTIN_GROUP_ALL_AGENTS, BUILTIN_GROUP_ROOM_TEAM } from '@/lib/types/agent-group'

const mockGetToken = vi.fn().mockResolvedValue('test-token')

function defaultOptions(overrides: Record<string, unknown> = {}) {
  return {
    userId: 'user-1',
    getToken: mockGetToken,
    isLoaded: true,
    ...overrides,
  }
}

describe('useGroupManagement – default team behavior', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    localStorage.clear()
    mockListAgentGroups.mockResolvedValue({ success: true, groups: [] })
    mockGetAllAgents.mockResolvedValue({ success: true, agents: [] })
  })

  afterEach(() => {
    cleanup()
  })

  it('defaults to all_agents when no default team is provided', async () => {
    const { result } = renderHook(() =>
      useGroupManagement(defaultOptions())
    )

    await waitFor(() => {
      expect(result.current.loadingGroups).toBe(false)
    })

    expect(result.current.selectedGroup).toBe(BUILTIN_GROUP_ALL_AGENTS)
    expect(result.current.resolvedTargetMode).toEqual({ message_target_mode: 'all_agents' })
    expect(result.current.roomMembershipLabel).toBeUndefined()
  })

  it('selects room_team for manual room membership with room_default routing', async () => {
    const { result } = renderHook(() =>
      useGroupManagement(defaultOptions({
        defaultGroup: BUILTIN_GROUP_ROOM_TEAM,
        defaultGroupName: 'Story Agent',
        defaultTargetMode: { message_target_mode: 'room_default' },
      }))
    )

    await waitFor(() => {
      expect(result.current.loadingGroups).toBe(false)
    })

    expect(result.current.selectedGroup).toBe(BUILTIN_GROUP_ROOM_TEAM)
    expect(result.current.selectedGroupName).toBe('Story Agent')
    expect(result.current.roomMembershipLabel).toBe('Story Agent')
    expect(result.current.resolvedTargetMode).toEqual({ message_target_mode: 'room_default' })
  })

  it('keeps the manual room label even when the catalog includes builtin entries', async () => {
    mockListAgentGroups.mockResolvedValue({
      success: true,
      groups: [
        {
          group_id: BUILTIN_GROUP_ALL_AGENTS,
          name: 'All Agents',
          type: 'builtin',
          owner_id: null,
          agents: [],
          description: 'Search the entire agent network for the best match',
        },
        {
          group_id: BUILTIN_GROUP_ROOM_TEAM,
          name: 'Room Team',
          type: 'builtin',
          owner_id: null,
          agents: [],
        },
        {
          group_id: 'team-research',
          name: 'Research Team',
          type: 'user',
          owner_id: 'user-1',
          agents: ['agent-1'],
        },
      ],
    })

    const { result } = renderHook(() =>
      useGroupManagement(defaultOptions({
        defaultGroup: BUILTIN_GROUP_ROOM_TEAM,
        defaultGroupName: 'Weather Agent',
        defaultTargetMode: { message_target_mode: 'room_default' },
      }))
    )

    await waitFor(() => {
      expect(result.current.groups.length).toBeGreaterThan(0)
    })

    expect(result.current.selectedGroup).toBe(BUILTIN_GROUP_ROOM_TEAM)
    expect(result.current.selectedGroupName).toBe('Weather Agent')
    expect(result.current.resolvedTargetMode).toEqual({ message_target_mode: 'room_default' })
  })

  it('selecting All Agents overrides to true network broadcast', async () => {
    mockListAgentGroups.mockResolvedValue({
      success: true,
      groups: [
        {
          group_id: BUILTIN_GROUP_ALL_AGENTS,
          name: 'All Agents',
          type: 'builtin',
          owner_id: null,
          agents: [],
        },
        {
          group_id: 'team-research',
          name: 'Research Team',
          type: 'user',
          owner_id: 'user-1',
          agents: ['agent-1'],
        },
      ],
    })

    const { result } = renderHook(() =>
      useGroupManagement(defaultOptions({
        roomId: 'room-manual',
        defaultGroup: BUILTIN_GROUP_ROOM_TEAM,
        defaultGroupName: 'Weather Agent',
        defaultTargetMode: { message_target_mode: 'room_default' },
      }))
    )

    await waitFor(() => {
      expect(result.current.groups.length).toBeGreaterThan(0)
    })

    act(() => {
      result.current.handleGroupChange(BUILTIN_GROUP_ALL_AGENTS)
    })
    expect(result.current.selectedGroup).toBe(BUILTIN_GROUP_ALL_AGENTS)
    expect(result.current.selectedGroupName).toBeUndefined()
    expect(result.current.isOverride).toBe(true)
    expect(result.current.resolvedTargetMode).toEqual({ message_target_mode: 'all_agents' })
    expect(localStorage.getItem('room-room-manual-override-group')).toBe(BUILTIN_GROUP_ALL_AGENTS)

    act(() => {
      result.current.handleGroupChange(BUILTIN_GROUP_ROOM_TEAM)
    })
    expect(result.current.selectedGroup).toBe(BUILTIN_GROUP_ROOM_TEAM)
    expect(result.current.selectedGroupName).toBe('Weather Agent')
    expect(result.current.isOverride).toBe(false)
    expect(result.current.resolvedTargetMode).toEqual({ message_target_mode: 'room_default' })
    expect(localStorage.getItem('room-room-manual-override-group')).toBeNull()
  })

  it('reselecting room membership restores after a saved team override', async () => {
    mockListAgentGroups.mockResolvedValue({
      success: true,
      groups: [{
        group_id: 'team-research',
        name: 'Research Team',
        type: 'user',
        owner_id: 'user-1',
        agents: ['agent-1'],
      }],
    })

    const { result } = renderHook(() =>
      useGroupManagement(defaultOptions({
        roomId: 'room-manual',
        defaultGroup: BUILTIN_GROUP_ROOM_TEAM,
        defaultGroupName: 'Weather Agent',
        defaultTargetMode: { message_target_mode: 'room_default' },
      }))
    )

    await waitFor(() => {
      expect(result.current.groups.length).toBeGreaterThan(0)
    })
    act(() => {
      result.current.handleGroupChange('team-research')
    })
    expect(result.current.resolvedTargetMode).toEqual({
      message_target_mode: 'saved_group',
      target_group_id: 'team-research',
    })

    act(() => {
      result.current.handleGroupChange(BUILTIN_GROUP_ROOM_TEAM)
    })
    expect(result.current.selectedGroup).toBe(BUILTIN_GROUP_ROOM_TEAM)
    expect(result.current.selectedGroupName).toBe('Weather Agent')
    expect(result.current.isOverride).toBe(false)
    expect(result.current.resolvedTargetMode).toEqual({ message_target_mode: 'room_default' })
    expect(localStorage.getItem('room-room-manual-override-group')).toBeNull()
  })

  it('preserves source-team provenance and snapshot routing while groups load', () => {
    mockListAgentGroups.mockReturnValue(new Promise(() => {}))

    const { result } = renderHook(() =>
      useGroupManagement(defaultOptions({
        defaultGroup: 'team-research',
        defaultGroupName: 'Research Team',
        defaultTargetMode: { message_target_mode: 'room_default' },
      }))
    )

    expect(result.current.selectedGroup).toBe('team-research')
    expect(result.current.selectedGroupName).toBe('Research Team')
    expect(result.current.resolvedTargetMode).toEqual({ message_target_mode: 'room_default' })
  })

  it('preserves source-team provenance when the group catalog request fails', async () => {
    mockListAgentGroups.mockResolvedValue({ success: false, error: 'Unavailable' })

    const { result } = renderHook(() =>
      useGroupManagement(defaultOptions({
        defaultGroup: 'team-research',
        defaultGroupName: 'Research Team',
        defaultTargetMode: { message_target_mode: 'room_default' },
      }))
    )

    await waitFor(() => {
      expect(result.current.loadingGroups).toBe(false)
    })

    expect(result.current.selectedGroup).toBe('team-research')
    expect(result.current.selectedGroupName).toBe('Research Team')
    expect(result.current.resolvedTargetMode).toEqual({ message_target_mode: 'room_default' })
  })

  it('preserves source-team provenance when the group catalog request rejects', async () => {
    mockListAgentGroups.mockRejectedValue(new Error('Network error'))
    const consoleSpy = vi.spyOn(console, 'error').mockImplementation(() => {})

    const { result } = renderHook(() =>
      useGroupManagement(defaultOptions({
        defaultGroup: 'team-research',
        defaultGroupName: 'Research Team',
        defaultTargetMode: { message_target_mode: 'room_default' },
      }))
    )

    await waitFor(() => {
      expect(result.current.loadingGroups).toBe(false)
    })

    expect(result.current.selectedGroup).toBe('team-research')
    expect(result.current.selectedGroupName).toBe('Research Team')
    expect(result.current.resolvedTargetMode).toEqual({ message_target_mode: 'room_default' })
    consoleSpy.mockRestore()
  })

  it('does not label a persisted override as All Agents when the catalog fails', async () => {
    mockListAgentGroups.mockResolvedValue({ success: false, error: 'Unavailable' })

    const { result } = renderHook(() =>
      useGroupManagement(defaultOptions({ roomId: 'room-override' }))
    )

    await waitFor(() => {
      expect(result.current.loadingGroups).toBe(false)
    })

    act(() => {
      result.current.handleGroupChange('team-research', 'Research Team')
    })

    expect(result.current.selectedGroup).toBe('team-research')
    expect(result.current.selectedGroupName).toBe('Research Team')
    expect(result.current.resolvedTargetMode).toEqual({
      message_target_mode: 'saved_group',
      target_group_id: 'team-research',
    })
    expect(localStorage.getItem('room-room-override-override-group-name')).toBe('Research Team')
  })

  it('uses a neutral Team label for legacy overrides without a persisted name', async () => {
    mockListAgentGroups.mockResolvedValue({ success: false, error: 'Unavailable' })

    const { result } = renderHook(() =>
      useGroupManagement(defaultOptions({ roomId: 'room-legacy' }))
    )

    await waitFor(() => {
      expect(result.current.loadingGroups).toBe(false)
    })

    act(() => {
      result.current.handleGroupChange('team-legacy')
    })

    expect(result.current.selectedGroupName).toBe('Selected Team')
    expect(result.current.resolvedTargetMode).toEqual({
      message_target_mode: 'saved_group',
      target_group_id: 'team-legacy',
    })
  })

  it('uses the room source team when that team still exists', async () => {
    mockListAgentGroups.mockResolvedValue({
      success: true,
      groups: [{
        group_id: 'team-research',
        name: 'Research Team',
        type: 'user',
        owner_id: 'user-1',
        agents: ['agent-1'],
      }],
    })

    const { result } = renderHook(() =>
      useGroupManagement(defaultOptions({
        defaultGroup: 'team-research',
        defaultGroupName: 'Research Team',
        defaultTargetMode: { message_target_mode: 'room_default' },
      }))
    )

    await waitFor(() => {
      expect(result.current.groups).toHaveLength(1)
    })

    expect(result.current.selectedGroup).toBe('team-research')
    expect(result.current.selectedGroupName).toBe('Research Team')
    expect(result.current.resolvedTargetMode).toEqual({
      message_target_mode: 'room_default',
    })
    expect(result.current.isOverride).toBe(false)
  })

  it('falls back to room_team when the room source team was deleted', async () => {
    const { result } = renderHook(() =>
      useGroupManagement(defaultOptions({
        defaultGroup: 'deleted-team',
        defaultGroupName: 'Deleted Team',
        defaultTargetMode: { message_target_mode: 'room_default' },
      }))
    )

    await waitFor(() => {
      expect(result.current.loadingGroups).toBe(false)
    })

    expect(result.current.selectedGroup).toBe(BUILTIN_GROUP_ROOM_TEAM)
    expect(result.current.selectedGroupName).toBe('Deleted Team')
    expect(result.current.resolvedTargetMode).toEqual({ message_target_mode: 'room_default' })
  })

  it('preserves room_team as the room_default selection id', async () => {
    const { result } = renderHook(() =>
      useGroupManagement(defaultOptions({
        defaultGroup: BUILTIN_GROUP_ROOM_TEAM,
        defaultGroupName: 'Room Agents',
        defaultTargetMode: { message_target_mode: 'room_default' },
      }))
    )

    await waitFor(() => {
      expect(result.current.loadingGroups).toBe(false)
    })

    expect(result.current.selectedGroup).toBe(BUILTIN_GROUP_ROOM_TEAM)
    expect(result.current.selectedGroupName).toBe('Room Agents')
    expect(result.current.resolvedTargetMode).toEqual({ message_target_mode: 'room_default' })
  })

  it('explicit override works when the selected team exists', async () => {
    mockListAgentGroups.mockResolvedValue({
      success: true,
      groups: [{
        group_id: 'grp-custom',
        name: 'Custom Team',
        type: 'user',
        owner_id: 'user-1',
        agents: ['agent-1'],
      }],
    })
    const { result } = renderHook(() =>
      useGroupManagement(defaultOptions())
    )

    await waitFor(() => {
      expect(result.current.groups).toHaveLength(1)
    })

    act(() => {
      result.current.handleGroupChange('grp-custom')
    })

    expect(result.current.selectedGroup).toBe('grp-custom')
    expect(result.current.isOverride).toBe(true)
  })

  it('explicit saved_group override works and persists for a room', async () => {
    mockListAgentGroups.mockResolvedValue({
      success: true,
      groups: [{
        group_id: 'grp-my-saved',
        name: 'Saved Team',
        type: 'user',
        owner_id: 'user-1',
        agents: ['agent-1'],
      }],
    })
    const { result } = renderHook(() =>
      useGroupManagement(defaultOptions({ roomId: 'room-empty' }))
    )

    await waitFor(() => {
      expect(result.current.groups).toHaveLength(1)
    })

    act(() => {
      result.current.handleGroupChange('grp-my-saved')
    })

    expect(result.current.selectedGroup).toBe('grp-my-saved')
    expect(result.current.resolvedTargetMode).toEqual({
      message_target_mode: 'saved_group',
      target_group_id: 'grp-my-saved',
    })
    expect(localStorage.getItem('room-room-empty-override-group')).toBe('grp-my-saved')
  })

  it('clears a stale override after successful catalog validation', async () => {
    let resolveGroups!: (value: unknown) => void
    mockListAgentGroups.mockReturnValue(new Promise(resolve => { resolveGroups = resolve }))

    const { result } = renderHook(() =>
      useGroupManagement(defaultOptions({
        roomId: 'room-snapshot',
        defaultGroup: 'team-source',
        defaultGroupName: 'Source Team',
        defaultTargetMode: { message_target_mode: 'room_default' },
      }))
    )

    act(() => {
      result.current.handleGroupChange('deleted-team', 'Deleted Team')
    })
    expect(result.current.isOverride).toBe(true)
    expect(result.current.resolvedTargetMode).toEqual({
      message_target_mode: 'saved_group',
      target_group_id: 'deleted-team',
    })

    await act(async () => {
      resolveGroups({
        success: true,
        groups: [{
          group_id: 'team-source',
          name: 'Source Team',
          type: 'user',
          owner_id: 'user-1',
          agents: ['agent-1'],
        }],
      })
    })

    await waitFor(() => {
      expect(result.current.isOverride).toBe(false)
    })
    expect(result.current.selectedGroup).toBe('team-source')
    expect(result.current.resolvedTargetMode).toEqual({ message_target_mode: 'room_default' })
    expect(localStorage.getItem('room-room-snapshot-override-group')).toBeNull()
    expect(localStorage.getItem('room-room-snapshot-override-group-name')).toBeNull()
  })

  it('reselecting the source team restores room-default routing', async () => {
    mockListAgentGroups.mockResolvedValue({
      success: true,
      groups: [{
        group_id: 'team-research',
        name: 'Research Team',
        type: 'user',
        owner_id: 'user-1',
        agents: ['agent-1'],
      }],
    })

    const { result } = renderHook(() =>
      useGroupManagement(defaultOptions({
        roomId: 'room-team',
        defaultGroup: 'team-research',
        defaultTargetMode: { message_target_mode: 'room_default' },
      }))
    )

    await waitFor(() => {
      expect(result.current.selectedGroup).toBe('team-research')
    })

    act(() => {
      result.current.handleGroupChange(BUILTIN_GROUP_ALL_AGENTS)
    })
    expect(result.current.selectedGroup).toBe(BUILTIN_GROUP_ALL_AGENTS)

    act(() => {
      result.current.handleGroupChange('team-research')
    })
    expect(result.current.selectedGroup).toBe('team-research')
    expect(result.current.resolvedTargetMode).toEqual({ message_target_mode: 'room_default' })
    expect(result.current.isOverride).toBe(false)
    expect(localStorage.getItem('room-room-team-override-group')).toBeNull()
  })
})

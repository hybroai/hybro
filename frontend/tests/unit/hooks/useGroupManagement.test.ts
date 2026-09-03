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
import { BUILTIN_GROUP_ALL_AGENTS } from '@/lib/types/agent-group'
import type { AgentGroup } from '@/lib/types/agent-group'

const mockGetToken = vi.fn().mockResolvedValue('test-token')

function defaultOptions() {
  return {
    userId: 'user-1',
    getToken: mockGetToken,
    isLoaded: true,
  }
}

const fakeGroup: AgentGroup = {
  group_id: 'grp-1',
  name: 'My Group',
  type: 'user' as const,
  owner_id: 'user-1',
  agents: ['agent-a', 'agent-b'],
}

const fakeGroup2: AgentGroup = {
  group_id: 'grp-2',
  name: 'Second Group',
  type: 'user' as const,
  owner_id: 'user-1',
  agents: ['agent-c'],
}

describe('useGroupManagement', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    localStorage.clear()
    mockListAgentGroups.mockResolvedValue({ success: true, groups: [] })
    mockGetAllAgents.mockResolvedValue({ success: true, agents: [] })
  })

  afterEach(() => {
    cleanup()
  })

  it('should return empty groups initially before fetch resolves', () => {
    mockListAgentGroups.mockReturnValue(new Promise(() => {}))
    mockGetAllAgents.mockReturnValue(new Promise(() => {}))

    const { result } = renderHook(() => useGroupManagement(defaultOptions()))

    expect(result.current.groups).toEqual([])
    expect(result.current.selectedGroup).toBe(BUILTIN_GROUP_ALL_AGENTS)
    expect(result.current.isOverride).toBe(false)
  })

  it('should fetch groups on mount when userId and isLoaded are set', async () => {
    const groups = [fakeGroup, fakeGroup2]
    mockListAgentGroups.mockResolvedValue({ success: true, groups })

    const { result } = renderHook(() => useGroupManagement(defaultOptions()))

    await waitFor(() => {
      expect(result.current.groups).toEqual(groups)
    })
    expect(mockListAgentGroups).toHaveBeenCalledWith('user-1', mockGetToken)
  })

  it('should open create-group modal via handleCreateGroup', async () => {
    const { result } = renderHook(() => useGroupManagement(defaultOptions()))

    await waitFor(() => {
      expect(result.current.loadingGroups).toBe(false)
    })

    act(() => {
      result.current.handleCreateGroup()
    })

    expect(result.current.groupManagementOpen).toBe(true)
    expect(result.current.groupAction).toEqual({ type: 'create' })
  })

  it('should handle create group error gracefully (listAgentGroups fails on refresh)', async () => {
    mockListAgentGroups.mockResolvedValueOnce({ success: true, groups: [] })
    mockListAgentGroups.mockRejectedValueOnce(new Error('Network error'))

    const { result } = renderHook(() => useGroupManagement(defaultOptions()))

    await waitFor(() => {
      expect(result.current.loadingGroups).toBe(false)
    })

    const consoleSpy = vi.spyOn(console, 'error').mockImplementation(() => {})

    await act(async () => {
      await result.current.handleGroupsChange()
    })

    expect(consoleSpy).toHaveBeenCalledWith('Failed to refresh groups:', expect.any(Error))
    consoleSpy.mockRestore()
  })

  it('should handle delete group by opening modal with delete action', async () => {
    mockListAgentGroups.mockResolvedValue({ success: true, groups: [fakeGroup] })

    const { result } = renderHook(() => useGroupManagement(defaultOptions()))

    await waitFor(() => {
      expect(result.current.groups).toHaveLength(1)
    })

    act(() => {
      result.current.handleDeleteGroup(fakeGroup)
    })

    expect(result.current.groupManagementOpen).toBe(true)
    expect(result.current.groupAction).toEqual({ type: 'delete', group: fakeGroup })
  })

  it('should update groups list when handleGroupCreated is called with new group', async () => {
    mockListAgentGroups.mockResolvedValue({ success: true, groups: [fakeGroup] })

    const { result } = renderHook(() => useGroupManagement(defaultOptions()))

    await waitFor(() => {
      expect(result.current.groups).toHaveLength(1)
    })

    act(() => {
      result.current.handleGroupCreated(fakeGroup2)
    })

    expect(result.current.groups).toHaveLength(2)
    expect(result.current.groups[1]).toEqual(fakeGroup2)
    expect(result.current.selectedGroup).toBe('grp-2')
    expect(result.current.isOverride).toBe(true)
  })

  it('should restore a persisted room override after mount', async () => {
    localStorage.setItem('room-room-42-override-group', 'grp-1')
    localStorage.setItem('room-room-42-override-group-name', 'My Group')
    mockListAgentGroups.mockResolvedValue({ success: true, groups: [fakeGroup] })

    const { result } = renderHook(() => useGroupManagement({
      ...defaultOptions(),
      roomId: 'room-42',
    }))

    await waitFor(() => {
      expect(result.current.selectedGroup).toBe('grp-1')
      expect(result.current.groups).toEqual([fakeGroup])
    })
    expect(result.current.selectedGroupName).toBe('My Group')
    expect(result.current.isOverride).toBe(true)
    expect(result.current.resolvedTargetMode).toEqual({
      message_target_mode: 'saved_group',
      target_group_id: 'grp-1',
    })
  })

  it('should apply group override via handleGroupChange', async () => {
    mockListAgentGroups.mockResolvedValue({ success: true, groups: [fakeGroup] })
    const opts = { ...defaultOptions(), roomId: 'room-42' }
    const { result } = renderHook(() => useGroupManagement(opts))

    await waitFor(() => {
      expect(result.current.groups).toEqual([fakeGroup])
    })

    act(() => {
      result.current.handleGroupChange('grp-1')
    })

    expect(result.current.selectedGroup).toBe('grp-1')
    expect(result.current.isOverride).toBe(true)
    expect(localStorage.getItem('room-room-42-override-group')).toBe('grp-1')

    act(() => {
      result.current.handleClearOverride()
    })

    expect(result.current.isOverride).toBe(false)
    expect(localStorage.getItem('room-room-42-override-group')).toBeNull()
  })

  it('should manage loading states correctly during fetch', async () => {
    let resolveGroups!: (v: unknown) => void
    let resolveAgents!: (v: unknown) => void

    mockListAgentGroups.mockReturnValue(
      new Promise(r => { resolveGroups = r })
    )
    mockGetAllAgents.mockReturnValue(
      new Promise(r => { resolveAgents = r })
    )

    const { result } = renderHook(() => useGroupManagement(defaultOptions()))

    expect(result.current.loadingGroups).toBe(true)
    expect(result.current.loadingAgents).toBe(true)

    await act(async () => {
      resolveGroups({ success: true, groups: [fakeGroup] })
    })

    await waitFor(() => {
      expect(result.current.loadingGroups).toBe(false)
    })
    expect(result.current.groups).toEqual([fakeGroup])

    await act(async () => {
      resolveAgents({ success: true, agents: [{ agent_id: 'a1', agent_card: { name: 'A1' } }] })
    })

    await waitFor(() => {
      expect(result.current.loadingAgents).toBe(false)
    })
  })
})

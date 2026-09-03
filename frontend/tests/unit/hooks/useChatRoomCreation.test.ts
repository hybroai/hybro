import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act, cleanup } from '@testing-library/react'

const mockPush = vi.fn()
vi.mock('next/navigation', () => ({
  useRouter: () => ({ push: mockPush }),
}))

vi.mock('@/lib/api/room', () => ({
  createNewRoom: vi.fn(),
  suggestAgents: vi.fn(),
}))

import type { SuggestAgentsResponse } from '@/lib/api/room'

vi.mock('@/lib/api/agent', () => ({
  getAllAgents: vi.fn(),
}))

vi.mock('@/components/ui/banner', () => ({
  banner: { info: vi.fn(), error: vi.fn(), success: vi.fn(), warning: vi.fn() },
}))

import { createNewRoom, suggestAgents } from '@/lib/api/room'
import { getAllAgents } from '@/lib/api/agent'
import { banner } from '@/components/ui/banner'
import { useChatRoomCreation } from '@/hooks/useChatRoomCreation'
import { useRoomUiStore } from '@/stores/room-ui-store'
import type { Agent } from '@/lib/types/agent'

const mockCreateNewRoom = createNewRoom as ReturnType<typeof vi.fn>
const mockGetAllAgents = getAllAgents as ReturnType<typeof vi.fn>
const mockSuggestAgents = suggestAgents as ReturnType<typeof vi.fn>

const mockAgent: Agent = {
  agent_id: 'agent-1',
  agent_card: { name: 'Test Agent' } as Agent['agent_card'],
  agent_status: 'active' as Agent['agent_status'],
}

const defaultProps = {
  userId: 'user-1',
  userName: 'Test User',
  getToken: vi.fn().mockResolvedValue('mock-token'),
}

describe('useChatRoomCreation', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mockPush.mockClear()
    useRoomUiStore.getState().resetAll()
  })

  afterEach(() => {
    cleanup()
  })

  describe('initial state', () => {
    it('should return default state', () => {
      const { result } = renderHook(() => useChatRoomCreation(defaultProps))

      expect(result.current.creating).toBe(false)
      expect(result.current.loadingAgents).toBe(false)
      expect(result.current.suggestingAgents).toBe(false)
      expect(result.current.defaultAgents).toEqual([])
    })
  })

  describe('loadDefaultAgents', () => {
    it('should load and store active agents', async () => {
      mockGetAllAgents.mockResolvedValue({
        success: true,
        agents: [mockAgent],
      })

      const { result } = renderHook(() => useChatRoomCreation(defaultProps))

      let agents: Agent[]
      await act(async () => {
        agents = await result.current.loadDefaultAgents()
      })

      expect(agents!).toEqual([mockAgent])
      expect(result.current.defaultAgents).toEqual([mockAgent])
    })

    it('should show error and return empty array on failure', async () => {
      mockGetAllAgents.mockRejectedValue(new Error('Network'))

      const { result } = renderHook(() => useChatRoomCreation(defaultProps))

      let agents: Agent[]
      await act(async () => {
        agents = await result.current.loadDefaultAgents()
      })

      expect(agents!).toEqual([])
      expect(banner.error).toHaveBeenCalledWith('Failed to load agents')
    })
  })

  describe('getAgentSuggestions', () => {
    it('should call suggestAgents and return response', async () => {
      const mockResponse = {
        success: true,
        suggested_agents: [{ agent_id: 'a-1', name: 'Agent', reason: 'fits' }],
      }
      mockSuggestAgents.mockResolvedValue(mockResponse)

      const { result } = renderHook(() => useChatRoomCreation(defaultProps))

      let response: SuggestAgentsResponse | null
      await act(async () => {
        response = await result.current.getAgentSuggestions('Help me code')
      })

      expect(mockSuggestAgents).toHaveBeenCalledWith('Help me code', 3, defaultProps.getToken)
      expect(response!).toEqual(mockResponse)
    })

    it('should return null on error', async () => {
      mockSuggestAgents.mockRejectedValue(new Error('Fail'))

      const { result } = renderHook(() => useChatRoomCreation(defaultProps))

      let response: unknown
      await act(async () => {
        response = await result.current.getAgentSuggestions('test')
      })

      expect(response).toBeNull()
    })
  })

  describe('createRoomWithMessage', () => {
    it('should fail when userId is missing', async () => {
      const { result } = renderHook(() =>
        useChatRoomCreation({ ...defaultProps, userId: undefined })
      )

      let roomId: string | null
      await act(async () => {
        roomId = await result.current.createRoomWithMessage('Hello')
      })

      expect(roomId!).toBeNull()
      expect(banner.error).toHaveBeenCalledWith('User information not available')
    })

    it('should fail when message is empty', async () => {
      const { result } = renderHook(() => useChatRoomCreation(defaultProps))

      let roomId: string | null
      await act(async () => {
        roomId = await result.current.createRoomWithMessage('   ')
      })

      expect(roomId!).toBeNull()
      expect(banner.error).toHaveBeenCalledWith('Message cannot be empty')
    })

    it('should create room with correct payload', async () => {
      mockCreateNewRoom.mockResolvedValue({
        success: true,
        room: { room_id: 'room-123' },
      })

      const { result } = renderHook(() => useChatRoomCreation(defaultProps))

      let roomId: string | null
      await act(async () => {
        roomId = await result.current.createRoomWithMessage('Hello world', {
          selectedAgents: [mockAgent],
          roomName: 'My Room',
        })
      })

      expect(roomId!).toBe('room-123')
      expect(mockCreateNewRoom).toHaveBeenCalledWith(
        'My Room',
        'user-1',
        'Test User',
        defaultProps.getToken,
        { 'agent-1': 'Test Agent' },
        { use_supervisor: true, initialMessage: 'Hello world' },
        undefined,
        {
          membership_seed_input: 'manual',
          room_agent_ids: ['agent-1'],
        },
      )
      const pending = useRoomUiStore.getState().pendingRoomData['room-123']
      expect(pending?.agentScope).toEqual({ source: 'room_default' })
    })

    it('should auto-generate room name from message when not provided', async () => {
      mockCreateNewRoom.mockResolvedValue({
        success: true,
        room: { room_id: 'room-456' },
      })

      const { result } = renderHook(() => useChatRoomCreation(defaultProps))

      await act(async () => {
        await result.current.createRoomWithMessage('Short msg')
      })

      expect(mockCreateNewRoom.mock.calls[0][0]).toBe('Short msg')
    })

    it('should truncate long auto-generated room names', async () => {
      mockCreateNewRoom.mockResolvedValue({
        success: true,
        room: { room_id: 'room-789' },
      })

      const { result } = renderHook(() => useChatRoomCreation(defaultProps))

      const longMessage = 'A'.repeat(50)
      await act(async () => {
        await result.current.createRoomWithMessage(longMessage)
      })

      const roomName = mockCreateNewRoom.mock.calls[0][0]
      expect(roomName).toBe('A'.repeat(30) + '...')
    })

    it('should store pending room data in zustand', async () => {
      mockCreateNewRoom.mockResolvedValue({
        success: true,
        room: { room_id: 'room-store' },
      })

      const { result } = renderHook(() => useChatRoomCreation(defaultProps))

      await act(async () => {
        await result.current.createRoomWithMessage('Hello', { targetGroup: 'g-1' })
      })

      const pending = useRoomUiStore.getState().pendingRoomData['room-store']
      expect(pending).toEqual({
        initialMessage: 'Hello',
        mode: 'supervisor',
        agentScope: { source: 'saved_group', group_id: 'g-1' },
        clientRequestId: expect.any(String),
        attachments: undefined,
      })
    })

    it('should show error on API failure', async () => {
      mockCreateNewRoom.mockRejectedValue(new Error('Server down'))

      const { result } = renderHook(() => useChatRoomCreation(defaultProps))

      let roomId: string | null
      await act(async () => {
        roomId = await result.current.createRoomWithMessage('Hello')
      })

      expect(roomId!).toBeNull()
      expect(banner.error).toHaveBeenCalledWith('Server down')
    })

    it('should derive membership_seed_input: saved_group when targetGroup is a saved group', async () => {
      mockCreateNewRoom.mockResolvedValue({
        success: true,
        room: { room_id: 'room-seeded' },
      })

      const { result } = renderHook(() => useChatRoomCreation(defaultProps))

      await act(async () => {
        await result.current.createRoomWithMessage('Hello', { targetGroup: 'group-abc' })
      })

      const membershipArg = mockCreateNewRoom.mock.calls[0][7]
      expect(membershipArg).toEqual({
        membership_seed_input: 'saved_group',
        seed_group_id: 'group-abc',
      })
    })

    it('should NOT derive membership from builtin groups (all_agents)', async () => {
      mockCreateNewRoom.mockResolvedValue({
        success: true,
        room: { room_id: 'room-all' },
      })

      const { result } = renderHook(() => useChatRoomCreation(defaultProps))

      await act(async () => {
        await result.current.createRoomWithMessage('Hello', { targetGroup: 'all_agents' })
      })

      const membershipArg = mockCreateNewRoom.mock.calls[0][7]
      expect(membershipArg).toBeUndefined()
    })

    it('should carry targetGroup in handoff for builtin groups', async () => {
      mockCreateNewRoom.mockResolvedValue({
        success: true,
        room: { room_id: 'room-carry' },
      })

      const { result } = renderHook(() => useChatRoomCreation(defaultProps))

      await act(async () => {
        await result.current.createRoomWithMessage('Hello', { targetGroup: 'all_agents' })
      })

      const pending = useRoomUiStore.getState().pendingRoomData['room-carry']
      expect(pending?.agentScope).toEqual({ source: 'all_agents' })
      expect(pending?.mode).toBe('supervisor')
      expect(pending?.clientRequestId).toEqual(expect.any(String))
    })

    it('should prefer selectedAgents over saved group seed', async () => {
      mockCreateNewRoom.mockResolvedValue({
        success: true,
        room: { room_id: 'room-manual' },
      })

      const { result } = renderHook(() => useChatRoomCreation(defaultProps))

      await act(async () => {
        await result.current.createRoomWithMessage('Hello', {
          selectedAgents: [mockAgent],
          targetGroup: 'group-abc',
        })
      })

      const membershipArg = mockCreateNewRoom.mock.calls[0][7]
      expect(membershipArg).toEqual({
        membership_seed_input: 'manual',
        room_agent_ids: ['agent-1'],
      })
      const roomAgentSet = mockCreateNewRoom.mock.calls[0][4]
      expect(Object.keys(roomAgentSet)).toContain('agent-1')
      const pending = useRoomUiStore.getState().pendingRoomData['room-manual']
      expect(pending?.agentScope).toEqual({ source: 'room_default' })
      expect(pending?.mode).toBe('supervisor')
    })

    it('should preserve explicit mention dispatch when selectedAgents seed the room', async () => {
      mockCreateNewRoom.mockResolvedValue({
        success: true,
        room: { room_id: 'room-manual-mention' },
      })

      const { result } = renderHook(() => useChatRoomCreation(defaultProps))

      await act(async () => {
        await result.current.createRoomWithMessage('Hello <@agent-mentioned|Mentioned>', {
          selectedAgents: [mockAgent],
          dispatch: { mentioned_agent_ids: ['agent-mentioned'] },
          targetGroup: 'group-abc',
        })
      })

      const membershipArg = mockCreateNewRoom.mock.calls[0][7]
      expect(membershipArg).toEqual({
        membership_seed_input: 'manual',
        room_agent_ids: ['agent-1'],
      })
      const roomAgentSet = mockCreateNewRoom.mock.calls[0][4]
      expect(Object.keys(roomAgentSet)).toContain('agent-1')
      const pending = useRoomUiStore.getState().pendingRoomData['room-manual-mention']
      expect(pending).toEqual({
        initialMessage: 'Hello <@agent-mentioned|Mentioned>',
        mode: 'supervisor',
        agentScope: { source: 'mention', agent_ids: ['agent-mentioned'] },
        clientRequestId: expect.any(String),
        attachments: undefined,
      })
    })

    it('should not seed or persist targetGroup when explicit dispatch uses mentions', async () => {
      mockCreateNewRoom.mockResolvedValue({
        success: true,
        room: { room_id: 'room-mention' },
      })

      const { result } = renderHook(() => useChatRoomCreation(defaultProps))

      await act(async () => {
        await result.current.createRoomWithMessage('Hello <@agent-mentioned|Mentioned>', {
          dispatch: { mentioned_agent_ids: ['agent-mentioned'] },
          targetGroup: 'group-abc',
        })
      })

      const membershipArg = mockCreateNewRoom.mock.calls[0][7]
      expect(membershipArg).toBeUndefined()
      const pending = useRoomUiStore.getState().pendingRoomData['room-mention']
      expect(pending).toEqual({
        initialMessage: 'Hello <@agent-mentioned|Mentioned>',
        mode: 'supervisor',
        agentScope: { source: 'mention', agent_ids: ['agent-mentioned'] },
        clientRequestId: expect.any(String),
        attachments: undefined,
      })
    })

    it('should not seed targetGroup when explicit non-mention dispatch is provided', async () => {
      mockCreateNewRoom.mockResolvedValue({
        success: true,
        room: { room_id: 'room-explicit-all' },
      })

      const { result } = renderHook(() => useChatRoomCreation(defaultProps))

      await act(async () => {
        await result.current.createRoomWithMessage('Hello everyone', {
          dispatch: { message_target_mode: 'all_agents' },
          targetGroup: 'group-abc',
        })
      })

      const membershipArg = mockCreateNewRoom.mock.calls[0][7]
      expect(membershipArg).toBeUndefined()
      const pending = useRoomUiStore.getState().pendingRoomData['room-explicit-all']
      expect(pending).toEqual({
        initialMessage: 'Hello everyone',
        mode: 'supervisor',
        agentScope: { source: 'all_agents' },
        clientRequestId: expect.any(String),
        attachments: undefined,
      })
    })

    it('should return null when API returns success=false (line 142)', async () => {
      mockCreateNewRoom.mockResolvedValue({
        success: false,
        error: 'Rate limited',
      })

      const { result } = renderHook(() => useChatRoomCreation(defaultProps))

      let roomId: string | null
      await act(async () => {
        roomId = await result.current.createRoomWithMessage('Hello')
      })

      expect(roomId!).toBeNull()
      expect(banner.error).toHaveBeenCalledWith('Rate limited')
    })

    it('should return null when room_id is missing (line 130)', async () => {
      mockCreateNewRoom.mockResolvedValue({
        success: true,
        room: { room_id: '' },
      })

      const { result } = renderHook(() => useChatRoomCreation(defaultProps))

      let roomId: string | null
      await act(async () => {
        roomId = await result.current.createRoomWithMessage('Hello')
      })

      expect(roomId!).toBeNull()
      expect(banner.error).toHaveBeenCalledWith('Room created but no room_id returned')
    })

    it('should return null when room object is null (line 142)', async () => {
      mockCreateNewRoom.mockResolvedValue({
        success: true,
        room: null,
      })

      const { result } = renderHook(() => useChatRoomCreation(defaultProps))

      let roomId: string | null
      await act(async () => {
        roomId = await result.current.createRoomWithMessage('Hello')
      })

      expect(roomId!).toBeNull()
      expect(banner.error).toHaveBeenCalledWith('Failed to create room')
    })

    it('should display generic error for non-Error exceptions', async () => {
      mockCreateNewRoom.mockRejectedValue('string error')

      const { result } = renderHook(() => useChatRoomCreation(defaultProps))

      await act(async () => {
        await result.current.createRoomWithMessage('Hello')
      })

      expect(banner.error).toHaveBeenCalledWith('Failed to create room')
    })
  })

  describe('createAndNavigate', () => {
    it('should create room then navigate', async () => {
      mockCreateNewRoom.mockResolvedValue({
        success: true,
        room: { room_id: 'room-nav' },
      })

      const dispatchSpy = vi.spyOn(window, 'dispatchEvent')
      const { result } = renderHook(() => useChatRoomCreation(defaultProps))

      let success: boolean
      await act(async () => {
        success = await result.current.createAndNavigate('Go!')
      })

      expect(success!).toBe(true)
      expect(mockPush).toHaveBeenCalledWith('/room/room-nav')
      expect(dispatchSpy).not.toHaveBeenCalled()
      dispatchSpy.mockRestore()
    })

    it('should return false on room creation failure', async () => {
      mockCreateNewRoom.mockRejectedValue(new Error('fail'))

      const { result } = renderHook(() => useChatRoomCreation(defaultProps))

      let success: boolean
      await act(async () => {
        success = await result.current.createAndNavigate('Go!')
      })

      expect(success!).toBe(false)
      expect(mockPush).not.toHaveBeenCalled()
    })
  })

  describe('createWithAgentsAndNavigate', () => {
    it('should fail when no agents selected', async () => {
      const { result } = renderHook(() => useChatRoomCreation(defaultProps))

      let success: boolean
      await act(async () => {
        success = await result.current.createWithAgentsAndNavigate('Hi', [])
      })

      expect(success!).toBe(false)
      expect(banner.error).toHaveBeenCalledWith('Please select at least one agent')
    })

    it('should create and navigate with agents', async () => {
      mockCreateNewRoom.mockResolvedValue({
        success: true,
        room: { room_id: 'room-agent' },
      })

      const { result } = renderHook(() => useChatRoomCreation(defaultProps))

      let success: boolean
      await act(async () => {
        success = await result.current.createWithAgentsAndNavigate('Hi', [mockAgent])
      })

      expect(success!).toBe(true)
      expect(mockPush).toHaveBeenCalledWith('/room/room-agent')
    })
  })
})

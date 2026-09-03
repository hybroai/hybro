/**
 * Tests for useChatRoomCreation — Supervisor Toggle in room creation.
 *
 * Verifies that useSupervisor is passed through CreateRoomOptions into
 * the extendInfo object sent to createNewRoom.
 */
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

vi.mock('@/lib/api/agent', () => ({
  getAllAgents: vi.fn(),
}))

vi.mock('@/components/ui/banner', () => ({
  banner: { info: vi.fn(), error: vi.fn(), success: vi.fn(), warning: vi.fn() },
}))

import { createNewRoom } from '@/lib/api/room'
import { useChatRoomCreation } from '@/hooks/useChatRoomCreation'
import { useRoomUiStore } from '@/stores/room-ui-store'
import type { Agent } from '@/lib/types/agent'

const mockCreateNewRoom = createNewRoom as ReturnType<typeof vi.fn>

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

describe('useChatRoomCreation — Supervisor Toggle', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mockPush.mockClear()
    useRoomUiStore.getState().resetAll()
  })

  afterEach(() => {
    cleanup()
  })

  it('should include use_supervisor: true in extendInfo when useSupervisor is true', async () => {
    mockCreateNewRoom.mockResolvedValue({
      success: true,
      room: { room_id: 'room-sv-1' },
    })

    const { result } = renderHook(() => useChatRoomCreation(defaultProps))

    await act(async () => {
      await result.current.createRoomWithMessage('Hello', {
        selectedAgents: [mockAgent],
        useSupervisor: true,
        roomName: 'Supervisor Room',
      })
    })

    expect(mockCreateNewRoom).toHaveBeenCalledWith(
      'Supervisor Room',
      'user-1',
      'Test User',
      defaultProps.getToken,
      { 'agent-1': 'Test Agent' },
      expect.objectContaining({
        use_supervisor: true,
        initialMessage: 'Hello',
      }),
      undefined,
      {
        membership_seed_input: 'manual',
        room_agent_ids: ['agent-1'],
      },
    )
  })

  it('should include use_supervisor: false in extendInfo when useSupervisor is false', async () => {
    mockCreateNewRoom.mockResolvedValue({
      success: true,
      room: { room_id: 'room-sv-2' },
    })

    const { result } = renderHook(() => useChatRoomCreation(defaultProps))

    await act(async () => {
      await result.current.createRoomWithMessage('Hello', {
        useSupervisor: false,
      })
    })

    const extendInfoArg = mockCreateNewRoom.mock.calls[0][5]
    expect(extendInfoArg).toHaveProperty('use_supervisor', false)
  })

  it('should default use_supervisor to true when useSupervisor is not provided', async () => {
    mockCreateNewRoom.mockResolvedValue({
      success: true,
      room: { room_id: 'room-sv-3' },
    })

    const { result } = renderHook(() => useChatRoomCreation(defaultProps))

    await act(async () => {
      await result.current.createRoomWithMessage('Hello', {
      })
    })

    const extendInfoArg = mockCreateNewRoom.mock.calls[0][5]
    // When useSupervisor is omitted, default is now true (Ultimate mode)
    expect(extendInfoArg.use_supervisor).toBe(true)
  })

  it('should omit debateMode from extendInfo', async () => {
    mockCreateNewRoom.mockResolvedValue({
      success: true,
      room: { room_id: 'room-sv-4' },
    })

    const { result } = renderHook(() => useChatRoomCreation(defaultProps))

    await act(async () => {
      await result.current.createRoomWithMessage('Hello', {
        selectedAgents: [mockAgent],
        useSupervisor: true,
      })
    })

    const extendInfoArg = mockCreateNewRoom.mock.calls[0][5]
    expect(extendInfoArg).toEqual(expect.objectContaining({
      use_supervisor: true,
      initialMessage: 'Hello',
    }))
  })

  it('should pass use_supervisor through createAndNavigate', async () => {
    mockCreateNewRoom.mockResolvedValue({
      success: true,
      room: { room_id: 'room-sv-5' },
    })

    const { result } = renderHook(() => useChatRoomCreation(defaultProps))

    await act(async () => {
      await result.current.createAndNavigate('Navigate test', {
        useSupervisor: true,
      })
    })

    const extendInfoArg = mockCreateNewRoom.mock.calls[0][5]
    expect(extendInfoArg).toHaveProperty('use_supervisor', true)
    expect(mockPush).toHaveBeenCalledWith('/room/room-sv-5')
  })

  it('should pass use_supervisor through createWithAgentsAndNavigate', async () => {
    mockCreateNewRoom.mockResolvedValue({
      success: true,
      room: { room_id: 'room-sv-6' },
    })

    const { result } = renderHook(() => useChatRoomCreation(defaultProps))

    await act(async () => {
      await result.current.createWithAgentsAndNavigate('With agents', [mockAgent], {
        useSupervisor: true,
      })
    })

    const extendInfoArg = mockCreateNewRoom.mock.calls[0][5]
    expect(extendInfoArg).toHaveProperty('use_supervisor', true)
    expect(mockPush).toHaveBeenCalledWith('/room/room-sv-6')
  })
})

import { describe, it, expect, vi, beforeEach } from "vitest"
import { cleanup, render, waitFor } from "@testing-library/react"
import { useRoomUiStore } from "@/stores/room-ui-store"

// --- Mocks ---

const mockSendUserMessage = vi.fn().mockResolvedValue(true)
const mockUseGroupManagement = vi.fn()

// Mock next/navigation
vi.mock("next/navigation", () => ({
  useParams: () => ({ id: "room-abc" }),
}))

// Mock useRoomWebhook — returns a stable room so the useEffect fires
vi.mock("@/hooks/useRoomWebhook", () => ({
  useRoomWebhook: () => ({
    room: { room_id: "room-abc", room_name: "Test Room", room_agent_set: { a1: "Agent One" } },
    loading: false,
    sending: false,
    processing: false,
    cancelling: false,
    sendUserMessage: mockSendUserMessage,
    cancelProcessing: vi.fn(),
    sseConnected: true,
    sseConnecting: false,
    sseEnabled: true,
    toggleSSE: vi.fn(),
    debateMode: false,
    supervisorMode: false,
  }),
}))

// Mock useGroupManagement
vi.mock("@/hooks/useGroupManagement", () => ({
  useGroupManagement: (options: unknown) => {
    mockUseGroupManagement(options)
    return {
      availableAgents: [],
      loadingAgents: false,
      agentsError: null,
      groups: [],
      loadingGroups: false,
      selectedGroup: "all_agents",
      isOverride: false,
      resolvedTargetMode: { message_target_mode: "all_agents" },
      groupManagementOpen: false,
      groupAction: null,
      handleGroupsChange: vi.fn(),
      handleCreateGroup: vi.fn(),
      handleEditGroup: vi.fn(),
      handleDeleteGroup: vi.fn(),
      handleGroupCreated: vi.fn(),
      handleGroupChange: vi.fn(),
      handleClearOverride: vi.fn(),
      setGroupManagementOpen: vi.fn(),
      setGroupAction: vi.fn(),
      loadAvailableAgents: vi.fn(),
      setAvailableAgents: vi.fn(),
    }
  },
}))

// Mock heavy child components to keep rendering fast
vi.mock("@/components/room-page-shell", () => ({
  RoomPageShell: ({ adapter }: {
    adapter: {
      externalValue?: string
      onSendMessage: (message: string, dispatch: { message_target_mode: 'saved_group', target_group_id: string }) => void
    }
  }) => (
    <div data-testid="shell" data-external-value={adapter.externalValue ?? ""}>
      <button
        type="button"
        data-testid="send-literal-mention"
        onClick={() => adapter.onSendMessage(
          "Send this literal <@agent-mentioned|Mentioned>",
          { message_target_mode: 'saved_group', target_group_id: 'group-abc' },
        )}
      />
    </div>
  ),
}))
vi.mock("@/components/require-auth", () => ({
  RequireAuth: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}))
vi.mock("@/components/group-management-modal", () => ({
  GroupManagementModal: () => null,
}))

let RoomChatPage: React.ComponentType

beforeEach(async () => {
  cleanup()
  vi.clearAllMocks()
  // Reset Zustand store
  useRoomUiStore.setState({ pendingRoomData: {} })
  const mod = await import("@/app/(portal)/room/[id]/page")
  RoomChatPage = mod.default
})

describe("Room page — prefill handoff consumer", () => {
  it("defaults room routing to room_default when the room has agents", async () => {
    render(<RoomChatPage />)

    await waitFor(() => {
      expect(mockUseGroupManagement).toHaveBeenCalled()
    })
    expect(mockUseGroupManagement.mock.calls.at(-1)?.[0]).toMatchObject({
      defaultGroup: "room_team",
      defaultGroupName: "Agent One",
      defaultTargetMode: { message_target_mode: "room_default" },
    })
  })

  it("prefills input (externalValue) instead of auto-sending when handoffMode is 'prefill'", async () => {
    // Seed Zustand store with prefill pending data
    useRoomUiStore.getState().setPendingRoomData("room-abc", {
      initialMessage: "Find top AI YouTubers",
      handoffMode: "prefill",
    })

    const { getByTestId } = render(<RoomChatPage />)

    await waitFor(() => {
      // Verify externalValue is passed through to the shell
      expect(getByTestId("shell").dataset.externalValue).toBe("Find top AI YouTubers")
    })

    // sendUserMessage should NOT have been called (prefill, not autosend)
    expect(mockSendUserMessage).not.toHaveBeenCalled()

    // Pending data should be consumed from the store
    expect(useRoomUiStore.getState().pendingRoomData["room-abc"]).toBeUndefined()
  })

  it("auto-sends when handoffMode is not set and final dispatch is persisted", async () => {
    useRoomUiStore.getState().setPendingRoomData("room-abc", {
      initialMessage: "Hello agents",
      mode: 'direct',
      agentScope: { source: 'room_default' },
      clientRequestId: 'request-stable',
    })

    render(<RoomChatPage />)

    await waitFor(() => {
      expect(mockSendUserMessage).toHaveBeenCalledOnce()
    })
    expect(mockSendUserMessage.mock.calls[0][0]).toEqual({
      userInput: "Hello agents",
      mode: "direct",
      agentScope: { source: "room_default" },
      clientRequestId: 'request-stable',
      pendingAttachments: undefined,
    })
  })

  it("reuses the same client request id when pending autosend is retried", async () => {
    mockSendUserMessage.mockResolvedValueOnce(false).mockResolvedValueOnce(true)
    useRoomUiStore.getState().setPendingRoomData("room-abc", {
      initialMessage: "Retry this request",
      mode: 'supervisor',
      agentScope: { source: 'all_agents' },
      clientRequestId: 'request-stable-retry',
    })

    const firstRender = render(<RoomChatPage />)
    await waitFor(() => {
      expect(mockSendUserMessage).toHaveBeenCalledTimes(1)
      expect(useRoomUiStore.getState().pendingRoomData["room-abc"]).toBeDefined()
    })
    firstRender.unmount()

    render(<RoomChatPage />)
    await waitFor(() => {
      expect(mockSendUserMessage).toHaveBeenCalledTimes(2)
    })

    expect(mockSendUserMessage.mock.calls.map(([input]) => input.clientRequestId)).toEqual([
      'request-stable-retry',
      'request-stable-retry',
    ])
  })

  it("blocks pending autosend when final dispatch is missing", async () => {
    useRoomUiStore.getState().setPendingRoomData("room-abc", {
      initialMessage: "Hello stale agents",
    })

    render(<RoomChatPage />)

    await waitFor(() => {
      expect(useRoomUiStore.getState().pendingRoomData["room-abc"]).toBeUndefined()
    })
    expect(mockSendUserMessage).not.toHaveBeenCalled()
  })

  it("does not reparse literal mention text over the supplied room dispatch", async () => {
    const { getByTestId } = render(<RoomChatPage />)

    getByTestId("send-literal-mention").click()

    await waitFor(() => {
      expect(mockSendUserMessage).toHaveBeenCalledOnce()
    })
    expect(mockSendUserMessage.mock.calls[0][0]).toEqual({
      userInput: "Send this literal <@agent-mentioned|Mentioned>",
      quoteData: undefined,
      pendingAttachments: undefined,
      mode: "direct",
      agentScope: { source: 'saved_group', group_id: 'group-abc' },
    })
  })
})

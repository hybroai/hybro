import { StrictMode } from "react"
import { describe, it, expect, vi, beforeEach } from "vitest"
import { render, screen, fireEvent, waitFor, cleanup } from "@testing-library/react"
import type { Agent } from "@/lib/types/agent"
import { useCaseTemplates } from "@/lib/use-case-templates"
import { useRoomUiStore } from "@/stores/room-ui-store"

// --- Mocks ---

const presetTeam = {
  group_id: 'travel-team',
  name: 'Travel Planner Team',
  description: 'Preset team for Travel Planner',
  type: 'user' as const,
  owner_id: 'user-1',
  agents: ['a1', 'a2'],
}
const mockEnsureUseCaseTeam = vi.fn().mockResolvedValue(presetTeam)
const mockCreateAndNavigate = vi.fn().mockResolvedValue(true)
const mockLoadAvailableAgents = vi.fn()

function makeAgent(id: string, name: string): Agent {
  return {
    agent_id: id,
    agent_card: {
      name, description: "", url: `https://ex.com/${id}`,
      version: "1.0.0", provider: { organization: "test", url: "https://test.com" },
      capabilities: {}, protocolVersion: "1.0.0",
      skills: [], defaultInputModes: ["text"], defaultOutputModes: ["text"],
    },
  }
}
const agents = [makeAgent("a1", "Weather Agent"), makeAgent("a2", "Travel Planner Agent")]
const travelPrompt = useCaseTemplates.find(
  (template) => template.id === "travel-planner",
)!.prefillMessage

// Mock useGroupManagement — returns controllable state
let gmState: Record<string, unknown> = {}
vi.mock("@/hooks/useGroupManagement", () => ({
  useGroupManagement: () => gmState,
}))

// Mock useChatRoomCreation
vi.mock("@/hooks/useChatRoomCreation", () => ({
  useChatRoomCreation: () => ({
    creating: false,
    createAndNavigate: mockCreateAndNavigate,
    loadDefaultAgents: vi.fn(),
    getAgentSuggestions: vi.fn(),
    createRoomWithMessage: vi.fn(),
    createWithAgentsAndNavigate: vi.fn(),
    defaultAgents: [],
  }),
}))

vi.mock('@/lib/use-case-team', () => ({
  ensureUseCaseTeam: (...args: unknown[]) => mockEnsureUseCaseTeam(...args),
}))

// Mock auth wrapper
vi.mock('@/lib/auth', () => ({
  useUser: () => ({ user: { id: 'user-1', firstName: 'Test' }, isLoaded: true }),
  useAuth: () => ({ getToken: vi.fn() }),
}))

// Mock next/navigation
vi.mock("next/navigation", () => ({
  useSearchParams: () => new URLSearchParams(),
  useRouter: () => ({ push: vi.fn() }),
}))

// Mock GroupManagementModal to avoid rendering complexity
vi.mock("@/components/group-management-modal", () => ({
  GroupManagementModal: () => null,
}))

// Dynamic import of the page component (after mocks are set up)
let ChatPage: React.ComponentType

beforeEach(async () => {
  cleanup()
  vi.clearAllMocks()
  useRoomUiStore.setState({ pendingChatHandoff: null })
  gmState = {
    availableAgents: agents,
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
    loadAvailableAgents: mockLoadAvailableAgents,
    setAvailableAgents: vi.fn(),
  }
  const mod = await import("@/app/(portal)/chat/page")
  ChatPage = mod.default
})

describe("Chat page — Use Case Cards integration", () => {
  it("renders the two remaining use case cards with the section label", async () => {
    render(<ChatPage />)
    await waitFor(() => {
      expect(screen.getByText("Travel Planner")).toBeDefined()
      expect(screen.getByText("Story & Image Creator")).toBeDefined()
      expect(screen.getByText("Featured Use Cases")).toBeDefined()
    })
    expect(screen.queryByText("Creator Discovery & Export")).toBeNull()
  })

  it("fills the composer and selects the preset team without navigating", async () => {
    const handleGroupCreated = vi.fn()
    gmState = { ...gmState, handleGroupCreated }
    const { container } = render(<ChatPage />)
    await waitFor(() => {
      expect(screen.getByText("Travel Planner")).toBeDefined()
    })

    fireEvent.click(screen.getByText("Travel Planner").closest("button")!)

    await waitFor(() => {
      expect(mockEnsureUseCaseTeam).toHaveBeenCalledWith(expect.objectContaining({
        ownerId: 'user-1',
        catalog: agents,
      }))
      expect(handleGroupCreated).toHaveBeenCalledWith(presetTeam)
      expect(container.querySelector('[contenteditable="true"]')?.textContent).toContain(
        travelPrompt,
      )
    })
    expect(mockCreateAndNavigate).not.toHaveBeenCalled()
  })

  it('uses the Story & Image preset and fills its own prompt', async () => {
    const storyTeam = {
      ...presetTeam,
      group_id: 'story-team',
      name: 'Story & Image Creator Team',
    }
    const handleGroupCreated = vi.fn()
    mockEnsureUseCaseTeam.mockResolvedValueOnce(storyTeam)
    gmState = { ...gmState, handleGroupCreated }
    const { container } = render(<ChatPage />)

    fireEvent.click(await screen.findByText('Story & Image Creator'))

    await waitFor(() => {
      expect(mockEnsureUseCaseTeam).toHaveBeenCalledWith(expect.objectContaining({
        template: expect.objectContaining({ id: 'story-and-image' }),
      }))
      expect(handleGroupCreated).toHaveBeenCalledWith(storyTeam)
      expect(container.querySelector('[contenteditable="true"]')?.textContent).toContain(
        'Give me a short fun story about AI Agents',
      )
    })
  })

  it('allows editing after the same use case is selected twice', async () => {
    const { container } = render(<ChatPage />)
    const card = await screen.findByText('Travel Planner')

    fireEvent.click(card.closest('button')!)
    await waitFor(() => {
      expect(container.querySelector('[contenteditable="true"]')?.textContent).toContain(
        travelPrompt,
      )
    })
    fireEvent.click(card.closest('button')!)
    await waitFor(() => expect(mockEnsureUseCaseTeam).toHaveBeenCalledTimes(2))

    const editor = container.querySelector('[contenteditable="true"]') as HTMLElement
    editor.textContent = 'My edited trip request'
    fireEvent.input(editor)

    await waitFor(() => {
      expect(editor.textContent).toBe('My edited trip request')
    })
  })

  it("disables cards when catalog is loading", async () => {
    gmState = { ...gmState, loadingAgents: true, availableAgents: [] }
    const { container } = render(<ChatPage />)
    await waitFor(() => {
      const cards = container.querySelectorAll("button[disabled]")
      expect(cards.length).toBeGreaterThanOrEqual(2)
    })
  })

  it("applies a single-agent handoff once under Strict Mode", async () => {
    useRoomUiStore.getState().setPendingChatHandoff({
      draft: "Find creators for my channel",
      seedAgents: [{
        agent_id: "a1",
        agent_card: { name: "YouTube Creator Finder Agent" } as never,
      }],
    })

    const { container } = render(
      <StrictMode>
        <ChatPage />
      </StrictMode>,
    )

    await waitFor(() => {
      expect(container.querySelector('[contenteditable="true"]')?.textContent).toContain(
        'Find creators for my channel',
      )
    })
    expect(document.activeElement).toHaveAttribute('contenteditable', 'true')
    expect(useRoomUiStore.getState().pendingChatHandoff).toBeNull()
  })

  it('clears a single-agent handoff seed when a use case takes over scope', async () => {
    useRoomUiStore.getState().setPendingChatHandoff({
      draft: 'Chat with this agent',
      seedAgents: [{
        agent_id: 'handoff-agent',
        agent_card: { name: 'Handoff Agent' } as never,
      }],
    })
    const handleGroupCreated = vi.fn((team) => {
      gmState = {
        ...gmState,
        selectedGroup: team.group_id,
        selectedGroupName: team.name,
      }
    })
    gmState = { ...gmState, handleGroupCreated }

    const { container } = render(<ChatPage />)

    await waitFor(() => {
      expect(container.querySelector('[contenteditable="true"]')?.textContent).toContain(
        'Chat with this agent',
      )
    })

    fireEvent.click(screen.getByText('Travel Planner').closest('button')!)

    await waitFor(() => {
      expect(handleGroupCreated).toHaveBeenCalledWith(presetTeam)
    })

    const editor = container.querySelector('[contenteditable="true"]') as HTMLElement
    editor.textContent = 'Plan my trip'
    fireEvent.input(editor)

    fireEvent.click(screen.getByLabelText('Send message'))

    await waitFor(() => {
      expect(mockCreateAndNavigate).toHaveBeenCalledWith(
        'Plan my trip',
        expect.objectContaining({
          selectedAgents: undefined,
          targetGroup: 'travel-team',
        }),
      )
    })
  })

  it("shows To Be Continued when catalog load fails", async () => {
    gmState = { ...gmState, agentsError: "Network error", availableAgents: [] }
    render(<ChatPage />)
    await waitFor(() => {
      expect(screen.getByText("To Be Continued")).toBeDefined()
    })
    expect(screen.queryByText("Failed to load agents")).toBeNull()
    expect(screen.queryByText("Retry")).toBeNull()
    expect(mockLoadAvailableAgents).not.toHaveBeenCalled()
  })
})

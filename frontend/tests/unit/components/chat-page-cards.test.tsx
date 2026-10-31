import { StrictMode } from "react"
import { describe, it, expect, vi, beforeEach } from "vitest"
import { render, screen, fireEvent, waitFor, cleanup } from "@testing-library/react"
import { createAgent } from "../../fixtures"
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
  return createAgent({ agent_id: id, name })
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

// Mock the setup status; undefined means not loaded yet.
let setupStatus: Record<string, unknown> | undefined
vi.mock("@/hooks/useSetupStatus", () => ({
  useSetupStatus: () => ({ data: setupStatus }),
}))

// The full agent catalog; undefined means not loaded yet.
let catalogAgents: Agent[] | undefined
vi.mock("@/hooks/room/useAgentCatalog", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/room/useAgentCatalog")>()),
  useAgentCatalog: () => ({ allAgentsData: catalogAgents }),
}))

// Mock next/navigation
vi.mock("next/navigation", () => ({
  usePathname: () => "/chat",
  useSearchParams: () => new URLSearchParams(),
  useRouter: () => ({ push: vi.fn() }),
}))

// Mock the subnet dialog host to avoid rendering complexity
vi.mock("@/components/subnets/subnet-dialog-host", () => ({
  SubnetDialogHost: () => null,
}))

// Dynamic import of the page component (after mocks are set up)
let ChatPage: React.ComponentType

beforeEach(async () => {
  cleanup()
  vi.clearAllMocks()
  useRoomUiStore.setState({ pendingChatHandoff: null })
  setupStatus = undefined
  catalogAgents = undefined
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

describe("Chat page — model provider gating", () => {
  const configured = { state: "configured", provider: "openai", auth: "api_key", text_model: "gpt-5-mini", image_model: "gpt-image-1", image_size: "auto", last_error: null }

  const story = createAgent({ agent_id: "s1", name: "Story Agent", source: "bundled" })
  const image = (status: "active" | "inactive", reason: Agent["inactive_reason"] = null) =>
    createAgent({ agent_id: "i1", name: "Image Generator Agent", source: "bundled", status, inactive_reason: reason })

  it("disables the image use case while the Image Generator waits for an image model", async () => {
    setupStatus = { ...configured, image_model: null }
    catalogAgents = [...agents, story, image("inactive", "image_model_not_configured")]
    render(<ChatPage />)
    const card = (await screen.findByText("Story & Image Creator")).closest("button")!
    expect(card).toBeDisabled()
    expect(card).toHaveTextContent("Needs an image model. Choose one in Settings, Model provider.")
    expect(screen.getByText("Travel Planner").closest("button")).not.toBeDisabled()
    expect(screen.queryByTestId("setup-banner")).toBeNull()
  })

  it("enables the image use case once the Image Generator is active", async () => {
    setupStatus = configured
    catalogAgents = [...agents, story, image("active")]
    render(<ChatPage />)
    expect((await screen.findByText("Story & Image Creator")).closest("button")).not.toBeDisabled()
  })

  it("keeps a use case enabled when its agent is only offline", async () => {
    setupStatus = configured
    catalogAgents = [...agents, story, image("inactive")]
    render(<ChatPage />)
    const card = (await screen.findByText("Story & Image Creator")).closest("button")!
    expect(card).not.toHaveTextContent("Needs")
  })

  it("starts a use case with agents from the full catalog", async () => {
    setupStatus = configured
    catalogAgents = [...agents, story, image("active")]
    gmState = { ...gmState, availableAgents: agents }
    render(<ChatPage />)
    fireEvent.click(await screen.findByText("Story & Image Creator"))
    await waitFor(() => expect(mockEnsureUseCaseTeam).toHaveBeenCalledWith(expect.objectContaining({
      catalog: [...agents, story, image("active")],
    })))
  })

  it("shows the permanent banner and disables the composer when unconfigured", async () => {
    setupStatus = { state: "unconfigured", image_size: "auto" }
    const { container } = render(<ChatPage />)
    const banner = await screen.findByTestId("setup-banner")
    expect(banner).toHaveTextContent("Connect a model provider")
    expect(banner).toHaveTextContent("Room Chat needs a model to plan and answer. This stays until a provider works.")
    expect(banner).toHaveAttribute("data-tone", "warning")
    expect(screen.getByRole("link", { name: "Set up provider" })).toHaveAttribute("href", "/chat?settings=model-provider")
    expect(screen.queryByRole("button", { name: "Dismiss" })).toBeNull()
    const editor = container.querySelector('[contenteditable="false"]')
    expect(editor).toHaveAttribute("data-placeholder", "Connect a model provider to start a chat")
    expect(screen.getByText("Travel Planner").closest("button")).toBeDisabled()
  })

  it("shows the red banner and keeps the composer disabled when the ChatGPT sign-in expired", async () => {
    setupStatus = { ...configured, auth: "oauth", last_error: "oauth_refresh_failed" }
    const { container } = render(<ChatPage />)
    const banner = await screen.findByTestId("setup-banner")
    expect(banner).toHaveTextContent("ChatGPT sign-in expired")
    expect(banner).toHaveTextContent("Room Chat is paused. Existing chats are kept; sign in again to continue.")
    expect(banner).toHaveAttribute("data-tone", "error")
    expect(screen.getByRole("link", { name: "Sign in again" })).toHaveAttribute("href", "/chat?settings=model-provider")
    expect(container.querySelector('[contenteditable="false"]')).not.toBeNull()
  })
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
      seedAgents: [createAgent({ agent_id: "a1", name: "YouTube Creator Finder Agent" })],
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
      seedAgents: [createAgent({ agent_id: 'handoff-agent', name: 'Handoff Agent' })],
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

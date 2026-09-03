import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import { CoreHeroComposer } from '@/components/portal/core-hero-composer'
import { useCaseTemplates } from '@/lib/use-case-templates'

vi.mock('@/lib/auth', () => ({
  useUser: () => ({
    user: { id: 'user-1', firstName: 'Dev', username: 'developer_local' },
    isLoaded: true,
    isSignedIn: true,
  }),
  useAuth: () => ({ getToken: vi.fn() }),
}))

vi.mock('@/lib/api/agent', () => ({
  getAllAgents: vi.fn().mockResolvedValue({ success: true, agents: [] }),
}))

const creationOptions: { current?: { onRequireAuth?: unknown } } = {}

vi.mock('@/hooks/useChatRoomCreation', () => ({
  useChatRoomCreation: (options: { onRequireAuth?: unknown }) => {
    creationOptions.current = options
    return {
      creating: false,
      createFromTemplate: vi.fn(),
      createWithAgentsAndNavigate: vi.fn(),
    }
  },
}))

vi.mock('@/components/room-chat-input', () => ({
  RoomChatInput: ({
    externalValue,
    disableAttachmentButton,
    disableMentionButton,
  }: {
    externalValue?: string
    disableAttachmentButton?: boolean
    disableMentionButton?: boolean
  }) => (
    <div
      data-testid="hero-demo"
      data-value={externalValue ?? ''}
      data-disable-attach={String(!!disableAttachmentButton)}
      data-disable-mention={String(!!disableMentionButton)}
    />
  ),
}))

afterEach(() => {
  cleanup()
  vi.useRealTimers()
})

describe('CoreHeroComposer', () => {
  it('typewrites hybro featured use cases, not SaaS creator discovery', async () => {
    vi.useFakeTimers()
    render(<CoreHeroComposer />)

    await vi.advanceTimersByTimeAsync(55 * 12)

    const value = screen.getByTestId('hero-demo').getAttribute('data-value') ?? ''
    expect(value).toContain('<@Weather Agent|Weather Agent>')
    expect(value).toContain('<@Travel Planner Agent|Travel Planner Agent>')
    expect(value).not.toMatch(/YouTube|Twitch|Creator Discovery/i)
    expect(useCaseTemplates.map((t) => t.id)).toEqual(['travel-planner', 'story-and-image'])
  })

  it('disables mention and attach buttons on the demo composer', () => {
    render(<CoreHeroComposer />)
    const demo = screen.getByTestId('hero-demo')
    expect(demo.getAttribute('data-disable-attach')).toBe('true')
    expect(demo.getAttribute('data-disable-mention')).toBe('true')
  })

  it('does not wire a sign-in redirect for the demo composer', () => {
    render(<CoreHeroComposer />)
    expect(creationOptions.current?.onRequireAuth).toBeUndefined()
  })
})

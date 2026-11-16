import { describe, expect, it, vi } from 'vitest'
import {
  FOCUS_SCROLL_TOP_OFFSET_PX,
  scrollUserMessageToFocus,
} from '@/lib/conversation/focus-scroll'

function mockRect(top: number, height = 40) {
  return { top, bottom: top + height, height, left: 0, right: 100, width: 100, x: 0, y: top, toJSON: () => ({}) }
}

describe('focus-scroll', () => {
  it('scrolls user message near the top of the container', () => {
    const container = {
      scrollTop: 200,
      scrollTo: vi.fn(),
      getBoundingClientRect: () => mockRect(50),
    } as unknown as HTMLElement

    const userMessage = {
      getBoundingClientRect: () => mockRect(250),
    } as HTMLElement

    scrollUserMessageToFocus(container, userMessage)

    expect(container.scrollTo).toHaveBeenCalledWith({
      top: 200 + (250 - 50 - FOCUS_SCROLL_TOP_OFFSET_PX),
      behavior: 'auto',
    })
  })
})

import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { MarkdownContent } from '@/components/markdown-content'

vi.mock('@/hooks/useRoomFile', () => ({
  useRoomFile: () => ({ objectUrl: null, error: null }),
}))

afterEach(() => cleanup())

describe('MarkdownContent images', () => {
  it('does not request a remote image until the user clicks', () => {
    const { container } = render(
      <MarkdownContent content="![chart](https://images.example.com/chart.png)" />,
    )

    expect(container.querySelector('img')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: /Load image from images\.example\.com/ }))

    const img = container.querySelector('img')
    expect(img?.getAttribute('src')).toBe('https://images.example.com/chart.png')
    expect(img?.getAttribute('alt')).toBe('chart')
    expect(img?.getAttribute('referrerpolicy')).toBe('no-referrer')
  })

  it('loads a linked remote image without following the link', () => {
    const { container } = render(
      <MarkdownContent content="[![chart](https://images.example.com/chart.png)](https://example.com/report)" />,
    )

    const button = screen.getByRole('button', { name: /Load image from/ })
    const clickEvent = new MouseEvent('click', { bubbles: true, cancelable: true })
    act(() => { button.dispatchEvent(clickEvent) })

    expect(clickEvent.defaultPrevented).toBe(true)
    expect(container.querySelector('img')).not.toBeNull()
  })

  it('renders same-origin and inline images directly', () => {
    const dataUrl = 'data:image/png;base64,iVBORw0KGgo='
    const { container } = render(
      <MarkdownContent content={`![local](/static/logo.png)\n\n![inline](${dataUrl})`} />,
    )

    const sources = Array.from(container.querySelectorAll('img')).map((img) => img.getAttribute('src'))
    expect(sources).toContain('/static/logo.png')
    expect(screen.queryByRole('button', { name: /Load image from/ })).toBeNull()
  })
})

// @vitest-environment node
import { renderToString } from 'react-dom/server'
import { describe, expect, it, vi } from 'vitest'
import { MarkdownContent } from '@/components/markdown-content'

vi.mock('@/hooks/useRoomFile', () => ({
  useRoomFile: () => ({ objectUrl: null, error: null }),
}))

// Server rendering has no window, so the origin check cannot compare origins.
describe('MarkdownContent images in server rendering', () => {
  it('emits no remote image or preload for absolute and protocol-relative URLs', () => {
    expect(typeof window).toBe('undefined')
    const html = renderToString(
      <MarkdownContent
        content={'![a](https://images.example.com/a.png)\n\n![b](//cdn.example.org/b.png)'}
      />,
    )

    // No src or href (img, or React's preload link) points at another host.
    expect(html).not.toMatch(/(src|href)="(https?:)?\/\//)
    expect(html).not.toMatch(/(src|href)="[^"]*(images\.example\.com|cdn\.example\.org)/)
    expect(html.replaceAll('<!-- -->', '')).toContain('Load image from images.example.com')
  })

  it('still renders same-origin paths directly', () => {
    const html = renderToString(<MarkdownContent content="![logo](/static/logo.png)" />)

    expect(html).toContain('src="/static/logo.png"')
    expect(html).not.toContain('Load image from')
  })
})

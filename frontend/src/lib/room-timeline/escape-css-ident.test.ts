import { describe, it, expect } from 'vitest'
import { escapeCssIdent } from './escape-css-ident'

describe('escapeCssIdent', () => {
  it('passes through simple strings', () => {
    expect(escapeCssIdent('abc-123')).toBe('abc-123')
  })

  it('escapes double quotes in fallback mode', () => {
    const originalCss = globalThis.CSS
    // @ts-expect-error test override — remove CSS entirely to test fallback
    globalThis.CSS = undefined
    try {
      expect(escapeCssIdent('a"b')).toBe('a\\"b')
    } finally {
      globalThis.CSS = originalCss
    }
  })
})

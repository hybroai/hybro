import { describe, expect, it } from 'vitest'
import { formatSessionDuration, formatSessionTime } from '@/lib/session-format'

describe('session formatting', () => {
  it('formats times in English regardless of the browser locale', () => {
    expect(formatSessionTime('2026-10-14T18:04:58Z', 'UTC')).toBe('Oct 14, 6:04 PM')
    expect(formatSessionTime('not a date', 'UTC')).toBe('')
    expect(formatSessionTime(null, 'UTC')).toBe('')
  })

  it('formats durations compactly', () => {
    expect(formatSessionDuration(28807)).toBe('29s')
    expect(formatSessionDuration(130000)).toBe('2m 10s')
    expect(formatSessionDuration(null)).toBe('')
  })
})

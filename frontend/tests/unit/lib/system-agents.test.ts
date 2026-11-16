// tests/unit/lib/system-agents.test.ts
import { describe, it, expect } from 'vitest'
import {
  isSystemAgent,
  isSummarySystemAgent,
} from '@/lib/system-agents'

describe('isSummarySystemAgent', () => {
  it('returns true for system:hybro', () => {
    expect(isSummarySystemAgent('system:hybro')).toBe(true)
  })

  it('returns false for system:clarifier', () => {
    expect(isSummarySystemAgent('system:clarifier')).toBe(false)
  })

  it('returns false for undefined', () => {
    expect(isSummarySystemAgent(undefined)).toBe(false)
  })

  it('returns false for random agent id', () => {
    expect(isSummarySystemAgent('agent-123')).toBe(false)
  })
})

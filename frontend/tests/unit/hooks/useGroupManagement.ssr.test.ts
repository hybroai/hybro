// @vitest-environment node

import React from 'react'
import { renderToString } from 'react-dom/server'
import { describe, expect, it, vi } from 'vitest'

vi.mock('@/lib/api/agent-group', () => ({
  listAgentGroups: vi.fn(),
}))

vi.mock('@/lib/api/agent', () => ({
  getAllAgents: vi.fn(),
}))

import { useGroupManagement } from '@/hooks/useGroupManagement'

function Harness() {
  useGroupManagement({
    userId: 'user-1',
    getToken: async () => null,
    isLoaded: true,
    roomId: 'room-1',
  })
  return React.createElement('div')
}

describe('useGroupManagement SSR', () => {
  it('does not access localStorage during server rendering', () => {
    expect(() => renderToString(React.createElement(Harness))).not.toThrow()
  })
})

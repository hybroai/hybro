import { describe, expect, it } from 'vitest'
import { sessionStateView } from './status'
import { SESSION_STATES } from './session-list-state'

describe('session state labels', () => {
  it('labels a step a check rule denied apart from an owner rejection', () => {
    expect(sessionStateView('denied').label).toBe('Denied')
    expect(sessionStateView('rejected').label).toBe('Rejected')
  })

  it('offers every state in the list filter', () => {
    expect(SESSION_STATES).toContain('denied')
  })
})

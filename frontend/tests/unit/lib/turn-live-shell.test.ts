import { describe, expect, it } from 'vitest'
import type { TurnViewModel } from '@/lib/room-timeline/types'
import {
  getAgentIndexSummary,
  defaultAgentIndexOpen,
} from '@/lib/room-timeline/turn-live-shell'

function makeTurn(overrides: Partial<TurnViewModel>): TurnViewModel {
  return {
    id: 't1',
    conversationId: 'r1',
    userMessageId: 'u1',
    userContent: 'hi',
    userAttachments: [],
    timestamp: '2026-01-01T00:00:00Z',
    status: 'completed',
    agentResults: [],
    activeAgentIds: [],
    isSupervisorTurn: true,
    displayMode: 'parallel_results',
    phase: 'completed',
    finalAnswer: { kind: 'pending', label: 'Working' },
    ...overrides,
  }
}

function makeAgentResult(
  overrides: Partial<TurnViewModel['agentResults'][number]>,
): TurnViewModel['agentResults'][number] {
  return {
    messageId: 'm1',
    agentName: 'Agent',
    status: 'completed',
    content: 'response',
    isSummaryAgent: false,
    isEphemeral: false,
    artifacts: [],
    ...overrides,
  }
}

describe('getAgentIndexSummary', () => {
  it('returns "Agent responses" prefix for deterministic_done live turn', () => {
    const agents = [
      makeAgentResult({ messageId: 'a1', agentId: 'agent-a', status: 'completed', content: 'A' }),
      makeAgentResult({ messageId: 'a2', agentId: 'agent-b', status: 'completed', content: 'B' }),
    ]
    const turn = makeTurn({
      status: 'active',
      phase: 'answering',
      finalAnswer: { kind: 'deterministic_done', label: 'Combined agent responses' },
      agentResults: agents,
    })
    const summary = getAgentIndexSummary(turn, agents)
    expect(summary).toContain('Agent responses')
    expect(summary).toContain('2 done')
  })

  it('reports completed deterministic agent invocations as calls', () => {
    const agents = [
      makeAgentResult({ messageId: 'a1', agentId: 'agent-a' }),
      makeAgentResult({ messageId: 'a2', agentId: 'agent-b' }),
    ]
    const turn = makeTurn({
      status: 'completed',
      phase: 'completed',
      finalAnswer: { kind: 'deterministic_done', label: 'Combined agent responses' },
      agentResults: agents,
    })
    const summary = getAgentIndexSummary(turn, agents)
    expect(summary).toBe('Agent responses · 2 calls')
  })

  it('returns "Sources" prefix for llm_synthesis turn', () => {
    const agents = [
      makeAgentResult({ messageId: 'a1' }),
      makeAgentResult({ messageId: 'a2' }),
    ]
    const turn = makeTurn({
      status: 'completed',
      phase: 'completed',
      finalAnswer: { kind: 'llm_synthesis', label: 'Synthesized', primaryMessageId: 's1' },
      agentResults: [
        ...agents,
        makeAgentResult({ messageId: 's1', isSummaryAgent: true, content: 'Synthesis' }),
      ],
    })
    const summary = getAgentIndexSummary(turn, agents)
    expect(summary).toBe('Sources · 2 agent calls')
  })

  it('reports that a HITL turn is waiting for input', () => {
    const agents = [
      makeAgentResult({ messageId: 'a1', status: 'completed' }),
    ]
    const turn = makeTurn({
      displayMode: 'awaiting_input',
      finalAnswer: { kind: 'hitl', label: 'Needs input', hitl: { source: 'agent', prompts: [] } },
      agentResults: agents,
    })
    const summary = getAgentIndexSummary(turn, agents)
    expect(summary).toBe('Waiting for input · 1 call')
  })

  it('returns "Activity" prefix for pending multi-agent turn', () => {
    const agents = [
      makeAgentResult({ messageId: 'a1', status: 'working', content: '' }),
      makeAgentResult({ messageId: 'a2', status: 'working', content: '' }),
    ]
    const turn = makeTurn({
      status: 'active',
      displayMode: 'working',
      phase: 'collecting',
      finalAnswer: { kind: 'pending', label: 'Working' },
      agentResults: agents,
    })
    const summary = getAgentIndexSummary(turn, agents)
    expect(summary).toContain('Activity')
    expect(summary).toContain('2 working')
  })
})

describe('defaultAgentIndexOpen', () => {
  it('expands last completed deterministic_done turn on page load', () => {
    const turn = makeTurn({
      status: 'completed',
      phase: 'completed',
      finalAnswer: { kind: 'deterministic_done', label: 'Combined agent responses' },
    })
    expect(defaultAgentIndexOpen(turn, true)).toBe(true)
  })

  it('expands last completed llm_synthesis turn on page load', () => {
    const turn = makeTurn({
      status: 'completed',
      phase: 'completed',
      finalAnswer: { kind: 'llm_synthesis', label: 'Synthesized', primaryMessageId: 's1' },
    })
    expect(defaultAgentIndexOpen(turn, true)).toBe(true)
  })

  it('collapses historical completed turns', () => {
    const turn = makeTurn({
      status: 'completed',
      phase: 'completed',
      finalAnswer: { kind: 'llm_synthesis', label: 'Synthesized', primaryMessageId: 's1' },
    })
    expect(defaultAgentIndexOpen(turn, false)).toBe(false)
  })

  it('expands active last turn while still working', () => {
    const turn = makeTurn({
      status: 'active',
      phase: 'collecting',
      finalAnswer: { kind: 'pending', label: 'Working' },
    })
    expect(defaultAgentIndexOpen(turn, true)).toBe(true)
  })
})

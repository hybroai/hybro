import type { AgentResultViewModel, FinalAnswerKind, TurnViewModel } from './types'

export function getAgentIndexSummary(
  turn: TurnViewModel,
  sourceResults: AgentResultViewModel[],
  kind: FinalAnswerKind = turn.finalAnswer.kind,
): string {
  const total = sourceResults.length
  if (total === 0) return ''

  const isComplete = turn.phase === 'completed' || turn.status === 'completed'
  const done = sourceResults.filter(r => r.status === 'completed').length
  const working = sourceResults.filter(r => r.status === 'working').length

  switch (kind) {
    case 'deterministic_done':
      if (isComplete) return `Agent responses · ${total} call${total === 1 ? '' : 's'}`
      return [
        `Agent responses · ${total} call${total === 1 ? '' : 's'}`,
        done > 0 ? `${done} done` : '',
        working > 0 ? `${working} working` : '',
      ].filter(Boolean).join(' · ')
    case 'llm_synthesis':
      return `Sources · ${total} agent call${total === 1 ? '' : 's'}`
    case 'canceled':
      return `Canceled · ${total} call${total === 1 ? '' : 's'}`
    case 'failed': {
      const failedCount = sourceResults.filter(r => r.status === 'failed').length
      return `Failed · ${failedCount || total} of ${total} call${total === 1 ? '' : 's'}`
    }
    case 'hitl':
      return `Waiting for input · ${total} call${total === 1 ? '' : 's'}`
    case 'pending':
    default:
      if (isComplete) return `Activity · ${total} call${total === 1 ? '' : 's'}`
      return [
        `Activity · ${total} call${total === 1 ? '' : 's'}`,
        done > 0 ? `${done} done` : '',
        working > 0 ? `${working} working` : '',
      ].filter(Boolean).join(' · ')
  }
}

/**
 * Whether AgentIndex should mount expanded. The last turn on a page matches the
 * post-completion layout (open index); older turns stay collapsed.
 */
export function defaultAgentIndexOpen(turn: TurnViewModel, isLastTurn: boolean): boolean {
  if (!isLastTurn) return false

  const { finalAnswer } = turn
  if (finalAnswer.kind === 'single') return false

  const isComplete =
    turn.phase === 'completed'
    || turn.status === 'completed'
    || turn.status === 'failed'
    || turn.status === 'partial'
  if (!isComplete) return true

  return (
    finalAnswer.kind === 'deterministic_done'
    || finalAnswer.kind === 'llm_synthesis'
    || finalAnswer.kind === 'canceled'
    || finalAnswer.kind === 'failed'
    || finalAnswer.kind === 'hitl'
  )
}

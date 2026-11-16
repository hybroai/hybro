import { describe, it, expect, beforeEach } from 'vitest'
import { selectPendingHitls } from '@/lib/selectors/select-hitl'
import { useMessageStore } from '@/stores/message-store'
import { createAgentMessage, resetCounters } from '../../../fixtures'
import { TASK_STATE } from '@/lib/types/sse'

function setup(msgs: ReturnType<typeof createAgentMessage>[]) {
  const store = useMessageStore.getState()
  store.clearRoom()
  store.setRoom('room-1')
  for (const m of msgs) store.upsertMessage(m, 'db')
  const s = useMessageStore.getState()
  return { entities: s.entities, orderedIds: s.orderedIds }
}

describe('selectPendingHitls', () => {
  beforeEach(() => {
    useMessageStore.getState().clearRoom()
    resetCounters()
  })

  it('returns non-grouped unresolved HITL', () => {
    const { entities, orderedIds } = setup([
      createAgentMessage({
        id: 'hitl-1', conversationId: 'room-1',
        hitlRequestId: 'req-1', hitlPrompt: 'What is your name?',
        senderName: 'Analyst', taskStatus: TASK_STATE.INPUT_REQUIRED,
      }),
    ])
    const result = selectPendingHitls('room-1', entities, orderedIds)
    expect(result).toHaveLength(1)
    expect(result[0].hitlId).toBe('req-1')
    expect(result[0].question).toBe('What is your name?')
    expect(result[0].isAnswered).toBe(false)
  })

  it('excludes resolved non-grouped HITL', () => {
    const { entities, orderedIds } = setup([
      createAgentMessage({
        id: 'hitl-1', conversationId: 'room-1',
        hitlRequestId: 'req-1', hitlResolved: true,
        hitlPrompt: 'Done?', hitlUserAnswer: 'Yes',
        senderName: 'Analyst', taskStatus: TASK_STATE.COMPLETED,
      }),
    ])
    const result = selectPendingHitls('room-1', entities, orderedIds)
    expect(result).toHaveLength(0)
  })

  it('returns entire group when any member is unanswered', () => {
    const { entities, orderedIds } = setup([
      createAgentMessage({
        id: 'g1-q1', conversationId: 'room-1',
        hitlRequestId: 'req-1', hitlPrompt: 'Q1?',
        hitlGroupId: 'group-A', hitlGroupTotal: 2, hitlGroupIndex: 0,
        hitlResolved: true, hitlUserAnswer: 'A1',
        senderName: 'Analyst', taskStatus: TASK_STATE.INPUT_REQUIRED,
      }),
      createAgentMessage({
        id: 'g1-q2', conversationId: 'room-1',
        hitlRequestId: 'req-2', hitlPrompt: 'Q2?',
        hitlGroupId: 'group-A', hitlGroupTotal: 2, hitlGroupIndex: 1,
        senderName: 'Analyst', taskStatus: TASK_STATE.INPUT_REQUIRED,
      }),
    ])
    const result = selectPendingHitls('room-1', entities, orderedIds)
    expect(result).toHaveLength(2)
    expect(result[0].isAnswered).toBe(true)
    expect(result[1].isAnswered).toBe(false)
    expect(result[0].groupId).toBe('group-A')
  })

  it('passes through choice promptType and choices', () => {
    const { entities, orderedIds } = setup([
      createAgentMessage({
        id: 'hitl-c', conversationId: 'room-1',
        hitlRequestId: 'req-c', hitlPrompt: 'Pick one',
        hitlPromptType: 'choice', hitlChoices: ['A', 'B', 'C'],
        senderName: 'Analyst', taskStatus: TASK_STATE.INPUT_REQUIRED,
      }),
    ])
    const result = selectPendingHitls('room-1', entities, orderedIds)
    expect(result).toHaveLength(1)
    expect(result[0].promptType).toBe('choice')
    expect(result[0].choices).toEqual(['A', 'B', 'C'])
  })

  it('passes through confirmation promptType', () => {
    const { entities, orderedIds } = setup([
      createAgentMessage({
        id: 'hitl-cf', conversationId: 'room-1',
        hitlRequestId: 'req-cf', hitlPrompt: 'Approve deploy?',
        hitlPromptType: 'confirmation',
        senderName: 'Analyst', taskStatus: TASK_STATE.INPUT_REQUIRED,
      }),
    ])
    const result = selectPendingHitls('room-1', entities, orderedIds)
    expect(result).toHaveLength(1)
    expect(result[0].promptType).toBe('confirmation')
    expect(result[0].choices).toBeUndefined()
  })

  it('defaults promptType to text when hitlPromptType is undefined', () => {
    const { entities, orderedIds } = setup([
      createAgentMessage({
        id: 'hitl-t', conversationId: 'room-1',
        hitlRequestId: 'req-t', hitlPrompt: 'What?',
        senderName: 'Analyst', taskStatus: TASK_STATE.INPUT_REQUIRED,
      }),
    ])
    const result = selectPendingHitls('room-1', entities, orderedIds)
    expect(result[0].promptType).toBe('text')
  })

  it('excludes HITL from other rooms', () => {
    const { entities, orderedIds } = setup([
      createAgentMessage({
        id: 'hitl-other', conversationId: 'room-2',
        hitlRequestId: 'req-1', hitlPrompt: 'Q?',
        senderName: 'Analyst', taskStatus: TASK_STATE.INPUT_REQUIRED,
      }),
    ])
    const result = selectPendingHitls('room-1', entities, orderedIds)
    expect(result).toHaveLength(0)
  })
})

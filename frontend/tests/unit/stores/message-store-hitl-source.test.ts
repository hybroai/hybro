import { describe, expect, it } from 'vitest'
import { buildPendingHitlIncomingMessage } from '@/lib/hitl/hitl-message-projection'
import { applyUpsert } from '@/stores/message-store/upsert'

function supervisorHitl() {
  return buildPendingHitlIncomingMessage(supervisorHitlInput())
}

function supervisorHitlInput(): Parameters<typeof buildPendingHitlIncomingMessage>[0] {
  return {
    conversationId: 'room-1',
    messageId: 'message-1',
    requestId: 'request-1',
    source: 'supervisor',
    prompt: 'Choose a market',
    promptType: 'choice',
    choices: ['A', 'B'],
    timestamp: '2026-08-17T00:00:00Z',
    agentId: null,
    agentName: null,
    agentSource: undefined,
    expiresAt: null,
    groupId: 'group-1',
    groupTotal: 1,
    groupIndex: 0,
    stepNumber: null,
    totalSteps: null,
    relatedMessageId: 'user-message-1',
    clientRequestId: 'client-1',
  }
}

describe('message store HITL source projection', () => {
  it('preserves supervisor ownership when hydrating a new entity', () => {
    const incoming = supervisorHitl()
    const result = applyUpsert({}, [], incoming, 'db')

    expect(result?.entities[incoming.id].hitlSource).toBe('supervisor')
  })

  it('preserves a check rule as the source of a system question', () => {
    const incoming = buildPendingHitlIncomingMessage({
      ...supervisorHitlInput(),
      source: 'system',
      promptType: 'approval',
      choices: ['Approve', 'Reject'],
    })
    const result = applyUpsert({}, [], incoming, 'db')

    expect(result?.entities[incoming.id].hitlSource).toBe('system')
  })

  it('does not regress an answered interaction from stale REST pending data', () => {
    const pending = buildPendingHitlIncomingMessage({
      conversationId: 'room-1',
      messageId: 'message-1',
      requestId: 'request-1',
      source: 'agent',
      prompt: 'Which market?',
      promptType: 'text',
      choices: null,
      timestamp: '2026-08-17T00:00:00Z',
      agentId: null,
      agentName: 'Agent',
      agentSource: undefined,
      expiresAt: null,
      interactionId: 'interaction-1',
      interactionStatus: 'open',
      interactionVersion: 1,
      groupId: 'interaction-1',
      groupTotal: 1,
      groupIndex: 0,
      stepNumber: null,
      totalSteps: null,
      relatedMessageId: 'user-1',
      clientRequestId: 'client-1',
    })
    const initial = applyUpsert({}, [], pending, 'sse')!
    const answered = applyUpsert(initial.entities, [pending.id], {
      ...pending,
      hitlResolved: true,
      hitlUserAnswer: 'London',
      hitlInteractionStatus: 'responded',
      hitlApplicationStatus: 'applied',
    }, 'sse')!

    const stale = applyUpsert(answered.entities, [pending.id], pending, 'sse')
    const entity = (stale ?? answered).entities[pending.id]
    expect(entity.hitlResolved).toBe(true)
    expect(entity.hitlUserAnswer).toBe('London')
    expect(entity.hitlInteractionStatus).toBe('responded')
    expect(entity.hitlApplicationStatus).toBe('applied')
  })

  it('enforces the HITL version monotonicity matrix', () => {
    const pending = buildPendingHitlIncomingMessage({
      conversationId: 'room-1', messageId: 'message-1', requestId: 'request-1',
      source: 'agent', prompt: 'Round one?', promptType: 'text', choices: null,
      timestamp: '2026-08-17T00:00:00Z', agentId: null, agentName: 'Agent',
      agentSource: undefined, expiresAt: null, interactionId: 'interaction-1',
      interactionStatus: 'open', interactionVersion: 2, groupId: 'interaction-1',
      groupTotal: 1, groupIndex: 0, stepNumber: null, totalSteps: null,
      relatedMessageId: 'user-1', clientRequestId: 'client-1',
    })
    const terminal = applyUpsert({}, [], {
      ...pending,
      hitlResolved: true,
      hitlUserAnswer: 'saved answer',
      hitlInteractionStatus: 'responded',
      hitlApplicationStatus: 'applied',
    }, 'sse')!

    for (const version of [2, undefined, 1, 3]) {
      const attempted = applyUpsert(terminal.entities, [pending.id], {
        ...pending,
        hitlInteractionVersion: version,
        hitlResolved: false,
        hitlInteractionStatus: 'open',
        hitlApplicationStatus: undefined,
        hitlUserAnswer: undefined,
      }, 'sse')
      const entity = (attempted ?? terminal).entities[pending.id]
      expect(entity.hitlResolved).toBe(true)
      expect(entity.hitlUserAnswer).toBe('saved answer')
      expect(entity.hitlInteractionStatus).toBe('responded')
    }

    const sameIdentityRevision = applyUpsert(terminal.entities, [pending.id], {
      ...pending,
      hitlPrompt: 'Round two?',
      hitlInteractionVersion: 3,
      hitlResolved: false,
      hitlInteractionStatus: 'open',
      hitlApplicationStatus: undefined,
      hitlUserAnswer: undefined,
    }, 'sse')
    const revised = (sameIdentityRevision ?? terminal).entities[pending.id]
    expect(revised).toMatchObject({
      hitlResolved: true,
      hitlPrompt: 'Round one?',
      hitlInteractionVersion: 2,
      hitlInteractionStatus: 'responded',
      hitlUserAnswer: 'saved answer',
    })

    const newInteraction = buildPendingHitlIncomingMessage({
      conversationId: 'room-1', messageId: 'message-1', requestId: 'request-1',
      source: 'agent', prompt: 'New interaction?', promptType: 'text', choices: null,
      timestamp: '2026-08-17T00:00:01Z', agentId: null, agentName: 'Agent',
      agentSource: undefined, expiresAt: null, interactionId: 'interaction-2',
      interactionStatus: 'open', interactionVersion: 1, groupId: 'interaction-2',
      groupTotal: 1, groupIndex: 0, stepNumber: null, totalSteps: null,
      relatedMessageId: 'user-1', clientRequestId: 'client-1',
    })
    const opened = applyUpsert(terminal.entities, [pending.id], newInteraction, 'sse')!
    expect(opened.entities[newInteraction.id]).toMatchObject({
      hitlInteractionId: 'interaction-2',
      hitlResolved: false,
      hitlPrompt: 'New interaction?',
    })
  })

  it('updates source ownership on an existing entity', () => {
    const incoming = supervisorHitl()
    const initial = applyUpsert({}, [], {
      ...incoming,
      hitlSource: 'agent',
    }, 'sse')
    expect(initial).not.toBeNull()

    const updated = applyUpsert(
      initial!.entities,
      [incoming.id],
      incoming,
      'sse',
    )

    expect(updated?.entities[incoming.id].hitlSource).toBe('supervisor')
  })
})

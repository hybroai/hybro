import { describe, expect, it } from 'vitest'
import type { MessageEntity } from '@/stores/message-store/types'
import { buildTranscriptTurnSources } from './transcript'

function entity(overrides: Partial<MessageEntity> & Pick<MessageEntity, 'id' | 'messageType'>): MessageEntity {
  return {
    conversationId: 'room-1',
    content: 'text',
    senderName: overrides.messageType === 'user' ? 'You' : 'Agent',
    timestamp: '2030-01-01T00:00:00.000Z',
    source: 'db',
    sourceVersion: 1,
    displayType: overrides.messageType === 'user' ? 'user-bubble' : 'agent-bubble',
    isEphemeral: false,
    createdAt: 0,
    updatedAt: 0,
    ...overrides,
  }
}

function sources(list: MessageEntity[]) {
  const entities = Object.fromEntries(list.map((item) => [item.id, item]))
  return buildTranscriptTurnSources('room-1', entities, list.map((item) => item.id))
}

describe('buildTranscriptTurnSources', () => {
  it('routes replies by relatedMessageId, then clientRequestId', () => {
    expect(sources([
      entity({ id: 'u1', messageType: 'user', clientRequestId: 'c1' }),
      entity({ id: 'u2', messageType: 'user', clientRequestId: 'c2' }),
      entity({ id: 'a1', messageType: 'agent', relatedMessageId: 'u1' }),
      entity({ id: 'a2', messageType: 'agent', clientRequestId: 'c2' }),
    ])).toEqual([
      { userMessageId: 'u1', replyIds: ['a1'] },
      { userMessageId: 'u2', replyIds: ['a2'] },
    ])
  })

  it('places an uncorrelated reply under the nearest preceding user message', () => {
    expect(sources([
      entity({ id: 'u1', messageType: 'user' }),
      entity({ id: 'a1', messageType: 'agent' }),
      entity({ id: 'u2', messageType: 'user' }),
      entity({ id: 'a2', messageType: 'agent' }),
    ])).toEqual([
      { userMessageId: 'u1', replyIds: ['a1'] },
      { userMessageId: 'u2', replyIds: ['a2'] },
    ])
  })

  it('drops agent messages that precede every user message', () => {
    expect(sources([
      entity({ id: 'a0', messageType: 'agent' }),
      entity({ id: 'u1', messageType: 'user' }),
    ])).toEqual([{ userMessageId: 'u1', replyIds: [] }])
  })

  it('keeps an answered agent reply that carries HITL metadata', () => {
    expect(sources([
      entity({ id: 'u1', messageType: 'user' }),
      entity({
        id: 'a1', messageType: 'agent', relatedMessageId: 'u1', content: 'Final answer',
        taskStatus: 'completed' as MessageEntity['taskStatus'],
        hitlRequestId: 'request-1', hitlUserAnswer: 'Paris', hitlResolved: true,
      }),
    ])).toEqual([{ userMessageId: 'u1', replyIds: ['a1'] }])
  })

  it('leaves out HITL question rows and unanswered prompts', () => {
    expect(sources([
      entity({ id: 'u1', messageType: 'user' }),
      entity({
        id: 'a1:question', messageType: 'agent', relatedMessageId: 'u1',
        hitlMessageId: 'a1', hitlRequestId: 'request-1',
      }),
      entity({
        id: 'a2', messageType: 'agent', relatedMessageId: 'u1', content: 'Which city?',
        taskStatus: 'input-required' as MessageEntity['taskStatus'], hitlRequestId: 'request-2',
      }),
    ])).toEqual([{ userMessageId: 'u1', replyIds: [] }])
  })

  it('does not move replies of a user shell under an earlier message', () => {
    expect(sources([
      entity({ id: 'u1', messageType: 'user' }),
      entity({ id: 'u-shell', messageType: 'user', content: '' }),
      entity({ id: 'a1', messageType: 'agent', relatedMessageId: 'u-shell' }),
    ])).toEqual([{ userMessageId: 'u1', replyIds: [] }])
  })

  it('ignores other rooms and user shells without content', () => {
    expect(sources([
      entity({ id: 'u-other', messageType: 'user', conversationId: 'room-2' }),
      entity({ id: 'u-shell', messageType: 'user', content: '' }),
      entity({ id: 'u1', messageType: 'user' }),
    ])).toEqual([{ userMessageId: 'u1', replyIds: [] }])
  })
})

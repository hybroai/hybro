import { describe, it, expect } from 'vitest'
import { applyUpsert, isNoOpUpdate } from '@/stores/message-store/upsert'
import type { MessageEntity, IncomingMessage } from '@/stores/message-store/types'

function makeEntity(overrides: Partial<MessageEntity> = {}): MessageEntity {
  return {
    id: 'msg-1',
    conversationId: 'room-1',
    messageType: 'agent',
    content: 'Hello',
    senderName: 'Agent',
    timestamp: '2026-02-17T10:00:00Z',
    source: 'sse',
    sourceVersion: 1,
    displayType: 'agent-bubble',
    isEphemeral: false,
    createdAt: 1000,
    updatedAt: 1000,
    taskStatus: 'input-required',
    ...overrides,
  }
}

function makeIncoming(overrides: Partial<IncomingMessage> = {}): IncomingMessage {
  return {
    id: 'msg-1',
    conversationId: 'room-1',
    messageType: 'agent',
    content: 'Hello',
    senderName: 'Agent',
    timestamp: '2026-02-17T10:00:00Z',
    ...overrides,
  }
}

describe('upsert HITL fields', () => {
  describe('mergeIncoming — new entity', () => {
    it('includes all HITL fields on new entity insert', () => {
      const incoming = makeIncoming({
        hitlRequestId: 'req-1',
        hitlPrompt: 'Pick a date',
        hitlPromptType: 'text',
        hitlChoices: null,
        hitlExpiresAt: '2026-12-31T00:00:00Z',
        hitlResolved: false,
        taskStatus: 'input-required',
      })
      const result = applyUpsert({}, [], incoming, 'sse')
      expect(result).not.toBeNull()
      const entity = result!.entities['msg-1']
      expect(entity.hitlRequestId).toBe('req-1')
      expect(entity.hitlPrompt).toBe('Pick a date')
      expect(entity.hitlPromptType).toBe('text')
      expect(entity.hitlChoices).toBeNull()
      expect(entity.hitlExpiresAt).toBe('2026-12-31T00:00:00Z')
      expect(entity.hitlResolved).toBe(false)
    })
  })

  describe('mergeIncoming — existing entity update', () => {
    it('preserves HITL fields when incoming omits them', () => {
      const existing = makeEntity({
        hitlRequestId: 'req-1',
        hitlPrompt: 'Original prompt',
        hitlPromptType: 'text',
        hitlResolved: false,
      })
      const incoming = makeIncoming({
        content: 'Updated content',
        taskStatus: 'input-required',
      })
      const result = applyUpsert({ 'msg-1': existing }, ['msg-1'], incoming, 'sse')
      expect(result).not.toBeNull()
      const entity = result!.entities['msg-1']
      expect(entity.hitlRequestId).toBe('req-1')
      expect(entity.hitlPrompt).toBe('Original prompt')
      expect(entity.hitlPromptType).toBe('text')
      expect(entity.hitlResolved).toBe(false)
    })

    it('overwrites hitlResolved when incoming provides it', () => {
      const existing = makeEntity({
        hitlRequestId: 'req-1',
        hitlResolved: false,
      })
      const incoming = makeIncoming({
        hitlResolved: true,
        taskStatus: 'input-required',
      })
      const result = applyUpsert({ 'msg-1': existing }, ['msg-1'], incoming, 'sse')
      expect(result).not.toBeNull()
      expect(result!.entities['msg-1'].hitlResolved).toBe(true)
    })

    it('overwrites hitlChoices with new array when another visible field also changes', () => {
      const existing = makeEntity({
        hitlRequestId: 'req-1',
        hitlChoices: ['A', 'B'],
        hitlPrompt: 'Old prompt',
      })
      const incoming = makeIncoming({
        hitlChoices: ['X', 'Y', 'Z'],
        hitlPrompt: 'New prompt',
        taskStatus: 'input-required',
      })
      const result = applyUpsert({ 'msg-1': existing }, ['msg-1'], incoming, 'sse')
      expect(result).not.toBeNull()
      expect(result!.entities['msg-1'].hitlChoices).toEqual(['X', 'Y', 'Z'])
    })

    it('clears stale HITL group fields when incoming projection is ungrouped', () => {
      const existing = makeEntity({
        hitlRequestId: 'req-old',
        hitlPrompt: 'Old grouped prompt',
        hitlPromptType: 'choice',
        hitlChoices: ['A', 'B'],
        hitlGroupId: 'old-group',
        hitlGroupTotal: 2,
        hitlGroupIndex: 1,
      })
      const incoming = makeIncoming({
        hitlRequestId: 'req-new',
        hitlPrompt: 'New single prompt',
        hitlPromptType: 'text',
        hitlChoices: null,
        hitlGroupId: null,
        hitlGroupTotal: null,
        hitlGroupIndex: null,
        taskStatus: 'input-required',
      })

      const result = applyUpsert({ 'msg-1': existing }, ['msg-1'], incoming, 'sse')

      expect(result).not.toBeNull()
      const entity = result!.entities['msg-1']
      expect(entity.hitlRequestId).toBe('req-new')
      expect(entity.hitlPrompt).toBe('New single prompt')
      expect(entity.hitlPromptType).toBe('text')
      expect(entity.hitlChoices).toBeNull()
      expect(entity.hitlGroupId).toBeNull()
      expect(entity.hitlGroupTotal).toBeNull()
      expect(entity.hitlGroupIndex).toBeNull()
    })

    it('resets stale applying state and answer when a follow-up HITL reuses the same message id', () => {
      const existing = makeEntity({
        hitlRequestId: 'req-old',
        hitlPrompt: 'Where do you want to go?',
        hitlResolved: true,
        hitlInteractionStatus: 'responded',
        hitlApplicationStatus: 'applying',
        hitlUserAnswer: 'New York City',
      })
      const incoming = makeIncoming({
        hitlRequestId: 'req-new',
        hitlPrompt: 'How many days or nights do you plan to spend in New York City?',
        hitlResolved: false,
        hitlInteractionStatus: 'open',
        hitlApplicationStatus: 'open',
        hitlUserAnswer: '',
        taskStatus: 'input-required',
      })

      const result = applyUpsert({ 'msg-1': existing }, ['msg-1'], incoming, 'sse')

      expect(result).not.toBeNull()
      const entity = result!.entities['msg-1']
      expect(entity.hitlRequestId).toBe('req-new')
      expect(entity.hitlPrompt).toBe('How many days or nights do you plan to spend in New York City?')
      expect(entity.hitlResolved).toBe(false)
      expect(entity.hitlInteractionStatus).toBe('open')
      expect(entity.hitlApplicationStatus).toBe('open')
      expect(entity.hitlUserAnswer).toBe('')
    })

    it('finalizes stale applying HITL state when a terminal completion arrives without new HITL fields', () => {
      const existing = makeEntity({
        hitlRequestId: 'req-final',
        hitlPrompt: 'How many days or nights do you plan to spend in New York City?',
        hitlResolved: false,
        hitlInteractionStatus: 'responded',
        hitlApplicationStatus: 'applying',
        hitlUserAnswer: '5 days',
        taskStatus: 'input-required',
      })
      const incoming = makeIncoming({
        content: 'Great! Here is a detailed 5-day itinerary for your trip to New York City.',
        taskStatus: 'completed',
      })

      const result = applyUpsert({ 'msg-1': existing }, ['msg-1'], incoming, 'db')

      expect(result).not.toBeNull()
      const entity = result!.entities['msg-1']
      expect(entity.taskStatus).toBe('completed')
      expect(entity.hitlResolved).toBe(true)
      expect(entity.hitlInteractionStatus).toBe('responded')
      expect(entity.hitlApplicationStatus).toBe('applied')
      expect(entity.hitlUserAnswer).toBe('5 days')
    })
  })

  describe('isNoOpUpdate — HITL-aware', () => {
    it('detects hitlResolved change as NOT no-op', () => {
      const existing = makeEntity({
        hitlRequestId: 'req-1',
        hitlResolved: false,
      })
      const incoming = makeIncoming({
        hitlResolved: true,
        taskStatus: 'input-required',
      })
      expect(isNoOpUpdate(existing, incoming, 'sse')).toBe(false)
    })

    it('detects hitlPrompt change as NOT no-op', () => {
      const existing = makeEntity({
        hitlPrompt: 'Old prompt',
      })
      const incoming = makeIncoming({
        hitlPrompt: 'New prompt',
        taskStatus: 'input-required',
      })
      expect(isNoOpUpdate(existing, incoming, 'sse')).toBe(false)
    })

    it('detects hitlRequestId change as NOT no-op', () => {
      const existing = makeEntity({
        hitlRequestId: undefined,
      })
      const incoming = makeIncoming({
        hitlRequestId: 'req-new',
        taskStatus: 'input-required',
      })
      expect(isNoOpUpdate(existing, incoming, 'sse')).toBe(false)
    })

    it('returns true when HITL fields are unchanged', () => {
      const existing = makeEntity({
        hitlRequestId: 'req-1',
        hitlPrompt: 'Same prompt',
        hitlResolved: false,
      })
      const incoming = makeIncoming({
        hitlRequestId: 'req-1',
        hitlPrompt: 'Same prompt',
        hitlResolved: false,
        taskStatus: 'input-required',
      })
      expect(isNoOpUpdate(existing, incoming, 'sse')).toBe(true)
    })

    it('treats omitted HITL fields as no change', () => {
      const existing = makeEntity({
        hitlRequestId: 'req-1',
        hitlPrompt: 'Stable prompt',
        hitlResolved: false,
      })
      const incoming = makeIncoming({
        taskStatus: 'input-required',
      })
      expect(isNoOpUpdate(existing, incoming, 'sse')).toBe(true)
    })

    it('detects hitlPromptType change as NOT no-op', () => {
      const existing = makeEntity({
        hitlRequestId: 'req-1',
        hitlPromptType: 'text',
      })
      const incoming = makeIncoming({
        hitlPromptType: 'choice',
        taskStatus: 'input-required',
      })
      expect(isNoOpUpdate(existing, incoming, 'sse')).toBe(false)
    })

    it('detects hitlChoices change as NOT no-op', () => {
      const existing = makeEntity({
        hitlRequestId: 'req-1',
        hitlChoices: ['A', 'B'],
      })
      const incoming = makeIncoming({
        hitlChoices: ['X', 'Y', 'Z'],
        taskStatus: 'input-required',
      })
      expect(isNoOpUpdate(existing, incoming, 'sse')).toBe(false)
    })

    it('detects hitlExpiresAt change as NOT no-op', () => {
      const existing = makeEntity({
        hitlRequestId: 'req-1',
        hitlExpiresAt: '2026-06-01T00:00:00Z',
      })
      const incoming = makeIncoming({
        hitlExpiresAt: '2026-12-31T00:00:00Z',
        taskStatus: 'input-required',
      })
      expect(isNoOpUpdate(existing, incoming, 'sse')).toBe(false)
    })

    it('treats identical hitlChoices arrays as no-op', () => {
      const existing = makeEntity({
        hitlRequestId: 'req-1',
        hitlChoices: ['A', 'B'],
        hitlPromptType: 'choice',
      })
      const incoming = makeIncoming({
        hitlChoices: ['A', 'B'],
        taskStatus: 'input-required',
      })
      expect(isNoOpUpdate(existing, incoming, 'sse')).toBe(true)
    })
  })
})

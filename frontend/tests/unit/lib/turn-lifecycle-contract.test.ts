import { describe, expect, it } from 'vitest'

import {
  isCanonicalHITLRequestData,
  isCanonicalHITLResponseData,
  validateCanonicalRunEventData,
} from '@/lib/turn-lifecycle/contract'

function runStarted(mode: string) {
  return {
    event_id: 'event-1',
    run_id: 'run-1',
    seq: 1,
    type: 'run_started',
    correlation_id: 'cr-1',
    room_seq: 1,
    payload: {
      hybro_turn_id: 'run-1',
      user_message_id: 'msg-1',
      started_at: '2026-10-14T00:00:00Z',
      mode,
    },
  }
}

describe('run_started mode', () => {
  it.each(['fast', 'direct', 'ultimate', 'supervisor', 'passthrough'])(
    'accepts %s',
    (mode) => {
      const result = validateCanonicalRunEventData(runStarted(mode))
      expect(result).toMatchObject({ canonical: true, valid: true })
    },
  )

  it('rejects an unknown mode', () => {
    const result = validateCanonicalRunEventData(runStarted('autonomous'))
    expect(result).toMatchObject({ canonical: true, valid: false })
  })
})

describe('canonical HITL frames as the backend translator sends them', () => {
  const request = {
    request_id: 'agent_question',
    message_id: 'orchestrator:run-1:inv_1',
    prompt: 'Who should sign the emails?',
    prompt_type: 'text',
    source: 'agent',
    run_id: 'run-1',
    required: true,
    allow_other: false,
    agent_label: 'Outreach Email Writer',
    interaction_id: 'a2a-input-required:8cca88bb',
    question_count: 1,
    question_index: 0,
    related_user_message_id: 'user-1',
    client_request_id: 'client-1',
    trace_id: 'trace-1',
    room_seq: 18,
  }

  it('accepts presentation fields on hitl_request', () => {
    expect(isCanonicalHITLRequestData(request)).toBe(true)
    expect(isCanonicalHITLRequestData({
      ...request,
      header: 'Sender',
      choice_options: [{ value: 'a', label: 'A', description: 'First' }],
    })).toBe(true)
    expect(isCanonicalHITLRequestData({ ...request, required: 'yes' })).toBe(false)
    expect(isCanonicalHITLRequestData({ ...request, choice_options: [{ value: 'a', secret: 'x' }] })).toBe(false)
    expect(isCanonicalHITLRequestData({ ...request, agent_id: 'private' })).toBe(false)
  })

  it('accepts the sanitized answer text on hitl_response', () => {
    const response = {
      request_id: 'agent_question',
      message_id: 'orchestrator:run-1:inv_1',
      source: 'agent',
      status: 'responded',
      interaction_id: 'a2a-input-required:8cca88bb',
      question_count: 1,
      question_index: 0,
      run_id: 'run-1',
      answer_ref: 'answer_f704',
      answer_text: 'Morgan Reyes, CEO of LeaseGuard',
      related_user_message_id: 'user-1',
      client_request_id: 'client-1',
      trace_id: 'trace-2',
      room_seq: 24,
    }
    expect(isCanonicalHITLResponseData(response)).toBe(true)
    expect(isCanonicalHITLResponseData({ ...response, answer_text: 42 })).toBe(false)
    expect(isCanonicalHITLResponseData({ ...response, answered_by_client_id: 'client-7' })).toBe(true)
    expect(isCanonicalHITLResponseData({ ...response, answered_by_client_id: null })).toBe(true)
    expect(isCanonicalHITLResponseData({ ...response, answered_by_client_id: 42 })).toBe(false)
    expect(isCanonicalHITLResponseData({ ...response, answered_by: 'client', answered_by_client_id: 'client-7' })).toBe(true)
    expect(isCanonicalHITLResponseData({ ...response, answered_by: 'owner' })).toBe(true)
    expect(isCanonicalHITLResponseData({ ...response, answered_by: 'someone' })).toBe(false)
  })
})

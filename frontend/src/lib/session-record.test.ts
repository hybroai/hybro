import { describe, expect, it } from 'vitest'
import { foldSessionEvents, type RoomEventRecord } from './session-record'

function runEvent(seq: number, type: string, payload: Record<string, unknown>): RoomEventRecord {
  return { room_seq: seq, kind: 'run_event', payload_public: { run_id: 'run-1', type, payload } }
}

function settled(extra: Record<string, unknown>): RoomEventRecord {
  return runEvent(2, 'run_settled', {
    started_at: '2026-11-21T00:00:00Z',
    settled_at: '2026-11-21T00:00:01Z',
    duration_ms: 1000,
    ...extra,
  })
}

const started = runEvent(1, 'run_started', { user_message_id: 'msg-1', started_at: '2026-11-21T00:00:00Z' })

describe('foldSessionEvents settlement', () => {
  it('records a policy cancel as denied, naming the rule', () => {
    const [exchange] = foldSessionEvents([
      started,
      settled({ status: 'canceled', cancellation_code: 'policy', rule_id: 'rule_1' }),
    ])

    expect(exchange.status).toBe('denied')
    expect(exchange.steps).toEqual([{ kind: 'denied', at: '2026-11-21T00:00:01Z', ruleId: 'rule_1' }])
  })

  it('records a policy cancel without a rule id as denied', () => {
    const [exchange] = foldSessionEvents([started, settled({ status: 'canceled', cancellation_code: 'policy' })])

    expect(exchange.status).toBe('denied')
    expect(exchange.steps).toEqual([{ kind: 'denied', at: '2026-11-21T00:00:01Z' }])
  })

  it('records a call the owner refused at a check rule as rejected', () => {
    const [exchange] = foldSessionEvents([
      started,
      settled({ status: 'canceled', cancellation_code: 'owner_refused' }),
    ])

    expect(exchange.status).toBe('rejected')
    expect(exchange.steps).toEqual([{ kind: 'rejected', at: '2026-11-21T00:00:01Z' }])
  })

  it('keeps any other cancel as canceled', () => {
    const [exchange] = foldSessionEvents([
      started,
      settled({ status: 'canceled', cancellation_code: 'user_requested' }),
    ])

    expect(exchange.status).toBe('canceled')
    expect(exchange.steps).toEqual([{ kind: 'canceled', at: '2026-11-21T00:00:01Z' }])
  })
})

describe('foldSessionEvents owner rejection', () => {
  it('keeps a cancel after the owner rejected as rejected', () => {
    const rejected: RoomEventRecord = {
      room_seq: 2,
      kind: 'hitl_response',
      payload_public: { run_id: 'run-1', interaction_id: 'i-1', status: 'canceled' },
    }
    const [exchange] = foldSessionEvents([
      started,
      rejected,
      { ...settled({ status: 'canceled', cancellation_code: 'user_requested' }), room_seq: 3 },
    ])

    expect(exchange.status).toBe('rejected')
    expect(exchange.steps.map(step => step.kind)).toEqual(['rejected'])
  })
})

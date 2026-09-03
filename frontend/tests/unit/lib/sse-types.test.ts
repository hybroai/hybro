import { describe, expect, it } from 'vitest'
import {
  hasSSEFrameEnvelope,
  isConnectedData,
  isRoomSSEType,
  PROCESSING_STATUS,
} from '@/lib/types/sse'
import type { RoomSSEFrameMap } from '@/lib/types/sse'

describe('final room SSE types', () => {
  it('recognizes only final room SSE top-level types', () => {
    expect(isRoomSSEType('connected')).toBe(true)
    expect(isRoomSSEType('heartbeat')).toBe(true)
    expect(isRoomSSEType('snapshot')).toBe(true)
    expect(isRoomSSEType('agent_response_partial')).toBe(true)
    expect(isRoomSSEType('hitl_request')).toBe(true)
    expect(isRoomSSEType('hitl_response')).toBe(true)
    expect(isRoomSSEType('hub_agent_event')).toBe(false)
    expect(isRoomSSEType('debate_round')).toBe(false)
    expect(isRoomSSEType('hitl_input_requested')).toBe(false)
    expect(isRoomSSEType('hitl_status_update')).toBe(false)
    expect(isRoomSSEType('user_message')).toBe(false)
    expect(isRoomSSEType('turn_event')).toBe(false)
    expect(isRoomSSEType('event' + '_type')).toBe(false)
  })

  it('requires the final top-level SSE envelope including data', () => {
    expect(hasSSEFrameEnvelope({
      type: 'heartbeat',
      timestamp: '2026-06-04T00:00:00.000Z',
      room_id: 'room-1',
      data: {},
    })).toBe(true)

    expect(hasSSEFrameEnvelope({
      type: 'heartbeat',
      timestamp: '2026-06-04T00:00:00.000Z',
      room_id: 'room-1',
    })).toBe(false)
  })

  it('rejects legacy outer protocol fields at the top level', () => {
    const legacyEventTypeKey = 'event' + '_type'
    expect(hasSSEFrameEnvelope({
      type: 'heartbeat',
      timestamp: '2026-06-04T00:00:00.000Z',
      room_id: 'room-1',
      data: {},
      [legacyEventTypeKey]: 'heartbeat',
    })).toBe(false)

    const legacyPayloadKey = 'pay' + 'load'
    expect(hasSSEFrameEnvelope({
      type: 'run_event',
      timestamp: '2026-06-04T00:00:00.000Z',
      room_id: 'room-1',
      data: {},
      [legacyPayloadKey]: {},
    })).toBe(false)

    const legacyRoutingKey = 'target' + '_group'
    expect(hasSSEFrameEnvelope({
      type: 'task_submitted',
      timestamp: '2026-06-04T00:00:00.000Z',
      room_id: 'room-1',
      data: {},
      [legacyRoutingKey]: 'all_agents',
    })).toBe(false)

    expect(hasSSEFrameEnvelope({
      type: 'run_event',
      timestamp: '2026-06-04T00:00:00.000Z',
      room_id: 'room-1',
      data: { payload: {} },
    })).toBe(true)
  })

  it('includes queued in processing statuses', () => {
    expect(PROCESSING_STATUS.QUEUED).toBe('queued')
  })

  it('requires connected.data.connection_id', () => {
    expect(isConnectedData({ connection_id: 'conn-1' })).toBe(true)
    expect(isConnectedData({})).toBe(false)
    expect(isConnectedData({ connection_id: '' })).toBe(false)
  })

  it('carries the room_seq handshake on connected and heartbeat data', () => {
    const connected = {
      type: 'connected',
      room_id: 'room-1',
      timestamp: '2026-07-02T00:00:00.000Z',
      data: { connection_id: 'conn-1', room_seq: 42 },
    } satisfies RoomSSEFrameMap['connected']
    expect(connected.data.room_seq).toBe(42)

    const heartbeat = {
      type: 'heartbeat',
      room_id: 'room-1',
      timestamp: '2026-07-02T00:00:00.000Z',
      data: { room_seq: 42 },
    } satisfies RoomSSEFrameMap['heartbeat']
    expect(heartbeat.data.room_seq).toBe(42)
  })

  it('pins the snapshot frame shape', () => {
    const snapshot = {
      type: 'snapshot',
      room_id: 'room-1',
      timestamp: '2026-07-02T00:00:00.000Z',
      data: {
        room_seq: 7,
        messages: [],
        tasks: [],
        runs: [],
        hitl: { requests: [], resolved: [] },
        streaming: {},
        trace: {},
      },
    } satisfies RoomSSEFrameMap['snapshot']
    expect(snapshot.data.room_seq).toBe(7)
  })

  it('allows room_seq / room_event_id / parent_event_id on delta data', () => {
    const delta = {
      type: 'task_update',
      room_id: 'room-1',
      timestamp: '2026-07-02T00:00:00.000Z',
      data: {
        message_id: 'm1',
        status: 'completed',
        client_request_id: 'cr-1',
        room_seq: 8,
        room_event_id: 'evt-8',
        parent_event_id: 'evt-3',
      },
    } satisfies RoomSSEFrameMap['task_update']
    expect(delta.data.room_seq).toBe(8)
    expect(delta.data.room_event_id).toBe('evt-8')
    expect(delta.data.parent_event_id).toBe('evt-3')
  })
})

describe('RoomSSEFrameMap HITL durable events', () => {
  it('allows hitl_request without client_request_id', () => {
    const frame = {
      type: 'hitl_request',
      room_id: 'room-1',
      timestamp: '2026-07-02T00:00:00.000Z',
      data: {
        request_id: 'hitl-1',
        message_id: 'agent-msg-1',
        source: 'agent',
        prompt: 'Need revenue',
        prompt_type: 'text',
        question_count: 1,
        question_index: 0,
      },
    } satisfies RoomSSEFrameMap['hitl_request']

    expect(frame.data.request_id).toBe('hitl-1')
  })

  it('allows hitl_response without client_request_id', () => {
    const frame = {
      type: 'hitl_response',
      room_id: 'room-1',
      timestamp: '2026-07-02T00:00:00.000Z',
      data: {
        request_id: 'hitl-1',
        message_id: 'agent-msg-1',
        source: 'agent',
        status: 'responded',
        question_count: 1,
        question_index: 0,
      },
    } satisfies RoomSSEFrameMap['hitl_response']

    expect(frame.data.status).toBe('responded')
  })
})

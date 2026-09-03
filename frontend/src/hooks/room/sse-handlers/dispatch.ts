import type { AnySSEFrame, RoomSSEFrameMap, RoomSSEMessage, RoomSSEType } from '@/lib/types/sse'
import { isRoomSSEType } from '@/lib/types/sse'
import { clientRequestIdOf, TURN_CORRELATED_EVENT_TYPES } from './client-request'
import type { SSEHandlerDeps } from './types'
import { handleAgentResponse, handleAgentResponsePartial } from './handlers/agent-response'
import { handleProcessingStatus } from './handlers/processing-status'
import {
  handleCancellation,
  handleError,
  handleRunEvent,
} from './handlers/misc'
import { handleTaskSubmitted } from './handlers/task-submitted'
import { handleTaskUpdate } from './handlers/task-update'
import { handleArtifactUpdate } from './handlers/artifact-update'
import { handleHitlRequest, handleHitlResponse } from './handlers/hitl'
import { RoomReducer } from '@/lib/room-sync/room-reducer'
import {
  isCanonicalHITLRequestData,
  isCanonicalHITLResponseData,
  validateCanonicalRunEventData,
} from '@/lib/turn-lifecycle/contract'
import { isCanonicalRoot, useTurnStore } from '@/stores/turn-store'

export const HANDLED_ROOM_SSE_TYPES = {
  connected: true,
  heartbeat: true,
  snapshot: true,
  processing_status: true,
  run_event: true,
  task_submitted: true,
  task_update: true,
  artifact_update: true,
  agent_response: true,
  agent_response_partial: true,
  error: true,
  hitl_request: true,
  hitl_response: true,
  cancellation: true,
} satisfies Record<RoomSSEType, true>

/** Frames the reducer routes as deltas (ordering is reducer-owned). */
type DeltaMessage = Exclude<
  RoomSSEMessage,
  | RoomSSEFrameMap['connected']
  | RoomSSEFrameMap['heartbeat']
  | RoomSSEFrameMap['snapshot']
>

/**
 * Fold one delta frame through the live handler path. This is the single
 * fold path shared by live deltas, buffered pre-snapshot deltas, and reorder
 * window replay (Room Stream Snapshot plan P4). Ordering and buffering are
 * reducer-owned; here only the client_request_id extraction happens.
 */
function requestProtocolRecovery(deps: SSEHandlerDeps, reason: string): void {
  console.warn('[SSE] canonical Turn protocol violation:', reason)
  deps.requestSnapshotRef?.current?.()
}

function lifecycleMatchesRoot(
  deps: SSEHandlerDeps,
  userMessageId: string,
  clientRequestId: string,
): boolean {
  return deps.lifecycle.getMessageId() === userMessageId
    && deps.lifecycle.getClientRequestId() === clientRequestId
}

function foldCanonicalDelta(deps: SSEHandlerDeps, roomMessage: DeltaMessage): 'continue' | 'stop' {
  const store = useTurnStore.getState()
  let result: ReturnType<typeof store.applyEvent> | undefined

  if (roomMessage.type === 'run_event') {
    const validation = validateCanonicalRunEventData(roomMessage.data)
    if (!validation.canonical) return 'continue'
    if (!validation.valid) {
      requestProtocolRecovery(deps, validation.reason)
      return 'stop'
    }
    const existingTurn = store.rooms[deps.roomId]?.turns[validation.data.run_id]
    result = store.applyEvent(deps.roomId, { kind: 'run_event', data: validation.data })
    if (
      result.ok
      && validation.data.type === 'run_started'
      && !existingTurn
      && lifecycleMatchesRoot(
        deps,
        validation.data.payload.user_message_id,
        validation.data.correlation_id,
      )
    ) {
      deps.lifecycle.startProcessing(
        validation.data.payload.user_message_id,
        validation.data.correlation_id,
      )
    }
    if (
      result.ok
      && validation.data.type === 'run_settled'
      && existingTurn
      && !['completed', 'failed', 'canceled'].includes(existingTurn.state)
      && lifecycleMatchesRoot(
        deps,
        existingTurn.userMessageId,
        existingTurn.clientRequestId,
      )
    ) {
      deps.lifecycle.stopProcessing()
    }
  } else if (roomMessage.type === 'hitl_request' && roomMessage.data.run_id) {
    if (!isCanonicalHITLRequestData(roomMessage.data)) {
      requestProtocolRecovery(deps, 'Malformed canonical hitl_request')
      return 'stop'
    }
    result = store.applyEvent(deps.roomId, { kind: 'hitl_request', data: roomMessage.data })
  } else if (roomMessage.type === 'hitl_response' && roomMessage.data.run_id) {
    if (!isCanonicalHITLResponseData(roomMessage.data)) {
      requestProtocolRecovery(deps, 'Malformed canonical hitl_response')
      return 'stop'
    }
    result = store.applyEvent(deps.roomId, { kind: 'hitl_response', data: roomMessage.data })
  } else if (roomMessage.type === 'agent_response') {
    result = store.applyEvent(deps.roomId, { kind: 'agent_response', data: roomMessage.data })
  } else if (roomMessage.type === 'task_submitted' || roomMessage.type === 'task_update') {
    // Canonical Runs never emit task_* cards. Legacy-shaped frames may still
    // hydrate MessageStore, but the product renderer never consumes them as
    // Trace/Card lifecycle authority.
    if (roomMessage.data.run_id) {
      requestProtocolRecovery(deps, 'Deprecated canonical task card frame')
      return 'stop'
    }
    if (isCanonicalRoot(
      deps.roomId,
      roomMessage.data.client_request_id,
      roomMessage.data.related_message_id,
    )) {
      return 'stop'
    }
  } else if (roomMessage.type === 'processing_status'
    && isCanonicalRoot(deps.roomId, roomMessage.data.client_request_id, roomMessage.data.message_id)) {
    return 'stop'
  }

  if (result && !result.ok) {
    requestProtocolRecovery(deps, result.violation ?? 'Unknown canonical fold violation')
    return 'stop'
  }
  return 'continue'
}

async function foldDelta(deps: SSEHandlerDeps, roomMessage: DeltaMessage): Promise<void> {
  if (foldCanonicalDelta(deps, roomMessage) === 'stop') return
  const clientReqId = clientRequestIdOf(roomMessage)

  if (TURN_CORRELATED_EVENT_TYPES.has(roomMessage.type) && !clientReqId) {
    console.debug(
      'Dropping turn-correlated SSE event without client_request_id:',
      roomMessage.type,
    )
    return
  }

  switch (roomMessage.type) {
    case 'agent_response':
      await handleAgentResponse(
        deps,
        roomMessage,
        isCanonicalRoot(
          deps.roomId,
          roomMessage.data.client_request_id,
          roomMessage.data.related_message_id,
        ),
      )
      break
    case 'agent_response_partial':
      handleAgentResponsePartial(deps, roomMessage, clientReqId)
      break
    case 'processing_status':
      handleProcessingStatus(deps, roomMessage, clientReqId)
      break
    case 'error':
      handleError(deps, roomMessage)
      break
    case 'task_submitted':
      await handleTaskSubmitted(
        deps,
        roomMessage,
        clientReqId,
        false,
      )
      break
    case 'task_update':
      await handleTaskUpdate(
        deps,
        roomMessage,
        clientReqId,
        false,
      )
      break
    case 'artifact_update':
      handleArtifactUpdate({ roomId: deps.roomId, lifecycle: deps.lifecycle }, roomMessage, clientReqId)
      break
    case 'hitl_request':
      await handleHitlRequest(deps, roomMessage, clientReqId)
      break
    case 'hitl_response':
      handleHitlResponse(deps, roomMessage, clientReqId)
      break
    case 'run_event':
      handleRunEvent(deps, deps.lifecycle, roomMessage)
      break
    case 'cancellation':
      handleCancellation(deps, roomMessage)
      break
    default:
      roomMessage satisfies never
  }
}

export function createSSEDispatcher(deps: SSEHandlerDeps) {
  // The reducer owns ordering: snapshot replace + ordered delta patch with
  // gap self-heal. The handlers above remain the fold functions.
  const reducer = new RoomReducer({
    roomId: deps.roomId,
    onDelta: (frame: AnySSEFrame) => {
      return foldDelta(deps, frame as DeltaMessage)
    },
    hitlRequestIndex: deps.hitlRequestIndex.current,
    processingLifecycle: deps.lifecycle,
    requestSnapshot: () => {
      const request = deps.requestSnapshotRef?.current
      if (request) {
        request()
      } else {
        console.warn('[SSE] snapshot recovery requested but no reconnect surface is bound')
      }
    },
  })

  return async (sseMessage: AnySSEFrame) => {
    if (!isRoomSSEType(sseMessage.type)) {
      console.debug('Ignoring unknown room SSE frame type:', sseMessage.type, sseMessage)
      return
    }
    await reducer.handle(sseMessage)
  }
}

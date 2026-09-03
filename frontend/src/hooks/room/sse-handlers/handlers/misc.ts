import { banner } from '@/components/ui/banner'
import type { ErrorData, RoomSSEFrameMap } from '@/lib/types/sse'
import type { ProcessingLifecycle } from '../../processing-lifecycle'
import type { SSEHandlerDeps } from '../types'
import { ensureTurnTerminalStampedFromBackendTruth } from '@/lib/room-timeline/turn-terminal-stamp'
import { useTraceStore } from '@/stores/trace-store'

function isErrorDataObject(data: unknown): data is ErrorData {
  return Boolean(data && typeof data === 'object' && !Array.isArray(data))
}

function isTurnScopedError(data: ErrorData): boolean {
  return 'client_request_id' in data || 'message_id' in data || 'agent_id' in data
}

export function handleError(_ctx: SSEHandlerDeps, sseMessage: RoomSSEFrameMap['error']): void {
  console.error('❌ SSE error message:', sseMessage.data)
  const errorData = isErrorDataObject(sseMessage.data) ? sseMessage.data : undefined
  if (!errorData) {
    console.debug('Ignoring malformed error SSE data:', sseMessage.data)
    banner.error('Unknown error')
    return
  }

  if (isTurnScopedError(errorData) && !errorData.client_request_id) {
    console.debug('Ignoring turn-scoped error without client_request_id:', errorData)
    return
  }

  if (errorData?.error_type === 'rate_limit_exceeded') {
    const retryAfter = errorData.retry_after_seconds
    const retryMinutes = retryAfter ? Math.ceil(retryAfter / 60) : 60
    const quotaDetails = [
      retryAfter ? `Retry after ${retryMinutes} minutes.` : undefined,
      typeof errorData.user_requests_used === 'number' && typeof errorData.user_requests_limit === 'number'
        ? `User requests: ${errorData.user_requests_used}/${errorData.user_requests_limit}.`
        : undefined,
      typeof errorData.system_requests_used === 'number' && typeof errorData.system_requests_limit === 'number'
        ? `System requests: ${errorData.system_requests_used}/${errorData.system_requests_limit}.`
        : undefined,
    ].filter(Boolean).join(' ')
    banner.error(
      errorData.error || `Rate limit exceeded. Please try again in ${retryMinutes} minutes.`,
      { duration: 15000, description: quotaDetails || undefined },
    )
  } else {
    banner.error(errorData?.error || 'Unknown error')
  }
}

export function handleConnected(sseMessage: RoomSSEFrameMap['connected']): void {
  console.debug('Room SSE connected:', sseMessage.data.connection_id)
}

export function handleHeartbeat(): void {
}

export function handleCancellation(
  _ctx: SSEHandlerDeps,
  sseMessage: RoomSSEFrameMap['cancellation'],
): void {
  console.debug('Room SSE cancellation event:', sseMessage.data)
}

export function handleRunEvent(
  ctx: SSEHandlerDeps,
  lifecycle: ProcessingLifecycle,
  sseMessage: RoomSSEFrameMap['run_event'],
): void {
  const correlationId = sseMessage.data?.correlation_id
  if (typeof correlationId === 'string' && correlationId.length > 0) {
    const pendingAckClientRequestId = lifecycle.getPendingRunEventAck()
    if (pendingAckClientRequestId && pendingAckClientRequestId === correlationId) {
      lifecycle.clearPendingRunEventAck()
    }
  }

  const sub = sseMessage.data?.type as string | undefined

  // Decision-visibility projection (Phase 1): fold public trace kinds into
  // the Turn Trace store. Non-trace run_event sub-types are ignored here.
  if (sub && sseMessage.data?.run_id) {
    useTraceStore.getState().applyRunEvent({
      eventId: sseMessage.data.event_id,
      runId: sseMessage.data.run_id,
      type: sub,
      payload: (sseMessage.data.payload ?? {}) as Record<string, unknown>,
      correlationId: typeof correlationId === 'string' && correlationId.length > 0
        ? correlationId
        : null,
    })
  }

  if (sub === 'run_failed' || sub === 'run_completed' || sub === 'run_canceled') {
    const runId = sseMessage.data?.run_id
    if (runId) {
      useTraceStore.getState().setRunStatus(
        runId,
        sub === 'run_completed' ? 'completed' : sub === 'run_canceled' ? 'canceled' : 'failed',
      )
      void ensureTurnTerminalStampedFromBackendTruth(
        ctx.roomId,
        lifecycle,
        { relatedMessageId: runId, clientRequestId: correlationId },
        ctx.getToken,
      )
    }
    void ctx.reconcileWithDb(ctx.roomId)
  }
}

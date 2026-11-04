import { toast } from 'sonner'
import type { ErrorData, RoomSSEFrameMap } from '@/lib/types/sse'
import type { ProcessingLifecycle } from '../../processing-lifecycle'
import type { SSEHandlerDeps } from '../types'
import { log, logToken } from '@/lib/log'

function isErrorDataObject(data: unknown): data is ErrorData {
  return Boolean(data && typeof data === 'object' && !Array.isArray(data))
}

function isTurnScopedError(data: ErrorData): boolean {
  return 'client_request_id' in data || 'message_id' in data || 'agent_id' in data
}

export function handleError(_ctx: SSEHandlerDeps, sseMessage: RoomSSEFrameMap['error']): void {
  const errorData = isErrorDataObject(sseMessage.data) ? sseMessage.data : undefined
  log.error('sse_error_frame', {
    conversation_id: sseMessage.conversation_id,
    error_type: logToken(errorData?.error_type),
    client_request_id: errorData?.client_request_id,
  })
  if (!errorData) {
    log.debug('sse_frame_ignored', {
      conversation_id: sseMessage.conversation_id,
      type: sseMessage.type,
      reason: 'malformed_data',
    })
    toast.error('Unknown error')
    return
  }

  if (isTurnScopedError(errorData) && !errorData.client_request_id) {
    log.debug('sse_frame_ignored', {
      conversation_id: sseMessage.conversation_id,
      type: sseMessage.type,
      reason: 'missing_client_request_id',
    })
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
    toast.error(
      errorData.error || `Rate limit exceeded. Please try again in ${retryMinutes} minutes.`,
      { duration: 15000, description: quotaDetails || undefined },
    )
  } else {
    toast.error(errorData?.error || 'Unknown error')
  }
}

export function handleConnected(sseMessage: RoomSSEFrameMap['connected']): void {
  log.debug('sse_room_connected', { conversation_id: sseMessage.conversation_id, connection_id: sseMessage.data.connection_id })
}

export function handleHeartbeat(): void {
}

export function handleCancellation(
  _ctx: SSEHandlerDeps,
  sseMessage: RoomSSEFrameMap['cancellation'],
): void {
  log.debug('sse_cancellation_received', { conversation_id: sseMessage.conversation_id })
}

export function handleRunEvent(
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
}

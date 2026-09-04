import { banner } from '@/components/ui/banner'
import type { ProcessingStatus, ProcessingStatusData, RoomSSEFrameMap } from '@/lib/types/sse'
import { PROCESSING_STATUS, isProcessingDone, TASK_STATE } from '@/lib/types/sse'
import { useMessageStore } from '@/stores/message-store'
import type { MessageEntity } from '@/stores/message-store/types'
import { useRoomUiStore } from '@/stores/room-ui-store'
import {
  appendProcessingStatusLog,
  findProcessingStatusUserEntity,
  parseTurnPhaseFromDetails,
  processingDetailsToLogMessage,
} from '../../processing-status-log'
import { resolveUserMessageId } from '../client-request'
import { applyRoomCommands } from '../apply-commands'
import type { SSEHandlerDeps } from '../types'

// ── Turn-level gating (post-heuristic, Room Stream Snapshot plan §8) ───────
// Terminal frames are durable-confirmed (§4 rule 4), so the old id-matching
// chains are gone. A status belongs to the current live turn when its client
// request matches the lifecycle's pending ack or when it resolves to the
// lifecycle's own message. Everything else is stamped on its resolved entity
// without touching the live lifecycle.

function belongsToCurrentTurn(
  lifecycle: SSEHandlerDeps['lifecycle'],
  clientReqId: string | null,
  resolvedEntityId: string | undefined,
): boolean {
  const pendingAck = lifecycle.getPendingRunEventAck()
  if (pendingAck && clientReqId && pendingAck === clientReqId) return true
  const lifecycleMessageId = lifecycle.getMessageId()
  if (lifecycleMessageId && resolvedEntityId && lifecycleMessageId === resolvedEntityId) {
    return true
  }
  return !lifecycleMessageId
}

/**
 * Turn-level terminal gate: terminal processing_status frames are
 * durable-confirmed turn facts. Per-agent terminal statuses (message_id
 * pointing at a child agent task with a related user message) are NOT turn
 * facts and are dropped here.
 */
function isTurnLevelTerminal(
  sseMessageId: string | undefined,
  lifecycleMessageId: string | null,
  terminalUser: MessageEntity | undefined,
  relatedMessageId: string | null | undefined,
  clientReqId: string | null,
): boolean {
  if (!sseMessageId) return true
  if (lifecycleMessageId && sseMessageId === lifecycleMessageId) return true
  if (terminalUser?.id === sseMessageId) return true
  if (terminalUser && clientReqId && terminalUser.clientRequestId === clientReqId) {
    // Per-agent terminal: the frame addresses a child task while pointing
    // back at the turn user message via related_message_id.
    if (relatedMessageId && relatedMessageId === terminalUser.id) return false
    return true
  }
  return false
}

const PROCESSING_STATUS_VALUES = new Set<string>(Object.values(PROCESSING_STATUS))

function hasValidProcessingDetails(
  details: unknown,
): details is Record<string, unknown> | null {
  return details === null || (typeof details === 'object' && !Array.isArray(details))
}

function isProcessingStatusData(data: unknown): data is ProcessingStatusData {
  if (!data || typeof data !== 'object') return false
  const value = data as Record<string, unknown>
  if (!Object.prototype.hasOwnProperty.call(value, 'message_id')) return false
  if (typeof value.message_id !== 'string' || value.message_id.length === 0) return false
  if (typeof value.client_request_id !== 'string' || value.client_request_id.length === 0) return false
  if (typeof value.status !== 'string' || !PROCESSING_STATUS_VALUES.has(value.status)) return false
  if (!Object.prototype.hasOwnProperty.call(value, 'details')) return false
  return hasValidProcessingDetails(value.details)
}

export function handleProcessingStatus(
  ctx: SSEHandlerDeps,
  sseMessage: RoomSSEFrameMap['processing_status'],
  clientReqId: string | null,
): void {
  if (!isProcessingStatusData(sseMessage.data)) {
    const details = sseMessage.data && typeof sseMessage.data === 'object'
      ? (sseMessage.data as Record<string, unknown>).details
      : undefined
    const message = details === undefined
      ? 'Ignoring processing_status without required object/null details:'
      : 'Ignoring invalid processing_status data:'
    console.debug(message, details ?? sseMessage.data)
    return
  }

  const status = sseMessage.data.status
  const store = useMessageStore.getState()
  const { roomId, lifecycle } = ctx

  if (
    status === PROCESSING_STATUS.QUEUED
    || status === PROCESSING_STATUS.PROCESSING
    || status === PROCESSING_STATUS.AWAITING_INPUT
  ) {
    // A room-level terminal event can arrive before DB hydration has produced
    // the user entity. In that race there is no entity terminal stamp yet, so
    // the lifecycle itself is the authoritative absorbing-state guard.
    if (lifecycle.isProcessingResolved()) {
      return
    }

    const pendingAckClientRequestId = lifecycle.getPendingRunEventAck()
    if (
      pendingAckClientRequestId
      && clientReqId
      && clientReqId !== pendingAckClientRequestId
    ) {
      return
    }

    const lifecycleMessageId = lifecycle.getMessageId()
    const relatedMessageId = (sseMessage.data as { related_message_id?: string | null }).related_message_id ?? undefined

    const resolvedClientMessageId = resolveUserMessageId(roomId, clientReqId)
    const userMsgId =
      resolvedClientMessageId ??
      relatedMessageId ??
      (sseMessage.data.message_id as string | undefined) ??
      lifecycleMessageId
    const processingUserEntity = findProcessingStatusUserEntity(roomId, {
      messageId: userMsgId,
      clientRequestId: clientReqId,
      relatedMessageId,
      preferClientRequestId: true,
    })
    if (processingUserEntity?.turnTerminalStatus) {
      if (
        processingUserEntity.turnTerminalStatus === 'failed'
        || processingUserEntity.turnTerminalStatus === 'canceled'
      ) {
        return
      }
      const stageMessage = processingDetailsToLogMessage(sseMessage.data.details)?.toLowerCase() ?? ''
      const isOrchestrationStage =
        stageMessage.includes('synthesiz')
        || stageMessage.includes('evaluat')
        || stageMessage.includes('planning')
        || stageMessage.includes('delegat')
      const holdForSynthesis =
        processingUserEntity.turnCompletionKind === 'synthesis'
        || isOrchestrationStage
      if (!holdForSynthesis) {
        return
      }
    }

    if (!belongsToCurrentTurn(lifecycle, clientReqId, processingUserEntity?.id)) {
      return
    }

    // Durable cancellation owns the current turn. Ignore late forward-progress
    // updates until a terminal processing status settles it.
    if (useRoomUiStore.getState().getRoomFlags(roomId).cancelling) {
      return
    }

    // A non-terminal update must be attributable to a resolvable turn: the
    // user entity, the lifecycle message, or the pending ack. Unattributable
    // updates are ignored (previously they sat in the correlation buffer).
    if (!processingUserEntity && !lifecycleMessageId && !pendingAckClientRequestId) {
      return
    }

    appendProcessingStatusLog(
      roomId,
      processingUserEntity,
      processingDetailsToLogMessage(sseMessage.data.details),
      sseMessage.timestamp,
      'sse',
      { turnPhase: parseTurnPhaseFromDetails(sseMessage.data.details) },
    )

    lifecycle.startProcessing(processingUserEntity?.id ?? lifecycleMessageId ?? userMsgId)
    return
  }

  if (!isProcessingDone(status as ProcessingStatus) && status !== PROCESSING_STATUS.RATE_LIMITED) {
    return
  }

  const lifecycleMessageId = lifecycle.getMessageId()
  const relatedMessageId = (sseMessage.data as { related_message_id?: string | null }).related_message_id ?? undefined
  const resolvedClientMessageId = resolveUserMessageId(roomId, clientReqId)
  const terminalUserMsgId = resolvedClientMessageId ?? relatedMessageId ?? sseMessage.data.message_id ?? lifecycleMessageId
  const terminalUser = findProcessingStatusUserEntity(roomId, {
    messageId: terminalUserMsgId,
    clientRequestId: clientReqId,
    relatedMessageId,
    preferClientRequestId: true,
  })

  const resolvedTerminalUserMsgId = terminalUser?.id ?? terminalUserMsgId

  // Per-agent terminal statuses are not turn facts: drop them before any
  // lifecycle resolution or entity stamping.
  if (
    (lifecycleMessageId || resolvedClientMessageId || terminalUser)
    && !isTurnLevelTerminal(
      sseMessage.data.message_id as string | undefined,
      lifecycleMessageId,
      terminalUser,
      relatedMessageId,
      clientReqId,
    )
  ) {
    return
  }

  const isCurrentLifecycleTerminal = belongsToCurrentTurn(
    lifecycle,
    clientReqId,
    terminalUser?.id,
  )

  if (isCurrentLifecycleTerminal) {
    lifecycle.markProcessingResolved()
    lifecycle.stopProcessing({ clearMessageId: false })
    ctx.setCancelling(false)
    lifecycle.disarmCancelTimeout()
    store.removeMessage(lifecycle.placeholderId(roomId))
    lifecycle.dismissPlaceholder()

    const turnClientRequestId = clientReqId ?? sseMessage.data.client_request_id
    if (turnClientRequestId) {
      applyRoomCommands([
        { type: 'stream_clear_client_request', clientRequestId: turnClientRequestId },
      ])
    }

    if (sseMessage.data.message_id === lifecycleMessageId) {
      lifecycle.setMessageId(null)
    }
    if (!lifecycle.hasCancelTimedOut()) {
      if (status === PROCESSING_STATUS.CANCELED) {
        banner.info('Processing stopped by user')
        store.upsertMessage({
          id: `cancel-confirm-${Date.now()}`,
          roomId,
          messageType: 'agent',
          content: 'Processing was stopped by the user.',
          senderName: 'System',
          taskStatus: TASK_STATE.CANCELED,
          taskContent: 'Processing stopped by user',
          timestamp: new Date().toISOString(),
          isEphemeral: true,
        }, 'optimistic')
        store.cancelAllNonTerminal(roomId)
      } else if (status === PROCESSING_STATUS.FAILED) {
        banner.error(`Processing failed: ${processingDetailsToLogMessage(sseMessage.data.details) ?? 'Unknown error'}`)
      } else if (status === PROCESSING_STATUS.RATE_LIMITED) {
        // rate limit terminal — banner handled elsewhere if needed
      }
    }
    lifecycle.setCancelTimedOut(false)
  }

  if (resolvedTerminalUserMsgId) {
    const existingUserMsg = store.entities[resolvedTerminalUserMsgId]
    if (existingUserMsg) {
      const terminalStatus =
        status === PROCESSING_STATUS.CANCELED ? 'canceled' :
        status === PROCESSING_STATUS.FAILED ||
        status === PROCESSING_STATUS.ERROR ||
        status === PROCESSING_STATUS.REJECTED ||
        status === PROCESSING_STATUS.RATE_LIMITED ? 'failed' : 'completed'
      const rawKind = sseMessage.data.details?.turn_completion_kind
      const incomingKind: 'synthesis' | 'deterministic' | undefined =
        rawKind === 'synthesis' || rawKind === 'deterministic' ? rawKind : undefined

      if (!existingUserMsg.turnTerminalStatus || (incomingKind && !existingUserMsg.turnCompletionKind)) {
        store.upsertMessage({
          id: resolvedTerminalUserMsgId,
          roomId,
          messageType: existingUserMsg.messageType,
          content: existingUserMsg.content,
          senderName: existingUserMsg.senderName,
          timestamp: existingUserMsg.timestamp,
          turnTerminalStatus: existingUserMsg.turnTerminalStatus || terminalStatus,
          turnCompletionKind: existingUserMsg.turnCompletionKind || incomingKind,
        }, 'sse')
      } else if (
        existingUserMsg.turnCompletionKind === 'deterministic'
        && incomingKind === 'synthesis'
        && terminalStatus === 'completed'
      ) {
        store.upsertMessage({
          id: resolvedTerminalUserMsgId,
          roomId,
          messageType: existingUserMsg.messageType,
          content: existingUserMsg.content,
          senderName: existingUserMsg.senderName,
          timestamp: existingUserMsg.timestamp,
          turnCompletionKind: 'synthesis',
        }, 'sse')
      }
    }
  }

  // Terminal frames are durable-confirmed (§4 rule 4): no fixed-delay
  // reconciliation is scheduled. Gap recovery is the reducer's snapshot
  // re-request — the only self-heal path.
}

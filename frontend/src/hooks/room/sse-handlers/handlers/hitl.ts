import { parseHitlPresentation } from '@/lib/hitl/presentation'
import type { RoomSSEFrameMap, TaskState } from '@/lib/types/sse'
import {
  buildPendingHitlIncomingMessage,
  hitlQuestionEntityId,
  hitlRequestKey,
} from '@/lib/hitl/hitl-message-projection'
import { useMessageStore } from '@/stores/message-store'
import { normalizeTimestampOrNow } from '@/lib/time'
import { findProcessingStatusUserEntity } from '../../processing-status-log'
import type { SSEHandlerDeps } from '../types'

export async function handleHitlRequest(
  ctx: SSEHandlerDeps,
  sseMessage: RoomSSEFrameMap['hitl_request'],
  _clientReqId: string | null,
): Promise<void> {
  const {
    request_id, message_id, source, prompt, prompt_type, choices,
    agent_name, agent_id, step_number, total_steps, expires_at,
    interaction_id, interaction_status, interaction_version, application_status,
    question_count, question_index, related_message_id, related_user_message_id,
    agent_label,
  } = sseMessage.data

  if (!request_id || !message_id) return

  const store = useMessageStore.getState()
  const { lifecycle, hitlRequestIndex, conversationId } = ctx

  const eventClientRequestId =
    typeof sseMessage.data.client_request_id === 'string' &&
    sseMessage.data.client_request_id.length > 0
      ? sseMessage.data.client_request_id
      : undefined
  const hasEventTurnIdentity = !!eventClientRequestId || !!related_user_message_id || !!related_message_id
  const processingUser =
    findProcessingStatusUserEntity(conversationId, {
      clientRequestId: eventClientRequestId,
      relatedMessageId: related_user_message_id ?? related_message_id,
    }) ??
    (!hasEventTurnIdentity
      ? findProcessingStatusUserEntity(conversationId, {
          messageId: lifecycle.getMessageId(),
          latestUser: true,
        })
      : undefined)
  const activeClientRequestId = lifecycle.getPendingRunEventAck()
  const lifecycleMessageId = lifecycle.getMessageId()
  const eventMatchesActiveAck =
    !!activeClientRequestId &&
    !!eventClientRequestId &&
    activeClientRequestId === eventClientRequestId
  const userMatchesActiveAck =
    !!activeClientRequestId &&
    !!processingUser?.clientRequestId &&
    activeClientRequestId === processingUser.clientRequestId
  const userMatchesLifecycle =
    !!lifecycleMessageId &&
    processingUser?.id === lifecycleMessageId
  const isCurrentTurnHitl =
    eventMatchesActiveAck ||
    userMatchesActiveAck ||
    userMatchesLifecycle ||
    (!activeClientRequestId && !lifecycleMessageId && !!processingUser)
  if (isCurrentTurnHitl) {
    store.removeMessage(lifecycle.placeholderId(conversationId))
    lifecycle.dismissPlaceholder()
    lifecycle.markProcessingResolved()
    lifecycle.stopProcessing({ clearMessageId: false })
  }

  let resolvedAgentName = agent_label ?? agent_name
  if (!resolvedAgentName && agent_id) {
    resolvedAgentName = await ctx.getAgentName(agent_id)
  }

  const incoming = buildPendingHitlIncomingMessage({
    conversationId,
    messageId: message_id,
    requestId: request_id,
    source,
    prompt,
    promptType: prompt_type,
    choices: Array.isArray(choices) ? choices as string[] : null,
    presentation: parseHitlPresentation(sseMessage.data),
    timestamp: sseMessage.timestamp,
    agentId: agent_id,
    agentName: resolvedAgentName,
    agentSource: ctx.getAgentSource(agent_id ?? undefined),
    expiresAt: expires_at,
    interactionId: interaction_id,
    interactionStatus: interaction_status,
    interactionVersion: interaction_version,
    applicationStatus: application_status,
    groupId: interaction_id,
    groupTotal: question_count,
    groupIndex: question_index,
    stepNumber: step_number,
    totalSteps: total_steps,
    relatedMessageId: related_user_message_id ?? related_message_id,
    clientRequestId: sseMessage.data.client_request_id,
  })
  const legacyProjection = store.entities[message_id]
  if (
    legacyProjection
    && legacyProjection.id !== incoming.id
    && legacyProjection.hitlRequestId === request_id
    && legacyProjection.hitlResolved !== true
  ) {
    store.upsertMessage({
      id: legacyProjection.id,
      conversationId,
      messageType: 'agent',
      content: legacyProjection.content,
      senderName: legacyProjection.senderName,
      timestamp: legacyProjection.timestamp,
      hitlResolved: true,
    }, 'sse')
  }
  store.upsertMessage(incoming, 'sse')
  const requestKey = hitlRequestKey(interaction_id, request_id)
  hitlRequestIndex.current.set(requestKey, incoming.id)
}

export function handleHitlResponse(
  ctx: SSEHandlerDeps,
  sseMessage: RoomSSEFrameMap['hitl_response'],
  _clientReqId: string | null,
): void {
  const {
    request_id,
    status: hitlStatus,
    error_message,
    interaction_id,
    interaction_status,
    interaction_version,
    application_status,
    client_request_id,
  } = sseMessage.data
  if (!request_id) return

  const store = useMessageStore.getState()
  const requestKey = hitlRequestKey(interaction_id, request_id)
  const indexedEntityId = ctx.hitlRequestIndex.current.get(requestKey)
    ?? ctx.hitlRequestIndex.current.get(request_id)
  const fallbackMessageId = sseMessage.data.message_id
  const projectedEntityId = fallbackMessageId
    ? hitlQuestionEntityId(
        fallbackMessageId,
        interaction_id,
        request_id,
        sseMessage.data.question_count,
      )
    : undefined
  const entityId = indexedEntityId
    ?? (projectedEntityId && store.entities[projectedEntityId]
      ? projectedEntityId
      : fallbackMessageId)
  const entity = entityId ? store.entities[entityId] : undefined
  if (!entity) return

  if (entity.hitlRequestId && entity.hitlRequestId !== request_id) {
    if (indexedEntityId) {
      ctx.hitlRequestIndex.current.delete(requestKey)
      ctx.hitlRequestIndex.current.delete(request_id)
    }
    return
  }

  if (!indexedEntityId) {
    ctx.hitlRequestIndex.current.set(requestKey, entity.id)
  }

  let resolvedTaskStatus = entity.taskStatus
  let resolvedTaskError: string | null = entity.taskError ?? null
  let resolvedContent = entity.content
  const resolved = ['responded', 'resolved', 'expired', 'canceled', 'error'].includes(hitlStatus)
  if (hitlStatus === 'expired') {
    resolvedTaskStatus = 'failed' as TaskState
    resolvedTaskError = error_message || 'Request expired'
    resolvedContent = error_message || entity.content
  } else if (hitlStatus === 'canceled') {
    resolvedTaskStatus = 'canceled' as TaskState
    resolvedTaskError = error_message || 'Request canceled'
    resolvedContent = error_message || entity.content
  } else if (hitlStatus === 'error') {
    resolvedTaskStatus = 'failed' as TaskState
    resolvedTaskError = error_message || 'Hybro could not route this response'
    resolvedContent = error_message || entity.content
  } else if (hitlStatus === 'responded' || hitlStatus === 'resolved') {
    resolvedTaskError = null
  }

  store.upsertMessage({
    id: entity.id,
    conversationId: ctx.conversationId,
    messageType: 'agent',
    content: resolvedContent,
    senderName: entity.senderName,
    timestamp: normalizeTimestampOrNow(sseMessage.timestamp),
    hitlResolved: resolved,
    hitlInteractionId: interaction_id ?? entity.hitlInteractionId,
    hitlInteractionStatus: interaction_status
      ?? (hitlStatus === 'error' ? 'error' : entity.hitlInteractionStatus ?? hitlStatus),
    hitlInteractionVersion: interaction_version ?? entity.hitlInteractionVersion,
    hitlApplicationStatus: application_status
      ?? (hitlStatus === 'error' ? 'error' : entity.hitlApplicationStatus ?? hitlStatus),
    clientRequestId: client_request_id ?? entity.clientRequestId,
    taskStatus: resolvedTaskStatus,
    taskError: resolvedTaskError,
  }, 'sse')

  if (resolved) {
    ctx.hitlRequestIndex.current.delete(requestKey)
    ctx.hitlRequestIndex.current.delete(request_id)
  }
}

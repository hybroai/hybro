import { useCallback } from 'react'
import type { MutableRefObject } from 'react'
import { cancelMessage } from '@/lib/api/sse'
import { ApiError } from '@/lib/api-client'
import { toast } from 'sonner'
import { useRoomUiStore } from '@/stores/room-ui-store'
import { useMessageStore } from '@/stores/message-store'
import { useTurnStore } from '@/stores/turn-store'
import { selectPendingHitls } from '@/lib/selectors/select-hitl'
import type { ProcessingLifecycle } from './processing-lifecycle'
import { findProcessingStatusUserEntity } from './processing-status-log'
import { hydrateRoomFromDb } from '@/lib/room-sync/hydrate-room'
import { hitlRequestKey } from '@/lib/hitl/hitl-message-projection'
import { acquireHitlSubmissionFence } from './hitl-submission-fence'
import { log } from '@/lib/log'

export function useRoomActions(
  conversationId: string,
  getToken: (() => Promise<string | null>) | undefined,
  lifecycle: ProcessingLifecycle,
  hitlRequestIndex: MutableRefObject<Map<string, string>>,
  reconcileWithDb: (conversationId: string) => Promise<void>,
  setCancelling: (v: boolean) => void,
  sseEnabled: boolean,
  setSseEnabled: (v: boolean) => void,
  getAgentName?: (agentId: string) => Promise<string>,
  getAgentSource?: (agentId: string | undefined) => 'cloud' | 'local' | 'hub' | undefined,
  requestCanonicalSnapshot?: () => void,
) {
  // Cancel ongoing message processing
  const cancelProcessing = useCallback(async () => {
    const messageId = lifecycle.getMessageId()
    if (!messageId) {
      toast.warning('Unable to cancel — no active task found')
      return false
    }

    try {
      setCancelling(true)
      lifecycle.setCancelTimedOut(false)
      const cancellation = await cancelMessage(messageId, getToken)

      if (
        cancellation.outcome === 'pending_reconciliation'
        || cancellation.outcome === 'canceled'
      ) {
        lifecycle.armCancelTimeout(() => {
          const cancelling = useRoomUiStore.getState().getRoomFlags(conversationId).cancelling
          if (cancelling) {
            lifecycle.setCancelTimedOut(true)
            toast.warning('Cancellation is taking longer than expected — the agent may still be stopping')
          }
        })
        try {
          await reconcileWithDb(conversationId)
        } catch (reconcileError) {
          log.error('cancel_reconcile_failed', { conversation_id: conversationId, error: reconcileError })
        }
        try {
          requestCanonicalSnapshot?.()
        } catch (snapshotError) {
          log.error('cancel_snapshot_request_failed', { conversation_id: conversationId, error: snapshotError })
        }
        return true
      }

      if (cancellation.outcome === 'already_terminal') {
        await reconcileWithDb(conversationId)
        setCancelling(false)
        if (cancellation.status !== 'finalizing') {
          lifecycle.markProcessingResolved()
          lifecycle.stopProcessing()
          lifecycle.disarmCancelTimeout()
          useMessageStore.getState().removeMessage(lifecycle.placeholderId(conversationId))
        }
        return true
      }

      throw new Error('Cancellation response did not include a recognized outcome')
    } catch (error) {
      log.error('cancel_failed', { conversation_id: conversationId, error })
      setCancelling(false)
      toast.error(`Failed to stop processing: ${error instanceof Error ? error.message : 'Unknown error'}`)
      return false
    }
  }, [
    getToken,
    setCancelling,
    lifecycle,
    conversationId,
    reconcileWithDb,
    requestCanonicalSnapshot,
  ])

  const respondToHitlBatch = useCallback(async (
    interactionId: string,
    answers: Array<{ requestId: string; answer: string }>,
    clientRequestId?: string,
  ) => {
    const answerById = new Map(answers.map(answer => [answer.requestId, answer.answer]))
    const store = useMessageStore.getState()
    const interactionEntities = Object.values(store.entities).filter(entity =>
      entity.conversationId === conversationId
      && entity.hitlRequestId
      && (entity.hitlInteractionId ?? entity.hitlGroupId ?? entity.hitlRequestId) === interactionId
    )
    const entities = interactionEntities.filter(entity => (
      entity.hitlRequestId && answerById.has(entity.hitlRequestId)
    ))

    const releaseSubmissionFence = acquireHitlSubmissionFence(conversationId, interactionId)
    let response
    try {
      const { respondToHitlBatch: submitBatch } = await import('@/lib/api/hitl')
      response = await submitBatch(
        conversationId,
        interactionId,
        answers,
        clientRequestId,
        getToken,
      )
    } catch (error) {
      if (error instanceof ApiError && (error.status === 409 || error.status === 410)) {
        // Identical durable retries return success from the backend. A typed
        // conflict is therefore never inferred as success from local state:
        // refresh DB + REST pending authority, request a forced canonical
        // snapshot reconnect as best effort, then surface the original conflict.
        await reconcileWithDb(conversationId)
        try {
          requestCanonicalSnapshot?.()
        } catch (snapshotError) {
          log.error('hitl_snapshot_request_failed', { conversation_id: conversationId, error: snapshotError })
        }
        if (!getAgentName || !getAgentSource) {
          throw new Error('Authoritative HITL refresh is unavailable.', { cause: error })
        }
        const refreshed = await hydrateRoomFromDb({
          conversationId,
          phase: 'hitl_overlay',
          getToken,
          hitlRequestIndex,
          getAgentName,
          getAgentSource,
        })
        if (refreshed.hitlFetchFailed) {
          throw new Error('Authoritative HITL pending refresh failed.', { cause: error })
        }
      }
      throw error
    } finally {
      releaseSubmissionFence()
    }

    // MessageStore and the lifecycle may now belong to a different room.
    if (useMessageStore.getState().conversationId !== conversationId) return

    const applied = response.status === 'applied' || response.status === 'responded'
    for (const entity of entities) {
      const requestId = entity.hitlRequestId
      if (!requestId) continue
      store.upsertMessage({
        id: entity.id,
        conversationId,
        messageType: 'agent',
        content: entity.content,
        senderName: entity.senderName,
        timestamp: entity.timestamp,
        hitlResolved: applied,
        hitlUserAnswer: answerById.get(requestId),
        hitlInteractionStatus: applied ? 'applied' : 'applying',
        hitlApplicationStatus: applied ? 'applied' : 'applying',
      }, 'optimistic')
      if (applied) {
        hitlRequestIndex.current.delete(hitlRequestKey(interactionId, requestId))
        hitlRequestIndex.current.delete(requestId)
      }
    }

    if (applied) {
      const first = entities[0]
      const resumeClientRequestId = clientRequestId ?? first?.clientRequestId
      const processingUserEntity = findProcessingStatusUserEntity(conversationId, {
        relatedMessageId: first?.relatedMessageId,
        clientRequestId: resumeClientRequestId,
        beforeTimestamp: first?.timestamp,
      })
      const currentMessageId = lifecycle.getMessageId()
      const currentClientRequestId = lifecycle.getClientRequestId()
      const pendingAck = lifecycle.getPendingRunEventAck()
      if (
        (currentMessageId && currentMessageId !== processingUserEntity?.id)
        || (currentClientRequestId && currentClientRequestId !== resumeClientRequestId)
        || (pendingAck && pendingAck !== resumeClientRequestId)
        || (first?.clientRequestId && first.clientRequestId !== resumeClientRequestId)
        || useRoomUiStore.getState().getRoomFlags(conversationId).cancelling
      ) return

      // The HTTP request waits for continuation: newer SSE may already have
      // settled this exact root or paused it at a subsequent questionnaire.
      // HITL itself marks processing resolved, so that flag cannot fence ACKs.
      const turns = useTurnStore.getState().rooms[conversationId]?.turns
      const turn = Object.values(turns ?? {}).find(candidate => (
        (candidate.userMessageId === processingUserEntity?.id
          && candidate.clientRequestId === resumeClientRequestId)
        || candidate.hitlInteractions.some(item => item.interactionId === interactionId)
      ))
      if (turn) {
        const interaction = turn.hitlInteractions.find(item => item.interactionId === interactionId)
        if (
          turn.userMessageId !== processingUserEntity?.id
          || turn.clientRequestId !== resumeClientRequestId
          || !['active', 'awaiting_input'].includes(turn.state)
          || !interaction
          || !['awaiting_input', 'resumed'].includes(interaction.state)
          || (turn.activeInteractionId && turn.activeInteractionId !== interactionId)
          || turn.hitlInteractions.some(item => (
            item.interactionId !== interactionId && item.state === 'awaiting_input'
          ))
        ) return
      } else if (processingUserEntity?.turnTerminalStatus) {
        return
      }
      const latestStore = useMessageStore.getState()
      if (selectPendingHitls(conversationId, latestStore.entities, latestStore.orderedIds).some(hitl => (
        hitl.interactionId !== interactionId && !hitl.isAnswered
      ))) return

      lifecycle.resetPlaceholder()
      lifecycle.resetProcessingResolved()
      lifecycle.setPendingRunEventAck(resumeClientRequestId ?? null)
      store.removeMessage(lifecycle.placeholderId(conversationId))
      lifecycle.startProcessing(processingUserEntity?.id)
    }
  }, [
    getAgentName,
    getAgentSource,
    getToken,
    hitlRequestIndex,
    lifecycle,
    reconcileWithDb,
    requestCanonicalSnapshot,
    conversationId,
  ])

  const cancelHitlRequest = useCallback(async (
    requestId: string,
    requestedInteractionId?: string,
  ) => {
    const store = useMessageStore.getState()
    const target = Object.values(store.entities).find(entity => {
      if (entity.conversationId !== conversationId || entity.hitlRequestId !== requestId) return false
      const entityInteractionId = entity.hitlInteractionId
        ?? entity.hitlGroupId
        ?? entity.hitlRequestId
      return requestedInteractionId === undefined
        ? !entity.hitlResolved
        : entityInteractionId === requestedInteractionId
    })
    const interactionId = requestedInteractionId ?? (target
      ? (target.hitlInteractionId ?? target.hitlGroupId ?? target.hitlRequestId)
      : undefined)
    if (!interactionId || !target?.hitlInteractionVersion) {
      throw new Error('The interaction changed before it could be canceled.')
    }
    setCancelling(true)
    lifecycle.setCancelTimedOut(false)
    let result
    try {
      const { cancelHitl } = await import('@/lib/api/hitl')
      result = await cancelHitl(
        conversationId,
        interactionId,
        target.hitlInteractionVersion,
        target.clientRequestId ?? crypto.randomUUID(),
        getToken,
      )
    } catch (error) {
      if (error instanceof ApiError && (error.status === 404 || error.status === 409 || error.status === 410)) {
        try {
          await reconcileWithDb(conversationId)
        } catch (reconcileError) {
          log.error('hitl_cancel_reconcile_failed', { conversation_id: conversationId, outcome: 'rejected', error: reconcileError })
        }
      }
      setCancelling(false)
      throw error
    }

    for (const entity of Object.values(store.entities)) {
      const entityInteractionId = entity.hitlInteractionId ?? entity.hitlGroupId ?? entity.hitlRequestId
      if (entity.conversationId !== conversationId || entityInteractionId !== interactionId) continue
      store.upsertMessage({
        id: entity.id,
        conversationId,
        messageType: 'agent',
        content: entity.content,
        senderName: entity.senderName,
        timestamp: entity.timestamp,
        hitlResolved: true,
        hitlInteractionStatus: 'canceled',
        hitlInteractionVersion: result.interaction_version,
        taskStatus: 'canceled',
        taskError: 'Input request canceled',
      }, 'optimistic')
      if (entity.hitlRequestId) {
        hitlRequestIndex.current.delete(hitlRequestKey(
          entity.hitlInteractionId ?? entity.hitlGroupId,
          entity.hitlRequestId,
        ))
        hitlRequestIndex.current.delete(entity.hitlRequestId)
      }
    }

    try {
      await reconcileWithDb(conversationId)
    } catch (reconcileError) {
      log.error('hitl_cancel_reconcile_failed', { conversation_id: conversationId, outcome: 'accepted', error: reconcileError })
    }
    try {
      requestCanonicalSnapshot?.()
    } catch (snapshotError) {
      log.error('hitl_cancel_snapshot_request_failed', { conversation_id: conversationId, error: snapshotError })
    }
  }, [
    getToken,
    hitlRequestIndex,
    lifecycle,
    reconcileWithDb,
    requestCanonicalSnapshot,
    conversationId,
    setCancelling,
  ])

  // Manually refresh messages — reconciles from DB and re-overlays any pending HITL questions
  // that may have been missed by SSE (e.g. during the "Applying your answers" transition).
  const refreshMessages = useCallback(async () => {
    await reconcileWithDb(conversationId)
    if (getAgentName && getAgentSource) {
      await hydrateRoomFromDb({
        conversationId,
        phase: 'hitl_overlay',
        getToken,
        hitlRequestIndex,
        getAgentName,
        getAgentSource,
      })
    }
  }, [conversationId, reconcileWithDb, getToken, hitlRequestIndex, getAgentName, getAgentSource])

  // Toggle SSE connection
  const toggleSSE = useCallback(() => {
    setSseEnabled(!sseEnabled)
  }, [setSseEnabled, sseEnabled])

  return {
    cancelProcessing,
    respondToHitlBatch,
    cancelHitlRequest,
    refreshMessages,
    toggleSSE,
  }
}

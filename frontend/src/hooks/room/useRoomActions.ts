import { useCallback } from 'react'
import type { MutableRefObject } from 'react'
import { cancelMessage } from '@/lib/api/sse'
import { ApiError } from '@/lib/api-client'
import { banner } from '@/components/ui/banner'
import { useRoomUiStore } from '@/stores/room-ui-store'
import { useMessageStore } from '@/stores/message-store'
import type { ProcessingLifecycle } from './processing-lifecycle'
import {
  appendProcessingStatusLog,
  ensureInitialProcessingStatusLog,
  findProcessingStatusUserEntity,
} from './processing-status-log'
import { hydrateRoomFromDb } from '@/lib/room-sync/hydrate-room'
import { hitlRequestKey } from '@/lib/hitl/hitl-message-projection'
import { acquireHitlSubmissionFence } from './hitl-submission-fence'

export function useRoomActions(
  roomId: string,
  getToken: (() => Promise<string | null>) | undefined,
  lifecycle: ProcessingLifecycle,
  hitlRequestIndex: MutableRefObject<Map<string, string>>,
  reconcileWithDb: (roomId: string) => Promise<void>,
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
      banner.warning('Unable to cancel — no active task found')
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
        const store = useMessageStore.getState()
        const userMessage = store.entities[messageId]
        ensureInitialProcessingStatusLog(roomId, userMessage)
        appendProcessingStatusLog(
          roomId,
          userMessage,
          'Stopping...',
          new Date().toISOString(),
        )
        lifecycle.armCancelTimeout(() => {
          const cancelling = useRoomUiStore.getState().getRoomFlags(roomId).cancelling
          if (cancelling) {
            lifecycle.setCancelTimedOut(true)
            banner.warning('Cancellation is taking longer than expected — the agent may still be stopping')
          }
        })
        try {
          await reconcileWithDb(roomId)
        } catch (reconcileError) {
          console.error('Failed to reconcile pending cancellation:', reconcileError)
        }
        try {
          requestCanonicalSnapshot?.()
        } catch (snapshotError) {
          console.error('Failed to request cancellation snapshot:', snapshotError)
        }
        return true
      }

      if (cancellation.outcome === 'already_terminal') {
        await reconcileWithDb(roomId)
        setCancelling(false)
        if (cancellation.status !== 'finalizing') {
          lifecycle.markProcessingResolved()
          lifecycle.stopProcessing()
          lifecycle.disarmCancelTimeout()
          useMessageStore.getState().removeMessage(lifecycle.placeholderId(roomId))
        }
        return true
      }

      throw new Error('Cancellation response did not include a recognized outcome')
    } catch (error) {
      console.error('Error cancelling message:', error)
      setCancelling(false)
      banner.error(`Failed to stop processing: ${error instanceof Error ? error.message : 'Unknown error'}`)
      return false
    }
  }, [
    getToken,
    setCancelling,
    lifecycle,
    roomId,
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
      entity.roomId === roomId
      && entity.hitlRequestId
      && (entity.hitlInteractionId ?? entity.hitlGroupId ?? entity.hitlRequestId) === interactionId
    )
    const entities = interactionEntities.filter(entity => (
      entity.hitlRequestId && answerById.has(entity.hitlRequestId)
    ))

    const releaseSubmissionFence = acquireHitlSubmissionFence(roomId, interactionId)
    let response
    try {
      const { respondToHitlBatch: submitBatch } = await import('@/lib/api/hitl')
      response = await submitBatch(
        roomId,
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
        await reconcileWithDb(roomId)
        try {
          requestCanonicalSnapshot?.()
        } catch (snapshotError) {
          console.error('Failed to request canonical HITL snapshot:', snapshotError)
        }
        if (!getAgentName || !getAgentSource) {
          throw new Error('Authoritative HITL refresh is unavailable.', { cause: error })
        }
        const refreshed = await hydrateRoomFromDb({
          roomId,
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

    const applied = response.status === 'applied' || response.status === 'responded'
    for (const entity of entities) {
      const requestId = entity.hitlRequestId
      if (!requestId) continue
      store.upsertMessage({
        id: entity.id,
        roomId,
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
      lifecycle.resetPlaceholder()
      lifecycle.resetProcessingResolved()
      lifecycle.setPendingRunEventAck(clientRequestId ?? first?.clientRequestId ?? null)
      const processingUserEntity = findProcessingStatusUserEntity(roomId, {
        relatedMessageId: first?.relatedMessageId,
        clientRequestId: clientRequestId ?? first?.clientRequestId,
        beforeTimestamp: first?.timestamp,
      })
      store.removeMessage(lifecycle.placeholderId(roomId))
      ensureInitialProcessingStatusLog(roomId, processingUserEntity)
      appendProcessingStatusLog(
        roomId,
        processingUserEntity,
        'Applying your answers…',
        new Date(Date.now() + 1).toISOString(),
      )
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
    roomId,
  ])

  const cancelHitlRequest = useCallback(async (
    requestId: string,
    requestedInteractionId?: string,
  ) => {
    const store = useMessageStore.getState()
    const target = Object.values(store.entities).find(entity => {
      if (entity.roomId !== roomId || entity.hitlRequestId !== requestId) return false
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
    const { cancelHitl } = await import('@/lib/api/hitl')
    let result
    try {
      result = await cancelHitl(
        roomId,
        interactionId,
        target.hitlInteractionVersion,
        target.clientRequestId ?? crypto.randomUUID(),
        getToken,
      )
    } catch (error) {
      if (error instanceof ApiError && (error.status === 404 || error.status === 409 || error.status === 410)) {
        await reconcileWithDb(roomId)
      }
      throw error
    }

    for (const entity of Object.values(store.entities)) {
      const entityInteractionId = entity.hitlInteractionId ?? entity.hitlGroupId ?? entity.hitlRequestId
      if (entity.roomId !== roomId || entityInteractionId !== interactionId) continue
      store.upsertMessage({
        id: entity.id,
        roomId,
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
  }, [getToken, hitlRequestIndex, reconcileWithDb, roomId])

  // Manually refresh messages — reconciles from DB and re-overlays any pending HITL questions
  // that may have been missed by SSE (e.g. during the "Applying your answers" transition).
  const refreshMessages = useCallback(async () => {
    await reconcileWithDb(roomId)
    if (getAgentName && getAgentSource) {
      await hydrateRoomFromDb({
        roomId,
        phase: 'hitl_overlay',
        getToken,
        hitlRequestIndex,
        getAgentName,
        getAgentSource,
      })
    }
  }, [roomId, reconcileWithDb, getToken, hitlRequestIndex, getAgentName, getAgentSource])

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

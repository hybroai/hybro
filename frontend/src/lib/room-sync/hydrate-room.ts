import { inquiryRoomMessagesByConversationId } from '@/lib/api/room'
import {
  convertApiMessageToIncoming,
  detectAndMarkStaleTasks,
  filterHydrationMessages,
} from '@/stores/message-store'
import { useMessageStore } from '@/stores/message-store'
import { useRoomUiStore } from '@/stores/room-ui-store'
import { isTerminalState } from '@/lib/types/sse'
import type { IncomingMessage } from '@/stores/message-store/types'
import { applyDbMessages } from './apply-db-messages'
import { overlayHitlForRoom } from './hitl-overlay'
import type { HydrateRoomOptions, HydrateRoomResult } from './types'
import { log } from '@/lib/log'

function markInitialHydrationComplete(targetConversationId: string): boolean {
  const store = useMessageStore.getState()
  if (store.conversationId !== targetConversationId) return false

  store.markDbSynced()
  useRoomUiStore.getState().markInitialHydrated(targetConversationId)
  return true
}

function pruneStaleProcessingPlaceholder(targetConversationId: string): void {
  const store = useMessageStore.getState()
  if (store.conversationId !== targetConversationId) return

  const placeholderId = `processing-placeholder-${targetConversationId}`
  if (!store.entities[placeholderId]) return

  const hasActiveTask = Object.values(store.entities).some(
    e => e.conversationId === targetConversationId
      && !e.isEphemeral
      && e.taskStatus
      && !isTerminalState(e.taskStatus),
  )
  if (hasActiveTask) return

  store.removeMessage(placeholderId)
}

async function fetchAndNormalizeMessages(
  targetConversationId: string,
  options: HydrateRoomOptions,
): Promise<{ filtered: IncomingMessage[]; rawCount: number } | { fetchFailed: true }> {
  const { getToken, userId, userName, getAgentName, getAgentSource } = options
  const response = await inquiryRoomMessagesByConversationId(targetConversationId, getToken)
  if (!response.success || !response.message_list) {
    return { fetchFailed: true }
  }

  const incomingMessages = await Promise.all(
    response.message_list.map(msg =>
      convertApiMessageToIncoming(msg, { userId, userName, getAgentName, getAgentSource }),
    ),
  )
  const withStaleDetection = detectAndMarkStaleTasks(incomingMessages)
  const filtered = filterHydrationMessages(withStaleDetection)

  return { filtered, rawCount: response.message_list.length }
}

const EMPTY_RESULT: HydrateRoomResult = {
  rawCount: 0,
  filteredCount: 0,
  appliedCount: 0,
  pendingHitlCount: 0,
  fetchFailed: false,
  hitlFetchFailed: false,
}

/**
 * Unified DB hydration / reconcile / HITL overlay orchestrator.
 * React hooks own gates (hydratedFromDb, inflight dedupe, retries).
 */
export async function hydrateRoomFromDb(options: HydrateRoomOptions): Promise<HydrateRoomResult> {
  const { conversationId, phase } = options

  if (phase === 'hitl_overlay') {
    if (!options.hitlRequestIndex) {
      log.warn('hitl_overlay_skipped', { conversation_id: conversationId, reason: 'missing_request_index' })
      return { ...EMPTY_RESULT }
    }
    try {
      const pending = await overlayHitlForRoom({
        conversationId,
        getToken: options.getToken,
        hitlRequestIndex: options.hitlRequestIndex,
        getAgentName: options.getAgentName,
        getAgentSource: options.getAgentSource,
      })
      return { ...EMPTY_RESULT, pendingHitlCount: pending.size }
    } catch (err) {
      log.error('hitl_overlay_failed', { conversation_id: conversationId, phase, error: err })
      return { ...EMPTY_RESULT, hitlFetchFailed: true }
    }
  }

  try {
    const fetched = await fetchAndNormalizeMessages(conversationId, options)
    if ('fetchFailed' in fetched) {
      log.error('room_hydration_failed', { conversation_id: conversationId, phase, reason: 'fetch_failed' })
      if (phase === 'initial') {
        markInitialHydrationComplete(conversationId)
      }
      return { ...EMPTY_RESULT, fetchFailed: true }
    }

    const { filtered, rawCount } = fetched
    const applyResult = applyDbMessages(conversationId, filtered)

    if (!applyResult) {
      return {
        rawCount,
        filteredCount: filtered.length,
        appliedCount: 0,
        pendingHitlCount: 0,
        fetchFailed: false,
        hitlFetchFailed: false,
      }
    }

    if (phase === 'initial') {
      markInitialHydrationComplete(conversationId)
    } else {
      useMessageStore.getState().markDbSynced()
      pruneStaleProcessingPlaceholder(conversationId)
    }

    let pendingHitlCount = 0
    let hitlFetchFailed = false
    if (phase === 'initial' && options.hitlRequestIndex) {
      try {
        const hydratedIds = new Set(filtered.map(m => m.id))
        const pending = await overlayHitlForRoom({
          conversationId,
          getToken: options.getToken,
          hitlRequestIndex: options.hitlRequestIndex,
          getAgentName: options.getAgentName,
          getAgentSource: options.getAgentSource,
          hydratedIdsForResolve: hydratedIds,
        })
        pendingHitlCount = pending.size
      } catch (hitlErr) {
        hitlFetchFailed = true
        log.error('hitl_overlay_failed', { conversation_id: conversationId, phase, error: hitlErr })
      }
    }

    return {
      rawCount,
      filteredCount: filtered.length,
      appliedCount: applyResult.appliedCount,
      pendingHitlCount,
      fetchFailed: false,
      hitlFetchFailed,
    }
  } catch (error) {
    log.error('room_hydration_failed', { conversation_id: conversationId, phase, error })
    if (phase === 'initial') {
      markInitialHydrationComplete(conversationId)
    }
    return { ...EMPTY_RESULT, fetchFailed: true }
  }
}

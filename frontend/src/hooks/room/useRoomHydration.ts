import { useCallback, useEffect, useRef } from 'react'
import type { MutableRefObject } from 'react'
import { useMessageStore } from '@/stores/message-store'
import { hydrateRoomFromDb } from '@/lib/room-sync/hydrate-room'
import { log } from '@/lib/log'

export function useRoomHydration(
  conversationId: string,
  userId: string | undefined,
  userName: string | undefined,
  getToken: (() => Promise<string | null>) | undefined,
  _room: unknown,
  hitlRequestIndex: MutableRefObject<Map<string, string>>,
  getAgentName: (agentId: string) => Promise<string>,
  getAgentSource: (agentId: string | undefined) => 'cloud' | 'local' | 'hub' | undefined,
) {
  const hydrationStartedRef = useRef<string | null>(null)
  const reconcileInflightRef = useRef<string | null>(null)

  const hydrateFromDb = useCallback(async (targetConversationId: string) => {
    const store = useMessageStore.getState()
    if (store.hydratedFromDb && store.conversationId === targetConversationId) return

    await hydrateRoomFromDb({
      conversationId: targetConversationId,
      phase: 'initial',
      getToken,
      userId,
      userName,
      getAgentName,
      getAgentSource,
      hitlRequestIndex,
    })
  }, [getToken, userId, userName, getAgentName, getAgentSource, hitlRequestIndex])

  const reconcileWithDb = useCallback(async (targetConversationId: string) => {
    if (reconcileInflightRef.current === targetConversationId) return
    reconcileInflightRef.current = targetConversationId
    try {
      let result = await hydrateRoomFromDb({
        conversationId: targetConversationId,
        phase: 'reconcile',
        getToken,
        userId,
        userName,
        getAgentName,
        getAgentSource,
      })

      if (!result.fetchFailed && result.filteredCount === 0) {
        await new Promise(r => setTimeout(r, 2000))
        if (reconcileInflightRef.current !== targetConversationId) return
        result = await hydrateRoomFromDb({
          conversationId: targetConversationId,
          phase: 'reconcile',
          getToken,
          userId,
          userName,
          getAgentName,
          getAgentSource,
        })
      }
    } catch (error) {
      log.error('room_reconcile_failed', { conversation_id: targetConversationId, error })
    } finally {
      if (reconcileInflightRef.current === targetConversationId) {
        reconcileInflightRef.current = null
      }
    }
  }, [getToken, userId, userName, getAgentName, getAgentSource])

  useEffect(() => {
    hydrationStartedRef.current = null
    reconcileInflightRef.current = null
  }, [conversationId])

  useEffect(() => {
    if (!conversationId || !userName || !_room) return
    if (hydrationStartedRef.current === conversationId) return
    hydrationStartedRef.current = conversationId
    hydrateFromDb(conversationId)
  }, [conversationId, userName, _room, hydrateFromDb])

  return { hydrateFromDb, reconcileWithDb }
}

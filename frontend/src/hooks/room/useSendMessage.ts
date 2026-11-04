import { useCallback } from 'react'
import { SendMessage } from '@/lib/api/room'
import { getActiveQueryClient } from '@/components/providers/query-provider'
import {
  optimisticallyMarkRoomProcessing,
  roomHistoryQueryKey,
} from '@/lib/room-history-query'
import { toast } from 'sonner'
import type { QuoteData, RoomQuoteWire } from '@/lib/types/quote'
import { MAX_QUOTE_TEXT_LENGTH } from '@/lib/types/quote'
import type { AgentScopeInput, ExecutionMode } from '@/lib/types/request'
import { useMessageStore } from '@/stores/message-store'
import type { PendingAttachment } from '@/lib/types/attachments'
import type { ProcessingLifecycle } from './processing-lifecycle'
import { useRoomUiStore } from '@/stores/room-ui-store'
import { log } from '@/lib/log'

export type SendUserMessageInput = {
  userInput: string
  quoteData?: QuoteData
  pendingAttachments?: PendingAttachment[]
  mode: ExecutionMode
  agentScope: AgentScopeInput
  clientRequestId?: string
}

export function useSendMessage(
  conversationId: string,
  userId: string | undefined,
  userName: string | undefined,
  room: unknown,
  getToken: (() => Promise<string | null>) | undefined,
  sending: boolean,
  sseConnected: boolean,
  lifecycle: ProcessingLifecycle,
  setSending: (v: boolean) => void,
  setCancelling: (v: boolean) => void,
  reconcileWithDb: (conversationId: string) => Promise<void>,
) {
  const sendUserMessage = useCallback(async ({
    userInput,
    quoteData,
    pendingAttachments,
    mode,
    agentScope,
    clientRequestId: existingClientRequestId,
  }: SendUserMessageInput) => {
    if (!userId || !userName || !room || sending || lifecycle.isSendGuardActive()) {
      log.warn('send_blocked', {
        conversation_id: conversationId,
        missing_user_id: !userId,
        missing_user_name: !userName,
        missing_room: !room,
        sending,
        send_guard_active: lifecycle.isSendGuardActive(),
        message_id: lifecycle.getMessageId(),
      })
      return false
    }

    const clientRequestId = existingClientRequestId ?? crypto.randomUUID()
    const optimisticUserMessageId = `cr:${clientRequestId}`
    const currentTime = new Date().toISOString()
    useRoomUiStore.getState().setPendingTurnSkeleton(conversationId, {
      text: userInput,
      attachments: pendingAttachments,
    })

    // Reset the transient processing lifecycle so SSE processing_status events
    // can manage the current live turn.
    lifecycle.resetPlaceholder()
    lifecycle.resetProcessingResolved()
    lifecycle.setPendingRunEventAck(clientRequestId)

    // Step 0: Add the optimistic user anchor with its live processing log immediately.
    // This prevents fast SSE events from attaching to the previous turn.
    const optimisticAttachments = pendingAttachments?.map(att => ({
      fileId: att.id,
      fileUrl: att.previewUrl || undefined,
      mimeType: att.file.type,
      fileName: att.file.name,
      sizeBytes: att.file.size,
    }))

    const msgStoreSend = useMessageStore.getState()
    msgStoreSend.removeMessage(lifecycle.placeholderId(conversationId))
    msgStoreSend.upsertMessage({
      id: optimisticUserMessageId,
      conversationId,
      messageType: 'user',
      content: userInput,
      senderName: userName,
      userId,
      timestamp: currentTime,
      clientRequestId,
      attachments: optimisticAttachments,
      quotedText: quoteData?.content ?? undefined,
      quotedSenderName: quoteData?.senderName ?? undefined,
    }, 'optimistic')
    lifecycle.startProcessing(optimisticUserMessageId)
    useRoomUiStore.getState().setActiveRunTriggerMessageIds(conversationId, [optimisticUserMessageId])

    useRoomUiStore.getState().markLocalSend(conversationId)
    const queryClient = getActiveQueryClient()
    const rollbackRoomHistory = queryClient
      ? await optimisticallyMarkRoomProcessing(queryClient, userId, conversationId, currentTime)
      : () => undefined

    try {
      setSending(true)  // Show spinner during message creation & parsing
      lifecycle.setSendGuard(true)

      // Upload pending attachments
      let uploadedAttachments: Array<{ file_id: string }> | undefined
      let uploadResponses: Map<string, { fileId: string; fileUrl: string; mimeType: string; fileName: string; sizeBytes: number }> | undefined
      if (pendingAttachments && pendingAttachments.length > 0) {
        const { uploadFile } = await import('@/lib/api/files')
        uploadResponses = new Map()
        const results = await Promise.all(
          pendingAttachments.map(async (att) => {
            const res = await uploadFile(att.file, conversationId, getToken)
            uploadResponses!.set(att.id, {
              fileId: res.file_id,
              fileUrl: res.file_url,
              mimeType: res.mime_type,
              fileName: res.file_name,
              sizeBytes: res.size_bytes,
            })
            return { file_id: res.file_id }
          })
        )
        uploadedAttachments = results
      }

      if (quoteData?.content && quoteData.content.length > MAX_QUOTE_TEXT_LENGTH) {
        toast.error(`Quote is too long (max ${MAX_QUOTE_TEXT_LENGTH} characters).`)
        lifecycle.stopProcessing()
        useRoomUiStore.getState().setPendingTurnSkeleton(conversationId)
        const msgStoreTooLong = useMessageStore.getState()
        msgStoreTooLong.removeMessage(optimisticUserMessageId)
        msgStoreTooLong.removeMessage(lifecycle.placeholderId(conversationId))
        rollbackRoomHistory()
        setSending(false)
        lifecycle.setSendGuard(false)
        return false
      }

      const structuredQuote: RoomQuoteWire | null = quoteData
        ? {
            text: quoteData.content.trim(),
            source_message_id: quoteData.messageId,
            source_kind: quoteData.sourceKind ?? 'unknown',
            sender_display_name: quoteData.senderName || null,
            source_agent_id: quoteData.sourceAgentId ?? null,
          }
        : null

      // Step 1: Send user message to backend using unified SendMessage API
      const createResponse = await SendMessage({
        conversationId,
        userInput,
        getToken,
        userId,
        userName,
        attachments: uploadedAttachments,
        mode,
        agentScope,
        clientRequestId,
        structuredQuote,
      })

      if (!createResponse.success) {
        throw new Error(`Failed to create user message: ${createResponse.error}`)
      }

      // Extract message_id from createResponse
      const messageId = createResponse.message_id || createResponse.message?.message_id || ""

      if (!messageId) {
        log.error('send_missing_message_id', { conversation_id: conversationId, client_request_id: clientRequestId })

        // Rollback optimistic entities.
        const msgStoreNoId = useMessageStore.getState()
        msgStoreNoId.removeMessage(optimisticUserMessageId)
        msgStoreNoId.removeMessage(lifecycle.placeholderId(conversationId))
        rollbackRoomHistory()

        toast.error('Message sent but server returned no ID. Please try again.')

        // Revoke orphaned blob URLs since attachments have already been cleared
        // from the input component and are unreachable by its cleanup.
        if (pendingAttachments) {
          for (const att of pendingAttachments) {
            if (att.previewUrl) {
              try { URL.revokeObjectURL(att.previewUrl) } catch { /* ignore */ }
            }
          }
        }

        lifecycle.stopProcessing()
        useRoomUiStore.getState().setPendingTurnSkeleton(conversationId)

        return false
      }

      // Step 2: Atomically swap the optimistic ID for the real server ID and
      // apply server-resolved attachment URLs in one state update.  Using two
      // separate calls (upsertMessage + replaceMessageId) would cause React to
      // render an intermediate state where both IDs exist, producing a visible
      // flash as the ConversationTurn key changes.
      const serverQuoteId =
        createResponse.message?.quote_id != null && createResponse.message.quote_id !== ''
          ? createResponse.message.quote_id
          : undefined

      const msgStoreSwap = useMessageStore.getState()
      msgStoreSwap.replaceAndPatchMessageId(optimisticUserMessageId, messageId, {
        content: userInput,
        clientRequestId,
        userId,
        timestamp: currentTime,
        quoteId: serverQuoteId,
        attachments: pendingAttachments?.map(att => {
          const uploaded = uploadResponses?.get(att.id)
          return {
            fileId: uploaded?.fileId || att.id,
            fileUrl: uploaded?.fileUrl || att.previewUrl || undefined,
            mimeType: uploaded?.mimeType || att.file.type,
            fileName: uploaded?.fileName || att.file.name,
            sizeBytes: uploaded?.sizeBytes || att.file.size,
          }
        }),
      })

      useRoomUiStore.getState().setPendingTurnSkeleton(conversationId)

      // Set correlation state before any post-send SSE flows in. A fast
      // terminal event can clear this state; do not rewrite it afterward.
      lifecycle.setMessageId(messageId)
      lifecycle.setPendingRunEventAck(clientRequestId)

      // Blob preview URLs are no longer needed now that server URLs are in
      // the store.  Revoke them to free browser blob memory.
      if (pendingAttachments) {
        for (const att of pendingAttachments) {
          if (att.previewUrl) {
            try { URL.revokeObjectURL(att.previewUrl) } catch { /* ignore */ }
          }
        }
      }

      // Step 3: Processing is auto-triggered by backend when sendMessage completes.
      // Only keep processing state if SSE hasn't already dismissed the live log
      // (race condition: fast agents can complete before the HTTP response returns).
      setSending(false)
      if (!lifecycle.isProcessingResolved()) {
        lifecycle.startProcessing(messageId)
      } else {
        // SSE already handled the full lifecycle — just make sure ref is clean
        lifecycle.setSendGuard(false)
      }
      setCancelling(false)
      lifecycle.setCancelTimedOut(false)
      lifecycle.clearSseDisconnection()
      lifecycle.disarmCancelTimeout()

      if (queryClient) {
        void queryClient.invalidateQueries({
          queryKey: roomHistoryQueryKey(userId),
          exact: true,
        })
      }

      return true

    } catch (error) {
      log.error('send_failed', { conversation_id: conversationId, client_request_id: clientRequestId, error })

      // Targeted rollback: remove only the specific optimistic messages (Gap 5)
      const msgStoreErr = useMessageStore.getState()
      msgStoreErr.removeMessage(optimisticUserMessageId)
      msgStoreErr.removeMessage(lifecycle.placeholderId(conversationId))
      rollbackRoomHistory()

      toast.error(`Failed to send message: ${error instanceof Error ? error.message : 'Unknown error'}`)

      // Revoke orphaned blob URLs since attachments have already been cleared
      // from the input component and are unreachable by its cleanup.
      if (pendingAttachments) {
        for (const att of pendingAttachments) {
          if (att.previewUrl) {
            try { URL.revokeObjectURL(att.previewUrl) } catch { /* ignore */ }
          }
        }
      }

      // Reconcile to recover any messages that might have been lost
      try {
        await reconcileWithDb(conversationId)
      } catch (reloadError) {
        log.error('send_reconcile_failed', { conversation_id: conversationId, client_request_id: clientRequestId, error: reloadError })
      }

      lifecycle.stopProcessing()
      useRoomUiStore.getState().setPendingTurnSkeleton(conversationId)

      return false
    } finally {
      setSending(false)
      // NOTE: Do NOT clear send guard here on the success path.
      // It stays true until processing completes (via SSE terminal events)
      // to prevent a race window where the user could double-send between
      // lifecycle.startProcessing(...) propagating through Zustand and the next render.
    }
  }, [userId, userName, room, conversationId, sending, sseConnected, getToken, setSending, lifecycle, setCancelling, reconcileWithDb])

  return { sendUserMessage }
}

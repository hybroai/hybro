export interface ProcessingLifecycle {
  setProcessing(active: boolean): void
  startProcessing(messageId?: string | null, clientRequestId?: string | null): void
  stopProcessing(options?: { clearMessageId?: boolean; clearSendGuard?: boolean }): void
  setPendingRunEventAck(clientRequestId: string | null): void
  getPendingRunEventAck(): string | null
  clearPendingRunEventAck(): void
  setSendGuard(active: boolean): void
  isSendGuardActive(): boolean
  setMessageId(id: string | null): void
  getMessageId(): string | null
  getClientRequestId(): string | null
  dismissPlaceholder(): void
  resetPlaceholder(): void
  isPlaceholderDismissed(): boolean
  markProcessingResolved(): void
  resetProcessingResolved(): void
  isProcessingResolved(): boolean
  placeholderId(conversationId: string): string
  armCancelTimeout(onTimeout: () => void, ms?: number): void
  disarmCancelTimeout(): void
  hasCancelTimedOut(): boolean
  setCancelTimedOut(v: boolean): void
  markSseDisconnection(): void
  clearSseDisconnection(): void
  hadSseDisconnection(): boolean
  reset(): void
  dispose(): void
  revive(): void
}

export function createProcessingLifecycle(
  setZustandProcessing: (v: boolean) => void
): ProcessingLifecycle {
  let disposed = false
  let currentProcessingMessageId: string | null = null
  let currentProcessingClientRequestId: string | null = null
  let placeholderDismissed = false
  let processingResolved = false
  let isProcessingGuard = false
  let pendingRunEventAckClientRequestId: string | null = null
  let cancelTimeout: ReturnType<typeof setTimeout> | null = null
  let cancelTimedOut = false
  let sseHadDisconnection = false

  return {
    setProcessing(active: boolean) {
      if (disposed) return
      setZustandProcessing(active)
      if (!active) {
        isProcessingGuard = false
      }
    },

    startProcessing(messageId, clientRequestId) {
      if (disposed) return
      if (messageId !== undefined) {
        currentProcessingMessageId = messageId
      }
      currentProcessingClientRequestId = clientRequestId !== undefined
        ? clientRequestId
        : pendingRunEventAckClientRequestId
      isProcessingGuard = true
      setZustandProcessing(true)
    },

    stopProcessing(options) {
      if (disposed) return
      const clearMessageId = options?.clearMessageId ?? true
      const clearSendGuard = options?.clearSendGuard ?? true
      setZustandProcessing(false)
      if (clearSendGuard) {
        isProcessingGuard = false
      }
      if (clearMessageId) {
        currentProcessingMessageId = null
        currentProcessingClientRequestId = null
      }
      pendingRunEventAckClientRequestId = null
    },

    setPendingRunEventAck(clientRequestId: string | null) {
      if (disposed) return
      pendingRunEventAckClientRequestId = clientRequestId
    },

    getPendingRunEventAck() {
      if (disposed) return null
      return pendingRunEventAckClientRequestId
    },

    clearPendingRunEventAck() {
      if (disposed) return
      pendingRunEventAckClientRequestId = null
    },

    setSendGuard(active: boolean) {
      if (disposed) return
      isProcessingGuard = active
    },

    isSendGuardActive() {
      if (disposed) return false
      return isProcessingGuard
    },

    setMessageId(id: string | null) {
      if (disposed) return
      currentProcessingMessageId = id
    },

    getMessageId() {
      if (disposed) return null
      return currentProcessingMessageId
    },

    getClientRequestId() {
      if (disposed) return null
      return currentProcessingClientRequestId
    },

    dismissPlaceholder() {
      if (disposed) return
      placeholderDismissed = true
    },

    resetPlaceholder() {
      if (disposed) return
      placeholderDismissed = false
    },

    isPlaceholderDismissed() {
      if (disposed) return false
      return placeholderDismissed
    },

    markProcessingResolved() {
      if (disposed) return
      processingResolved = true
    },

    resetProcessingResolved() {
      if (disposed) return
      processingResolved = false
    },

    isProcessingResolved() {
      if (disposed) return false
      return processingResolved
    },

    placeholderId(conversationId: string) {
      return `processing-placeholder-${conversationId}`
    },

    armCancelTimeout(onTimeout: () => void, ms = 15000) {
      if (disposed) return
      if (cancelTimeout) {
        clearTimeout(cancelTimeout)
      }
      cancelTimeout = setTimeout(onTimeout, ms)
    },

    disarmCancelTimeout() {
      if (cancelTimeout) {
        clearTimeout(cancelTimeout)
        cancelTimeout = null
      }
    },

    hasCancelTimedOut() {
      if (disposed) return false
      return cancelTimedOut
    },

    setCancelTimedOut(v: boolean) {
      if (disposed) return
      cancelTimedOut = v
    },

    markSseDisconnection() {
      if (disposed) return
      sseHadDisconnection = true
    },

    clearSseDisconnection() {
      if (disposed) return
      sseHadDisconnection = false
    },

    hadSseDisconnection() {
      if (disposed) return false
      return sseHadDisconnection
    },

    reset() {
      if (disposed) return
      setZustandProcessing(false)
      currentProcessingMessageId = null
      currentProcessingClientRequestId = null
      placeholderDismissed = false
      processingResolved = false
      isProcessingGuard = false
      pendingRunEventAckClientRequestId = null
      cancelTimedOut = false
      sseHadDisconnection = false
      if (cancelTimeout) {
        clearTimeout(cancelTimeout)
        cancelTimeout = null
      }
    },

    dispose() {
      disposed = true
      if (cancelTimeout) {
        clearTimeout(cancelTimeout)
        cancelTimeout = null
      }
    },

    // Undoes dispose() for the same room. Only the effect that disposed it
    // calls this, when Strict Mode or <Activity> runs that effect again.
    revive() {
      disposed = false
    },
  }
}

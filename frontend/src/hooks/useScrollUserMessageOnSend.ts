'use client'

import { useCallback, useLayoutEffect, useRef, type RefObject } from 'react'
import { scrollUserMessageToFocus } from '@/lib/conversation/focus-scroll'
import { escapeCssIdent } from '@/lib/room-timeline/escape-css-ident'

interface UseScrollUserMessageOnSendOptions {
  scrollRef: RefObject<HTMLDivElement | null>
  frameRef: RefObject<HTMLDivElement | null>
  lastUserMessageId: string | undefined
  localSendSeq: number
  programmaticScrollRef: RefObject<boolean>
  userPausedRef: RefObject<boolean>
  tailFollowRef?: RefObject<boolean>
}

function findStickyUserElement(frame: HTMLElement, userMessageId: string): HTMLElement | null {
  const userEl = frame.querySelector(
    `[data-message-id="${escapeCssIdent(userMessageId)}"]`,
  ) as HTMLElement | null
  if (!userEl) return null
  return (userEl.closest('.conversation-user-sticky') ?? userEl) as HTMLElement
}

export function useScrollUserMessageOnSend({
  scrollRef,
  frameRef,
  lastUserMessageId,
  localSendSeq,
  programmaticScrollRef,
  userPausedRef,
  tailFollowRef,
}: UseScrollUserMessageOnSendOptions) {
  const prevLocalSendSeqRef = useRef(localSendSeq)
  const prevLastUserMessageIdRef = useRef(lastUserMessageId)

  const scrollLastUserIntoStickyZone = useCallback(() => {
    const container = scrollRef.current
    const frame = frameRef.current
    if (!container || !frame || !lastUserMessageId) return false

    const stickyEl = findStickyUserElement(frame, lastUserMessageId)
    if (!stickyEl) return false

    programmaticScrollRef.current = true
    userPausedRef.current = false
    if (tailFollowRef) tailFollowRef.current = false
    // Align sticky wrapper with scrollport top (matches `.conversation-user-sticky { top: 0 }`).
    scrollUserMessageToFocus(container, stickyEl, 0)
    return true
  }, [scrollRef, frameRef, lastUserMessageId, programmaticScrollRef, userPausedRef, tailFollowRef])

  const scheduleScrollRetries = useCallback(() => {
    let retries = 0
    const tryScroll = () => {
      retries += 1
      if (scrollLastUserIntoStickyZone() || retries >= 5) return
      requestAnimationFrame(tryScroll)
    }
    requestAnimationFrame(tryScroll)
  }, [scrollLastUserIntoStickyZone])

  useLayoutEffect(() => {
    if (localSendSeq === prevLocalSendSeqRef.current) return
    prevLocalSendSeqRef.current = localSendSeq
    if (localSendSeq === 0) return

    if (!scrollLastUserIntoStickyZone()) {
      scheduleScrollRetries()
    }
  }, [localSendSeq, lastUserMessageId, scrollLastUserIntoStickyZone, scheduleScrollRetries])

  useLayoutEffect(() => {
    if (prevLastUserMessageIdRef.current === lastUserMessageId) return
    prevLastUserMessageIdRef.current = lastUserMessageId
    if (!lastUserMessageId || localSendSeq === 0) return

    if (!scrollLastUserIntoStickyZone()) {
      scheduleScrollRetries()
    }
  }, [lastUserMessageId, localSendSeq, scrollLastUserIntoStickyZone, scheduleScrollRetries])
}

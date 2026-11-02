'use client'

import { useRef, useState, useCallback, useEffect, useLayoutEffect, useMemo } from 'react'
import { useMessageStore } from '@/stores/message-store'
import { useInitialHydrationSeq, useLocalSendSeq, useRoomProcessing, useRoomUiStore } from '@/stores/room-ui-store'
import { useCanonicalTurns } from '@/stores/turn-store'
import type { TurnProjection } from '@/lib/turn-lifecycle/types'
import { buildTranscriptTurnSources } from '@/lib/conversation/transcript'
import { usePrimaryStreamScroll } from '@/hooks/usePrimaryStreamScroll'
import { useScrollUserMessageOnSend } from '@/hooks/useScrollUserMessageOnSend'
import {
  readConversationScrollSnapshot,
  restoreConversationScrollWithRetry,
  shouldSkipInitialHydrationScrollRestore,
} from '@/lib/conversation/conversation-scroll'
import { contentEndScrollTop, isNearContentEnd, scrollToContentEnd } from '@/lib/conversation/content-end-scroll'
import { FOCUS_SCROLL_MIN_SPACER_PX } from '@/lib/conversation/focus-scroll'
import { CanonicalTurnRenderer } from './CanonicalTurnRenderer'
import { PendingTurnHeader } from './PendingTurnHeader'
import { TranscriptTurn } from './TranscriptTurn'
import { UserMessageBlock } from './UserMessageBlock'
import { ScrollToBottomButton } from './ScrollToBottomButton'
import { resolveScrollStateAfterEvent } from './scroll-state'
interface ConversationMessageListProps {
  conversationId: string
  selectedAgentMessageId?: string
  enableAgentDetail?: boolean
}

interface ScrollMetrics {
  scrollHeight: number
  scrollTop: number
  clientHeight: number
}

/** Skip tail-follow re-enable inference briefly after programmatic scroll — never blocks user cancel. */
const PROGRAMMATIC_SCROLL_MS = 150

function readMetrics(el: HTMLElement): ScrollMetrics {
  return { scrollHeight: el.scrollHeight, scrollTop: el.scrollTop, clientHeight: el.clientHeight }
}

type ConversationTurn =
  | { kind: 'pending-user'; id: string; messageId: string }
  | { kind: 'transcript'; id: string; messageId: string; replyIds: string[] }
  | { kind: 'canonical'; id: string; turn: TurnProjection }

function PendingUserTurn({ messageId }: { messageId: string }) {
  const entity = useMessageStore((state) => state.entities[messageId])
  if (!entity || entity.messageType !== 'user') return null
  return (
    <div className="conversation-turn" data-turn-kind="pending-user">
      <div className="conversation-user-sticky" data-turn-slot="user">
        <UserMessageBlock entity={entity} />
      </div>
      <div className="conversation-body-frame conversation-turn-content flex flex-col">
        <div data-turn-slot="trace">
          <PendingTurnHeader startedAt={entity.timestamp} />
        </div>
      </div>
    </div>
  )
}

export function ConversationMessageList({ conversationId, selectedAgentMessageId, enableAgentDetail = true }: ConversationMessageListProps) {
  const canonicalTurns = useCanonicalTurns(conversationId)
  const processing = useRoomProcessing(conversationId)
  const storeVersion = useMessageStore(s => s.version)
  const turns = useMemo<ConversationTurn[]>(() => {
    const { entities, orderedIds } = useMessageStore.getState()
    const canonicalByUser = new Map(canonicalTurns.map((turn) => [turn.userMessageId, turn]))
    const included = new Set<string>()
    const ordered: ConversationTurn[] = []
    // Canonical roots own their Turns. A user message without one is either a
    // just-sent message waiting for its root (pending shell) or history from
    // before the canonical lifecycle (read-only transcript).
    const sources = buildTranscriptTurnSources(conversationId, entities, orderedIds)
    sources.forEach((source, index) => {
      const canonical = canonicalByUser.get(source.userMessageId)
      if (canonical) {
        if (!included.has(canonical.id)) {
          included.add(canonical.id)
          ordered.push({ kind: 'canonical', id: canonical.id, turn: canonical })
        }
      } else if (processing && index === sources.length - 1 && source.replyIds.length === 0) {
        ordered.push({ kind: 'pending-user', id: `pending:${source.userMessageId}`, messageId: source.userMessageId })
      } else {
        ordered.push({
          kind: 'transcript',
          id: `transcript:${source.userMessageId}`,
          messageId: source.userMessageId,
          replyIds: source.replyIds,
        })
      }
    })
    for (const turn of canonicalTurns) {
      if (!included.has(turn.id)) ordered.push({ kind: 'canonical', id: turn.id, turn })
    }
    return ordered
    // storeVersion re-derives the Turns whenever the message store changes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [canonicalTurns, conversationId, processing, storeVersion])
  const scrollRef = useRef<HTMLDivElement>(null)
  const frameRef = useRef<HTMLDivElement>(null)
  const [showScrollBtn, setShowScrollBtn] = useState(false)
  const [hasNewContent, setHasNewContent] = useState(false)

  const localSendSeq = useLocalSendSeq(conversationId)
  const initialHydrationSeq = useInitialHydrationSeq(conversationId)
  const prevConversationIdRef = useRef(conversationId)
  const prevHydrationSeqRef = useRef(0)
  const initialScrollResolvedRef = useRef(false)

  const userPausedRef = useRef(false)
  const tailFollowRef = useRef(false)
  const pinnedToContentEndRef = useRef(false)
  const programmaticScrollRef = useRef(false)
  const suppressScrollUntilRef = useRef(0)
  const prevMetricsRef = useRef<ScrollMetrics | null>(null)
  const primarySurfaceRef = useRef<HTMLDivElement>(null)

  const markProgrammaticScroll = useCallback(() => {
    if (!tailFollowRef.current) return
    suppressScrollUntilRef.current = performance.now() + PROGRAMMATIC_SCROLL_MS
  }, [])

  const stopTailFollow = useCallback(() => {
    if (!tailFollowRef.current && userPausedRef.current) return
    tailFollowRef.current = false
    userPausedRef.current = true
    programmaticScrollRef.current = false
    suppressScrollUntilRef.current = 0
    setShowScrollBtn(true)
  }, [])

  const lastTurn = turns[turns.length - 1]
  const primaryStreamMessageId = lastTurn?.kind === 'canonical'
    ? lastTurn.turn.currentAssistant?.messageId
    : undefined
  const lastUserMessageId = lastTurn?.kind === 'canonical'
    ? lastTurn.turn.userMessageId
    : lastTurn?.messageId
  const turnLive = lastTurn?.kind === 'canonical'
    ? lastTurn.turn.state === 'active'
    : lastTurn?.kind === 'pending-user' && processing
  const hydratedFromDb = useMessageStore(s => s.hydratedFromDb)

  const saveConversationScroll = useCallback((targetConversationId: string) => {
    const el = scrollRef.current
    if (!el) return
    useRoomUiStore.getState().saveConversationScroll(
      targetConversationId,
      readConversationScrollSnapshot(el),
    )
  }, [])

  useScrollUserMessageOnSend({
    scrollRef,
    frameRef,
    lastUserMessageId,
    localSendSeq,
    programmaticScrollRef,
    userPausedRef,
    tailFollowRef,
  })

  usePrimaryStreamScroll({
    scrollRef,
    primarySurfaceRef,
    primaryStreamMessageId,
    tailFollowRef,
    programmaticScrollRef,
    markProgrammaticScroll,
    enabled: turnLive || Boolean(primaryStreamMessageId),
  })

  const scrollToBottom = useCallback((behavior: ScrollBehavior = 'smooth') => {
    const el = scrollRef.current
    if (!el) return
    programmaticScrollRef.current = true
    markProgrammaticScroll()
    const targetScrollTop = contentEndScrollTop(el)
    scrollToContentEnd(el, behavior)
    userPausedRef.current = false
    tailFollowRef.current = true
    pinnedToContentEndRef.current = true
    setHasNewContent(false)
    setShowScrollBtn(false)
    useRoomUiStore.getState().saveConversationScroll(conversationId, { scrollTop: targetScrollTop, atBottom: true })

    if (behavior !== 'smooth') return

    const saveWhenSettled = () => {
      if (!isNearContentEnd(el)) return
      useRoomUiStore.getState().saveConversationScroll(conversationId, {
        scrollTop: el.scrollTop,
        atBottom: true,
      })
    }

    if ('onscrollend' in el) {
      el.addEventListener('scrollend', saveWhenSettled, { once: true })
      return
    }

    requestAnimationFrame(() => requestAnimationFrame(saveWhenSettled))
  }, [conversationId, markProgrammaticScroll])

  useEffect(() => {
    return () => {
      saveConversationScroll(conversationId)
    }
  }, [conversationId, saveConversationScroll])

  useLayoutEffect(() => {
    const el = scrollRef.current
    if (!el) return

    if (conversationId !== prevConversationIdRef.current) {
      prevConversationIdRef.current = conversationId
      prevHydrationSeqRef.current = 0
      initialScrollResolvedRef.current = false
      userPausedRef.current = false
      tailFollowRef.current = false
      pinnedToContentEndRef.current = false
      programmaticScrollRef.current = false
      suppressScrollUntilRef.current = 0
      prevMetricsRef.current = null
      setShowScrollBtn(false)
      setHasNewContent(false)
    }

    if (initialHydrationSeq !== prevHydrationSeqRef.current) {
      prevHydrationSeqRef.current = initialHydrationSeq
      initialScrollResolvedRef.current = true

      if (!userPausedRef.current && !shouldSkipInitialHydrationScrollRestore(localSendSeq)) {
        const saved = useRoomUiStore.getState().getConversationScroll(conversationId)
        programmaticScrollRef.current = true
        restoreConversationScrollWithRetry(el, saved, (result) => {
          userPausedRef.current = result === 'restored-position'
          const metrics = readMetrics(el)
          prevMetricsRef.current = metrics
          pinnedToContentEndRef.current = isNearContentEnd(el)
          tailFollowRef.current = turnLive && pinnedToContentEndRef.current
          setShowScrollBtn(!pinnedToContentEndRef.current)
          requestAnimationFrame(() => {
            programmaticScrollRef.current = false
          })
        })
      } else {
        prevMetricsRef.current = readMetrics(el)
        requestAnimationFrame(() => {
          programmaticScrollRef.current = false
        })
      }

      return
    }

    if (!initialScrollResolvedRef.current) {
      prevMetricsRef.current = readMetrics(el)
      return
    }

    const prev = prevMetricsRef.current
    const curr = readMetrics(el)

    if (prev && curr.scrollHeight !== prev.scrollHeight) {
      if (turnLive) {
        if (tailFollowRef.current) {
          programmaticScrollRef.current = true
          markProgrammaticScroll()
          scrollToContentEnd(el, 'auto')
          prevMetricsRef.current = readMetrics(el)
          return
        }

        setHasNewContent(true)
        setShowScrollBtn(true)
        prevMetricsRef.current = curr
        return
      }

      if (!userPausedRef.current && pinnedToContentEndRef.current) {
        scrollToBottom('auto')
        prevMetricsRef.current = readMetrics(el)
        return
      }

      setHasNewContent(true)
      setShowScrollBtn(true)
    }

    prevMetricsRef.current = curr
  }, [conversationId, storeVersion, initialHydrationSeq, localSendSeq, scrollToBottom, turnLive, markProgrammaticScroll])

  useEffect(() => {
    const el = scrollRef.current
    if (!el) return

    const onScroll = () => {
      const m = readMetrics(el)
      const atBottom = isNearContentEnd(el)
      pinnedToContentEndRef.current = atBottom

      if (turnLive) {
        const prevTop = prevMetricsRef.current?.scrollTop ?? null
        const userScrolledUp = prevTop !== null && m.scrollTop < prevTop - 1
        const inferFromScrollPosition = performance.now() >= suppressScrollUntilRef.current

        if (userScrolledUp && !programmaticScrollRef.current) {
          stopTailFollow()
        } else if (inferFromScrollPosition && atBottom) {
          tailFollowRef.current = true
          userPausedRef.current = false
          setShowScrollBtn(false)
          setHasNewContent(false)
        } else if (inferFromScrollPosition) {
          userPausedRef.current = !tailFollowRef.current
          setShowScrollBtn(!tailFollowRef.current)
        }

        if (inferFromScrollPosition) {
          useRoomUiStore.getState().saveConversationScroll(conversationId, { scrollTop: m.scrollTop, atBottom })
        }

        if (programmaticScrollRef.current) {
          programmaticScrollRef.current = false
        }

        prevMetricsRef.current = m
        return
      }

      const next = resolveScrollStateAfterEvent({
        atBottom,
        programmatic: programmaticScrollRef.current,
        previousScrollTop: prevMetricsRef.current?.scrollTop ?? null,
        currentScrollTop: m.scrollTop,
        wasPaused: userPausedRef.current,
      })

      programmaticScrollRef.current = next.programmatic
      userPausedRef.current = next.paused
      if (next.clearNewContent) setHasNewContent(false)

      setShowScrollBtn(!atBottom)
      prevMetricsRef.current = m

      if (!programmaticScrollRef.current) {
        useRoomUiStore.getState().saveConversationScroll(conversationId, { scrollTop: m.scrollTop, atBottom })
      }
    }

    const onWheel = (e: WheelEvent) => {
      if (!turnLive) return
      if (tailFollowRef.current && e.deltaY !== 0) {
        stopTailFollow()
        return
      }
      if (e.deltaY > 0 && isNearContentEnd(el)) {
        tailFollowRef.current = true
        userPausedRef.current = false
        setShowScrollBtn(false)
        setHasNewContent(false)
      }
    }

    const onTouchMove = () => {
      if (!turnLive || !tailFollowRef.current) return
      stopTailFollow()
    }

    el.addEventListener('scroll', onScroll, { passive: true })
    el.addEventListener('wheel', onWheel, { passive: true })
    el.addEventListener('touchmove', onTouchMove, { passive: true })
    return () => {
      el.removeEventListener('scroll', onScroll)
      el.removeEventListener('wheel', onWheel)
      el.removeEventListener('touchmove', onTouchMove)
    }
  }, [conversationId, turnLive, stopTailFollow])

  const handleOpenAgentDetail = useCallback((messageId: string) => {
    const store = useRoomUiStore.getState()
    if (selectedAgentMessageId === messageId) {
      store.closeAgentDetail(conversationId)
      return
    }
    store.openAgentDetail(conversationId, messageId)
  }, [conversationId, selectedAgentMessageId])

  return (
    <div className="relative h-full bg-background">
      <div className="conversation-top-cover" />

      <div
        ref={scrollRef}
        className="conversation-scroll-area h-full overflow-y-auto overscroll-contain"
      >
        <div className="conversation-gutter">
          <div
            ref={frameRef}
            className="conversation-frame"
            data-hydrated={hydratedFromDb || undefined}
            style={{ paddingTop: 'var(--conversation-sticky-top)', paddingBottom: 'calc(var(--conversation-dock-height, 120px) + 24px)' }}
          >
            {turns.map((entry, index) => (
              entry.kind === 'canonical' ? (
                <CanonicalTurnRenderer
                  key={entry.id}
                  turn={entry.turn}
                  selectedAgentMessageId={selectedAgentMessageId}
                  onOpenAgentDetail={enableAgentDetail ? handleOpenAgentDetail : undefined}
                  primarySurfaceRef={index === turns.length - 1 ? primarySurfaceRef : undefined}
                  isLastTurn={index === turns.length - 1}
                />
              ) : entry.kind === 'transcript' ? (
                <TranscriptTurn key={entry.id} userMessageId={entry.messageId} replyIds={entry.replyIds} />
              ) : (
                <PendingUserTurn
                  key={entry.id}
                  messageId={entry.messageId}
                />
              )
            ))}
            <div aria-hidden data-content-end />
            <div
              aria-hidden
              data-scroll-spacer
              style={{ height: FOCUS_SCROLL_MIN_SPACER_PX, flexShrink: 0 }}
            />
          </div>
        </div>
      </div>

      <ScrollToBottomButton
        visible={showScrollBtn}
        hasNewContent={hasNewContent}
        onClick={() => scrollToBottom('smooth')}
      />
    </div>
  )
}

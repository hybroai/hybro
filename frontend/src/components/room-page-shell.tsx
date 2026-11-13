'use client'

import React, { useCallback, useEffect, useLayoutEffect, useMemo, useRef } from 'react'
import { useQuery } from '@tanstack/react-query'
import type { AgentGroup, TargetModeDispatchInput } from '@/lib/types/agent-group'
import type { QuoteData } from '@/lib/types/quote'
import type { ChatMode } from '@/lib/types/chat-mode'
import type { RoomChatSubmit } from '@/components/room-chat-input'
import { ConversationMessageList } from '@/components/conversation/ConversationMessageList'
import { ComposerShell } from '@/components/composer/ComposerShell'
import type { HitlBatchAnswer } from '@/components/composer/HitlResponseBar'
import { useTextSelectionQuote } from '@/hooks/useTextSelectionQuote'
import { AgentResponseDetailPane } from '@/components/conversation/AgentResponseDetailPane'
import {
  ResizableHandle,
  ResizablePanel,
  ResizablePanelGroup,
} from '@/components/ui/resizable'
import { useGroupRef } from 'react-resizable-panels'
import { Sheet, SheetContent, SheetDescription, SheetTitle } from '@/components/ui/sheet'
import { useSidebar } from '@/components/ui/sidebar'
import { useMessageStore } from '@/stores/message-store'
import { useStreamBuffer } from '@/hooks/useStreamBuffer'
import { useRoomUiStore, useSelectedAgentMessageId } from '@/stores/room-ui-store'
import { selectAgentResponseDetail } from '@/lib/selectors/select-agent-response-detail'
import {
  getAgentTheme,
  type AgentResponseDetail,
} from '@/lib/selectors/conversation-types'
import { mapResultDisplayProps } from '@/lib/room-timeline/map-result-display'
import { useCanonicalTurns } from '@/stores/turn-store'
import {
  canonicalAgentCallParts,
  canonicalArtifactData,
  parseCanonicalCardIdentity,
} from '@/lib/api/agent-call-detail'
import { canonicalAgentCallDetailQueryOptions } from '@/lib/api/canonical-agent-call-detail-query'
import type { MessageEntity } from '@/stores/message-store/types'
import type { TurnActivityItem } from '@/lib/turn-lifecycle/types'
import { ApiError } from '@/lib/api-client'
import { isTerminalState } from '@/lib/types/sse'

function isAgentExecutionWithCallId(
  item: TurnActivityItem,
  publicCallId: string,
): item is Extract<TurnActivityItem, { kind: 'tool' }> & { executionKind: 'agent' } {
  return item.kind === 'tool' && item.executionKind === 'agent' && item.toolCallId === publicCallId
}

const EMPTY_ENTITIES: Record<string, MessageEntity> = {}
const EMPTY_ORDERED_IDS: string[] = []
const DETAIL_PANE_QUERY = '(min-width: 1024px)'

const DETAIL_PANE_LAYOUT = {
  'conversation-primary-panel': 50,
  'conversation-detail-panel': 50,
}

function canShowDetailPane(): boolean {
  if (typeof window === 'undefined' || !window.matchMedia) return true
  return window.matchMedia(DETAIL_PANE_QUERY).matches
}

function useCanShowDetailPane(): boolean {
  const [canShow, setCanShow] = React.useState(canShowDetailPane)

  useEffect(() => {
    if (typeof window === 'undefined' || !window.matchMedia) return
    const media = window.matchMedia(DETAIL_PANE_QUERY)
    const handleChange = () => setCanShow(media.matches)
    handleChange()
    media.addEventListener('change', handleChange)
    return () => media.removeEventListener('change', handleChange)
  }, [])

  return canShow
}

export interface GroupManagementAdapter {
  groups: AgentGroup[]
  loadingGroups: boolean
  selectedGroup: string
  selectedGroupName?: string
  roomMembershipLabel?: string
  resolvedTargetMode: TargetModeDispatchInput
  handleGroupChange: (groupId: string) => void
  handleCreateGroup: () => void
  handleEditGroup: (group: AgentGroup) => void
  handleDeleteGroup: (group: AgentGroup) => void
}

export interface QuoteState {
  quote: QuoteData | null
  setQuote: (data: QuoteData) => void
  clearQuote: () => void
}

export interface TimelineAdapter {
  conversationId: string
  getToken?: () => Promise<string | null>
  onSendMessage: RoomChatSubmit
  onCancelProcessing: () => void
  onRespondToHitlBatch: (interactionId: string, answers: HitlBatchAnswer[], clientRequestId?: string) => Promise<void>
  onCancelHitl: (requestId: string, interactionId?: string) => Promise<void>
  onRefreshHitl: () => Promise<void>
  onChatModeChange: (mode: ChatMode) => void
  isSending: boolean
  isProcessing: boolean
  isCancelling: boolean
  agents: { id: string; name: string; iconUrl?: string }[]
  roomAgentIds: string[]
  groupManagement: GroupManagementAdapter
  quoteState: QuoteState
  chatMode: ChatMode
  externalValue?: string
  onExternalValueConsumed?: () => void
}

interface RoomPageShellProps {
  adapter: TimelineAdapter
}

export function RoomPageShell({ adapter }: RoomPageShellProps) {
  const canShowAgentDetail = useCanShowDetailPane()
  const {
    open: sidebarOpen,
    openMobile: sidebarOpenMobile,
    setOpen: setSidebarOpen,
    setOpenMobile: setSidebarOpenMobile,
    isMobile: isMobileSidebar,
  } = useSidebar()
  const conversationGroupRef = useGroupRef()
  const prevSelectedMessageIdRef = useRef<string | undefined>(undefined)
  const sidebarSnapshotRef = useRef<{ desktopOpen: boolean; mobileOpen: boolean } | null>(null)
  const selectedMessageId = useSelectedAgentMessageId(adapter.conversationId)
  const entities = useMessageStore(s => selectedMessageId ? s.entities : EMPTY_ENTITIES)
  const orderedIds = useMessageStore(s => selectedMessageId ? s.orderedIds : EMPTY_ORDERED_IDS)
  const streamBuffer = useStreamBuffer(selectedMessageId)

  const baseDetail = useMemo(() => {
    if (!selectedMessageId) return null
    return selectAgentResponseDetail(
      adapter.conversationId,
      selectedMessageId,
      entities,
      orderedIds,
      streamBuffer,
    )
  }, [adapter.conversationId, selectedMessageId, entities, orderedIds, streamBuffer])

  // Canonical Agent Cards are folded from execution events, not MessageStore
  // entities. Build the detail shell from the Turn projection so the private
  // detail fetch opens the pane with the durable request/status facts. It
  // wins over a MessageStore entity with the same id: snapshot HITL recovery
  // anchors questions on a status-less shell entity under the card id.
  const canonicalTurns = useCanonicalTurns(adapter.conversationId)
  const canonicalCardDetail = useMemo<AgentResponseDetail | null>(() => {
    if (!selectedMessageId) return null
    const identity = parseCanonicalCardIdentity(selectedMessageId)
    if (!identity) return null
    for (const turn of canonicalTurns) {
      if (turn.runId !== identity.runId) continue
      const item = turn.activity.find((activity) => (
        isAgentExecutionWithCallId(activity, identity.publicCallId)
      ))
      if (!item) continue
      const agentName = item.targetName ?? 'Unknown agent'
      const userEntity = entities[turn.userMessageId] ?? null
      const status = item.status === 'completed'
        ? 'completed'
        : item.status === 'suspended'
          ? 'awaiting_input'
          : item.status === 'failed'
            ? 'failed'
            : item.status === 'canceled'
              ? 'canceled'
              : 'working'
      return {
        messageId: selectedMessageId,
        agentName,
        display: mapResultDisplayProps({
          agentId: undefined,
          agentName,
          agentSource: undefined,
          messageId: selectedMessageId,
          status,
          content: '',
          artifacts: [],
          isSummaryAgent: false,
          isEphemeral: false,
        }, false),
        taskDescription: item.requestSummary,
        theme: getAgentTheme(undefined, agentName),
        content: '',
        isStreaming: item.status === 'running',
        artifacts: [],
        requestMessage: userEntity,
      }
    }
    return null
  }, [canonicalTurns, entities, selectedMessageId])

  const canonicalCardTerminal = useMemo(() => {
    if (!selectedMessageId) return false
    const identity = parseCanonicalCardIdentity(selectedMessageId)
    if (!identity) return false
    const turn = canonicalTurns.find((item) => item.runId === identity.runId)
    if (!turn) return false
    const item = turn.activity.find((activity) => (
      isAgentExecutionWithCallId(activity, identity.publicCallId)
    ))
    return item != null && ['completed', 'failed', 'canceled'].includes(item.status)
  }, [canonicalTurns, selectedMessageId])

  const effectiveBaseDetail = canonicalCardDetail ?? baseDetail
  const canonicalTerminal = parseCanonicalCardIdentity(selectedMessageId ?? '') != null
    && (canonicalCardTerminal
      || (baseDetail?.taskStatus != null && isTerminalState(baseDetail.taskStatus)))
  const privateDetailQuery = useQuery(canonicalAgentCallDetailQueryOptions(
    adapter.conversationId,
    selectedMessageId,
    adapter.getToken,
    canonicalTerminal,
  ))

  const detail = useMemo(() => {
    if (!effectiveBaseDetail) return null
    if (!canonicalTerminal) return effectiveBaseDetail
    if (privateDetailQuery.isPending || privateDetailQuery.isFetching) {
      return {
        ...effectiveBaseDetail,
        content: '',
        parts: undefined,
        artifacts: [],
        isStreaming: true,
      }
    }
    if (privateDetailQuery.isError) {
      const error = privateDetailQuery.error
      return {
        ...effectiveBaseDetail,
        content: '',
        parts: undefined,
        artifacts: [],
        isStreaming: false,
        taskError: error instanceof ApiError && error.status === 404
          ? 'Private details are still being prepared. Close and reopen this response to retry.'
          : error instanceof Error ? error.message : 'Private output unavailable',
      }
    }
    const response = privateDetailQuery.data
    return {
      ...effectiveBaseDetail,
      // Typed A2A parts carry the private output.
      content: '',
      parts: response ? canonicalAgentCallParts(response.parts) : undefined,
      artifacts: canonicalArtifactData(response?.artifacts ?? []),
      isStreaming: false,
    }
  }, [canonicalTerminal, effectiveBaseDetail, privateDetailQuery])

  const restoreSidebar = useCallback(() => {
    const snapshot = sidebarSnapshotRef.current
    if (!snapshot) return
    if (isMobileSidebar) {
      setSidebarOpenMobile(snapshot.mobileOpen)
    } else {
      setSidebarOpen(snapshot.desktopOpen)
    }
    sidebarSnapshotRef.current = null
  }, [isMobileSidebar, setSidebarOpen, setSidebarOpenMobile])

  const prevConversationIdRef = useRef(adapter.conversationId)
  useEffect(() => {
    if (adapter.conversationId !== prevConversationIdRef.current) {
      restoreSidebar()
      useRoomUiStore.getState().closeAgentDetail(prevConversationIdRef.current)
      prevConversationIdRef.current = adapter.conversationId
      prevSelectedMessageIdRef.current = undefined
    }
  }, [adapter.conversationId, restoreSidebar])

  useLayoutEffect(() => {
    if (selectedMessageId && !detail) {
      restoreSidebar()
      useRoomUiStore.getState().closeAgentDetail(adapter.conversationId)
    }
  }, [selectedMessageId, detail, adapter.conversationId, restoreSidebar])

  const handleCloseDetail = useCallback(() => {
    prevSelectedMessageIdRef.current = undefined
    restoreSidebar()
    useRoomUiStore.getState().closeAgentDetail(adapter.conversationId)
  }, [adapter.conversationId, restoreSidebar])

  useLayoutEffect(() => {
    if (!selectedMessageId) return

    if (sidebarSnapshotRef.current !== null) return

    sidebarSnapshotRef.current = {
      desktopOpen: sidebarOpen,
      mobileOpen: sidebarOpenMobile,
    }
    if (isMobileSidebar) {
      setSidebarOpenMobile(false)
    } else {
      setSidebarOpen(false)
    }
  }, [
    selectedMessageId,
    sidebarOpen,
    sidebarOpenMobile,
    isMobileSidebar,
    setSidebarOpen,
    setSidebarOpenMobile,
  ])

  const dockRef = useRef<HTMLDivElement>(null)
  const primaryRef = useRef<HTMLDivElement>(null)
  const workspaceHostRef = useRef<HTMLDivElement>(null)

  useTextSelectionQuote(workspaceHostRef, adapter.quoteState.setQuote)
  useLayoutEffect(() => {
    const dock = dockRef.current
    const primary = primaryRef.current
    if (!dock || !primary) return

    const update = () => {
      primary.style.setProperty('--conversation-dock-height', `${dock.offsetHeight}px`)
    }
    update()

    const ro = new ResizeObserver(update)
    ro.observe(dock)
    return () => ro.disconnect()
  }, [])

  const desktopDetail = canShowAgentDetail ? detail : null

  useLayoutEffect(() => {
    if (!desktopDetail || !selectedMessageId) {
      if (!selectedMessageId) prevSelectedMessageIdRef.current = undefined
      return
    }
    if (prevSelectedMessageIdRef.current === selectedMessageId) return

    const group = conversationGroupRef.current
    if (group) {
      try {
        group.setLayout(DETAIL_PANE_LAYOUT)
      } catch {
        // Group may not be measured yet in non-layout environments.
      }
    }
    prevSelectedMessageIdRef.current = selectedMessageId
  }, [desktopDetail, selectedMessageId, conversationGroupRef])

  const primaryContent = (
    <div ref={primaryRef} className="conversation-primary">
      <main className="flex-1 overflow-hidden">
        <ConversationMessageList
          conversationId={adapter.conversationId}
          selectedAgentMessageId={selectedMessageId}
          enableAgentDetail
        />
      </main>
      <div ref={dockRef} className="conversation-input-dock conversation-gutter">
        <div className="conversation-frame">
          <ComposerShell adapter={adapter} />
        </div>
      </div>
    </div>
  )

  return (
    <>
      <div ref={workspaceHostRef} className="conversation-workspace-host relative h-full min-h-0">
        <ResizablePanelGroup
          id={desktopDetail ? 'conversation-resizable-workspace' : undefined}
          groupRef={conversationGroupRef}
          orientation="horizontal"
          defaultLayout={desktopDetail ? DETAIL_PANE_LAYOUT : undefined}
          className="conversation-workspace conversation-workspace-resizable"
          data-detail-open={desktopDetail ? 'true' : undefined}
        >
          <ResizablePanel
            id="conversation-primary-panel"
            defaultSize={desktopDetail ? '50%' : '100%'}
            minSize={desktopDetail ? '15%' : '100%'}
            maxSize={desktopDetail ? '80%' : '100%'}
            className="conversation-resizable-panel"
            data-testid="conversation-primary-panel"
          >
            {primaryContent}
          </ResizablePanel>
          {desktopDetail && (
            <>
              <ResizableHandle
                id="conversation-detail-resize-handle"
                className="conversation-detail-resize-handle"
              />
              <ResizablePanel
                id="conversation-detail-panel"
                defaultSize="50%"
                minSize="20%"
                maxSize="85%"
                className="conversation-detail-resizable-panel"
                data-testid="conversation-detail-panel"
              >
                <AgentResponseDetailPane detail={desktopDetail} onClose={handleCloseDetail} />
              </ResizablePanel>
            </>
          )}
        </ResizablePanelGroup>
      </div>

      {!canShowAgentDetail && (
        <Sheet open={!!detail} onOpenChange={(open) => { if (!open) handleCloseDetail() }}>
          <SheetContent
            side="bottom"
            className="h-[85dvh] p-0 flex flex-col gap-0 rounded-t-xl overflow-hidden sm:min-w-[480px] sm:max-w-2xl sm:inset-x-auto sm:left-1/2 sm:-translate-x-1/2"
            data-mobile-sheet
          >
            <SheetTitle className="sr-only">Agent response detail</SheetTitle>
            <SheetDescription className="sr-only">View the full response from the agent</SheetDescription>
            {detail && (
              <AgentResponseDetailPane detail={detail} onClose={handleCloseDetail} />
            )}
          </SheetContent>
        </Sheet>
      )}
    </>
  )
}

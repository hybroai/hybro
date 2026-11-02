'use client'

import { memo, type Ref, useMemo } from 'react'
import { useQueries } from '@tanstack/react-query'
import { useMessageStore } from '@/stores/message-store'
import type { TurnProjection } from '@/lib/turn-lifecycle/types'
import type { AgentResultViewModel, TurnViewModel } from '@/lib/room-timeline/types'
import { MarkdownContent } from '@/components/markdown-content'
import { Separator } from '@/components/ui/separator'
import { specificPublicAgentName } from '@/lib/agent-display-name'
import { UserMessageBlock } from './UserMessageBlock'
import { CanonicalTurnTrace } from './CanonicalTurnTrace'
import { AgentIndex } from './AgentIndex'
import { ArtifactList } from '@/components/artifact-list'
import { useAuth } from '@/lib/auth'
import { canonicalArtifactData } from '@/lib/api/agent-call-detail'
import type { ArtifactData } from '@/stores/message-store/types'
import { canonicalAgentCallDetailQueryOptions } from '@/lib/api/canonical-agent-call-detail-query'
import { Button } from '@/components/ui/button'
import { failedAddressedAgents, type FailedAddressedAgent } from '@/lib/room-timeline/failed-addressed-agents'

/**
 * Derive Agent Cards from the folded canonical executions.
 *
 * One Agent Execution (execution_kind == "agent") becomes one card; the
 * card identity is the opaque public call id, which is also the key used by
 * the authenticated detail fetch. No Legacy ``task_*``/MessageStore data is
 * consumed for card state.
 */
function canonicalAgentResults(turn: TurnProjection): AgentResultViewModel[] {
  return turn.activity.flatMap((item) => {
    if (item.kind !== 'tool' || item.executionKind !== 'agent') return []
    // The execution target carries the exact base Agent name. A missing or
    // generic target must never be replaced by a skill-qualified Trace label.
    const agentName = item.targetName
      ? (specificPublicAgentName(item.targetName) ?? 'Unknown agent')
      : 'Unknown agent'
    const status: AgentResultViewModel['status'] = item.status === 'completed'
      ? 'completed'
      : item.status === 'suspended'
        ? 'awaiting_input'
        : item.status === 'failed'
          ? 'failed'
          : item.status === 'canceled'
            ? 'canceled'
            : 'working'
    return [{
      agentId: undefined,
      agentName,
      agentSource: undefined,
      messageId: `orchestrator:${turn.runId}:${item.toolCallId}`,
      clientRequestId: turn.clientRequestId,
      status,
      executionStatus: item.status === 'suspended' ? 'awaiting_input' : item.status,
      content: '',
      artifacts: [],
      taskStatusMessage: item.requestSummary || undefined,
      failureDetail: status === 'failed' ? (item.errorMessage ?? item.errorCode) : undefined,
      isSummaryAgent: false,
      isEphemeral: false,
    }]
  })
}

function agentIndexAdapter(turn: TurnProjection, results: AgentResultViewModel[]): TurnViewModel {
  return {
    id: turn.id,
    conversationId: turn.conversationId,
    userMessageId: turn.userMessageId,
    userContent: '',
    userAttachments: [],
    timestamp: turn.startedAt,
    status: turn.state === 'canceled' ? 'partial' : turn.state,
    agentResults: results,
    activeAgentIds: results.filter((result) => result.status === 'working').flatMap((result) => result.agentId ?? []),
    isSupervisorTurn: false,
    displayMode: turn.state === 'awaiting_input' ? 'awaiting_input' : turn.state === 'active' ? 'working' : 'parallel_results',
    phase: turn.state === 'completed' ? 'completed' : 'answering',
    finalAnswer: turn.state === 'awaiting_input'
      ? { kind: 'hitl', label: 'Needs input' }
      : { kind: 'deterministic_done', label: 'Combined agent responses' },
  }
}

function useCanonicalTurnArtifacts(turn: TurnProjection): {
  artifacts: ArtifactData[]
  loadFailed: boolean
  retry: () => void
} {
  const { getToken } = useAuth()
  const detailMessageIds = useMemo(() => turn.activity.flatMap((item) => (
    item.kind === 'tool'
    && item.status === 'completed'
    && item.detailAvailable
      ? [`orchestrator:${turn.runId}:${item.toolCallId}`]
      : []
  )), [turn.activity, turn.runId])
  const enabled = turn.state === 'completed'
  const queries = useQueries({
    queries: detailMessageIds.map((messageId) => (
      canonicalAgentCallDetailQueryOptions(
        turn.conversationId,
        messageId,
        getToken,
        enabled,
      )
    )),
  })
  const unique = new Map<string, ArtifactData>()
  for (const query of queries) {
    for (const artifact of canonicalArtifactData(query.data?.artifacts ?? [])) {
      unique.set(artifact.artifactId, artifact)
    }
  }
  return {
    artifacts: [...unique.values()],
    loadFailed: queries.some((query) => query.isError),
    retry: () => {
      void Promise.all(
        queries.filter((query) => query.isError).map((query) => query.refetch()),
      )
    },
  }
}

function FailedAddressedAgentNotice({ agents }: { agents: FailedAddressedAgent[] }) {
  return (
    <div className="canonical-failed-agent-notice mb-3 rounded-lg border px-3 py-2 text-sm" role="note" data-failed-addressed-agents>
      {agents.map((agent) => (
        <p key={agent.name}>
          <strong>{agent.name} failed</strong>
          {agent.reason ? `: ${agent.reason}` : '.'}
        </p>
      ))}
      <p>The reply below is from Hybro, not from {agents.map((agent) => agent.name).join(' or ')}.</p>
    </div>
  )
}

function CanonicalFinalAnswer({
  turn,
  userText,
  surfaceRef,
}: {
  turn: TurnProjection
  userText: string
  surfaceRef?: Ref<HTMLDivElement>
}) {
  const { artifacts, loadFailed, retry } = useCanonicalTurnArtifacts(turn)
  // Awaiting-input content lives exclusively in the composer interaction UI.
  // The body area stays empty; the Turn Trace records the event and its
  // "Waiting for input" state.
  if (turn.state === 'awaiting_input') {
    return null
  }
  const assistant = turn.currentAssistant ?? turn.finalAnswer
  if (assistant?.text || artifacts.length > 0 || loadFailed) {
    const streaming = assistant?.status === 'streaming'
    // Only a final answer the backend has stored (finalCommitted) is an agent
    // message it accepts as a synthesis quote source; earlier text is not.
    const failedAgents = failedAddressedAgents(turn, userText)
    const quotable =
      turn.finalCommitted &&
      assistant !== undefined &&
      assistant === turn.finalAnswer &&
      assistant.status === 'completed'
    return (
      <div
        ref={surfaceRef}
        className="canonical-final-answer conversation-content-body"
        data-canonical-final-answer
        data-message-id={assistant?.messageId ?? turn.id}
        data-quote-message-id={quotable ? assistant.messageId : undefined}
        data-quote-agent-name={quotable ? 'HYBRO AI' : undefined}
        data-quote-source-kind={quotable ? 'synthesis' : undefined}
        data-streaming={streaming || undefined}
        aria-live={streaming ? 'polite' : undefined}
      >
        {failedAgents.length > 0 ? <FailedAddressedAgentNotice agents={failedAgents} /> : null}
        {assistant?.text ? (
          <MarkdownContent className="conversation-markdown-body" content={assistant.text} />
        ) : null}
        <ArtifactList artifacts={artifacts} />
        {loadFailed ? (
          <div className="mt-2 flex items-center gap-2 text-sm text-muted-foreground" role="alert">
            <span>Generated files could not be loaded.</span>
            <Button type="button" variant="outline" size="sm" onClick={retry}>
              Retry
            </Button>
          </div>
        ) : null}
      </div>
    )
  }
  if (turn.state === 'failed') {
    return (
      <div ref={surfaceRef} className="canonical-terminal-summary" role="alert" data-canonical-final-answer>
        <strong>Request failed</strong>
        {turn.terminalSummary ? <p>{turn.terminalSummary}</p> : null}
      </div>
    )
  }
  if (turn.state === 'canceled') {
    return (
      <div ref={surfaceRef} className="canonical-terminal-summary" data-canonical-final-answer>
        <strong>Request stopped</strong>
      </div>
    )
  }
  return null
}

interface CanonicalTurnRendererProps {
  turn: TurnProjection
  selectedAgentMessageId?: string
  onOpenAgentDetail?: (messageId: string) => void
  primarySurfaceRef?: Ref<HTMLDivElement>
  isLastTurn?: boolean
}

function CanonicalTurnRendererComponent({
  turn,
  selectedAgentMessageId,
  onOpenAgentDetail,
  primarySurfaceRef,
  isLastTurn = false,
}: CanonicalTurnRendererProps) {
  const userEntity = useMessageStore((state) => state.entities[turn.userMessageId])
  const results = canonicalAgentResults(turn)
  const indexTurn = agentIndexAdapter(turn, results)
  const assistant = turn.currentAssistant ?? turn.finalAnswer
  const hasFollowingContent = turn.state !== 'awaiting_input'
    && (Boolean(assistant?.text)
      || turn.state === 'failed'
      || turn.state === 'canceled'
      || results.length > 0)

  return (
    <div className="conversation-turn" data-turn-kind="canonical" data-turn-id={turn.id}>
      {userEntity?.messageType === 'user' ? (
        <div className="conversation-user-sticky" data-turn-slot="user">
          <UserMessageBlock entity={userEntity} />
        </div>
      ) : null}

      <div className="conversation-body-frame conversation-turn-content flex flex-col">
        <div data-turn-slot="trace">
          <CanonicalTurnTrace turn={turn} />
        </div>
        {hasFollowingContent ? (
          <Separator className="conversation-trace-separator" />
        ) : null}
        <div data-turn-slot="final">
          <CanonicalFinalAnswer
            turn={turn}
            userText={userEntity?.messageType === 'user' ? (userEntity.content ?? '') : ''}
            surfaceRef={primarySurfaceRef}
          />
        </div>
        {results.length > 0 ? (
          <div data-turn-slot="agents">
            <AgentIndex
              turn={indexTurn}
              sourceResults={results}
              selectedAgentMessageId={selectedAgentMessageId}
              onOpenDetail={onOpenAgentDetail}
              isLastTurn={isLastTurn}
            />
          </div>
        ) : null}
      </div>
    </div>
  )
}

export const CanonicalTurnRenderer = memo(CanonicalTurnRendererComponent)

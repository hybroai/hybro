'use client'

import { useState } from 'react'
import { ChevronRight } from 'lucide-react'
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible'
import type { AgentResultViewModel, TurnViewModel } from '@/lib/room-timeline/types'
import { getAgentTheme, type AgentDisplayProps } from '@/lib/selectors/conversation-types'
import { mapResultDisplayProps } from '@/lib/room-timeline/map-result-display'
import {
  countDurableArtifactFiles,
  hasUnavailableArtifactOutput,
  hasUsableArtifactOutput,
} from '@/lib/artifacts/artifact-identity'
import { getAgentIndexSummary, defaultAgentIndexOpen } from '@/lib/room-timeline/turn-live-shell'
import { useResultStreamDisplay } from '@/hooks/useStreamBuffer'
import { AgentCard } from './AgentCard'
import { cn } from '@/lib/utils'

interface AgentIndexProps {
  turn: TurnViewModel
  sourceResults: AgentResultViewModel[]
  selectedAgentMessageId?: string
  onOpenDetail?: (messageId: string) => void
  isLastTurn?: boolean
}

function IndexRow({
  result,
  selected,
  onOpenDetail,
}: {
  result: AgentResultViewModel
  selected: boolean
  onOpenDetail?: (messageId: string) => void
}) {
  const { content, isStreaming, artifacts } = useResultStreamDisplay(result)
  const baseDisplay = mapResultDisplayProps(result, isStreaming, content)
  const canonicalDisplay: AgentDisplayProps | undefined = result.executionStatus
    ? ({
        running: {
          label: 'Running', tone: 'accent', isAnimated: true,
          ariaLabel: `${result.agentName} — Running`,
        },
        awaiting_input: {
          label: 'Waiting for input', tone: 'warning', isAnimated: true,
          ariaLabel: `${result.agentName} — Waiting for input`,
        },
        completed: {
          label: 'Completed', tone: 'muted', isAnimated: false,
          ariaLabel: `${result.agentName} — Completed`,
        },
        failed: {
          label: 'Failed', tone: 'danger', isAnimated: false,
          ariaLabel: `${result.agentName} — Failed`,
        },
        canceled: {
          label: 'Canceled', tone: 'muted', isAnimated: false,
          ariaLabel: `${result.agentName} — Canceled`,
        },
      } satisfies Record<
        NonNullable<AgentResultViewModel['executionStatus']>,
        AgentDisplayProps
      >)[result.executionStatus]
    : undefined
  const theme = getAgentTheme(result.agentId, result.agentName)
  const fileCount = countDurableArtifactFiles(artifacts)
  const hasUnavailable = hasUnavailableArtifactOutput(artifacts)
  const hasUsefulOutput = Boolean(content.trim()) || hasUsableArtifactOutput(artifacts)
  const outputUnavailable = hasUnavailable && !hasUsefulOutput
  const display = outputUnavailable
    ? {
        label: 'Output unavailable',
        tone: 'danger' as const,
        isAnimated: false,
        ariaLabel: `${result.agentName} — Output unavailable`,
      }
    : (canonicalDisplay ?? baseDisplay)
  const statusSuffix = hasUnavailable && hasUsefulOutput
    ? 'partial output'
    : fileCount > 0
      ? `${fileCount} file${fileCount === 1 ? '' : 's'}`
      : undefined

  return (
    <AgentCard
      messageId={result.messageId}
      agentId={result.agentId}
      agentName={result.agentName}
      taskDescription={result.taskStatusMessage ?? ''}
      theme={theme}
      display={display}
      agentSource={result.agentSource}
      selected={selected}
      onOpen={onOpenDetail}
      compact
      statusSuffix={statusSuffix}
      lifecycleStatus={result.executionStatus}
      failureDetail={result.failureDetail}
    />
  )
}

export function AgentIndex({
  turn,
  sourceResults,
  selectedAgentMessageId,
  onOpenDetail,
  isLastTurn = false,
}: AgentIndexProps) {
  const { finalAnswer } = turn
  const [open, setOpen] = useState(() => defaultAgentIndexOpen(turn, isLastTurn))

  if (sourceResults.length === 0) return null

  const summary = getAgentIndexSummary(turn, sourceResults, finalAnswer.kind)
  const forceExpand = sourceResults.some(r => r.messageId === selectedAgentMessageId)
  const expanded = open || forceExpand
  const stripResults = sourceResults

  if (stripResults.length === 0) return null

  return (
    <Collapsible
      open={expanded}
      onOpenChange={setOpen}
      className="turn-activity-strip agent-index"
      style={{ overflowAnchor: 'none' }}
    >
      <CollapsibleTrigger
        className="flex w-full items-center gap-2 rounded-lg border px-3 py-2 text-left text-sm transition-colors hover:bg-muted/30"
        style={{ borderColor: 'var(--conversation-border-subtle)', color: 'var(--conversation-text-secondary)' }}
        aria-label={`${summary}, expandable`}
      >
        <ChevronRight className={cn('h-4 w-4 shrink-0 transition-transform', expanded && 'rotate-90')} />
        <span>{summary}</span>
      </CollapsibleTrigger>
      <CollapsibleContent className="turn-activity-strip-content data-[state=open]:animate-collapsible-down overflow-hidden data-[state=open]:overflow-visible">
        <div
          className="turn-activity-strip-list mt-2 flex flex-col"
          style={{ gap: 'var(--conversation-gap-block)' }}
        >
          {stripResults.map(result => (
            <IndexRow
              key={result.messageId}
              result={result}
              selected={result.messageId === selectedAgentMessageId}
              onOpenDetail={onOpenDetail}
            />
          ))}
        </div>
      </CollapsibleContent>
    </Collapsible>
  )
}

'use client'

import { useResultStreamDisplay } from '@/hooks/useStreamBuffer'
import { getAgentTheme } from '@/lib/selectors/conversation-types'
import { mapResultDisplayProps } from '@/lib/room-timeline/map-result-display'
import type { AgentResultViewModel } from '@/lib/room-timeline/types'
import { AgentCard } from './AgentCard'
import { AgentContentBlock } from './AgentContentBlock'
import { UserAnswerCard } from './UserAnswerCard'

interface AgentResultContentProps {
  result: AgentResultViewModel
  showAttribution?: boolean
  compact?: boolean
  selected?: boolean
  onOpenDetail?: (messageId: string) => void
}

export function AgentResultContent({
  result,
  showAttribution = false,
  compact = false,
  selected,
  onOpenDetail,
}: AgentResultContentProps) {
  const { content, isStreaming, artifacts } = useResultStreamDisplay(result)
  const display = mapResultDisplayProps(result, isStreaming, content)
  const theme = getAgentTheme(result.agentId, result.agentName)
  const taskDescription = result.taskStatusMessage ?? ''
  const hasContent = content.trim().length > 0 || (artifacts?.length ?? 0) > 0

  return (
    <div className="flex flex-col" style={{ gap: 'var(--conversation-gap-block)' }}>
      <AgentCard
        messageId={result.messageId}
        agentId={result.agentId}
        agentName={result.agentName}
        taskDescription={
          taskDescription
          || (result.status === 'working' && !hasContent ? 'Working on your request…' : '')
        }
        theme={theme}
        display={display}
        agentSource={result.agentSource}
        selected={selected}
        interactive={!compact}
        onOpen={onOpenDetail}
      />
      {result.hitlResolved && (
        <UserAnswerCard
          agentName={result.agentName}
          question={result.hitlResolved.prompt}
          answer={result.hitlResolved.answer}
        />
      )}
      {hasContent && (
        <AgentContentBlock
          agentId={result.agentId ?? result.messageId}
          agentName={result.agentName}
          messageId={result.messageId}
          content={content}
          isStreaming={isStreaming}
          showAttribution={showAttribution}
          artifacts={artifacts}
        />
      )}
    </div>
  )
}

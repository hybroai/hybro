'use client'

import { memo } from 'react'
import { useShallow } from 'zustand/react/shallow'
import { useMessageStore } from '@/stores/message-store'
import { MarkdownContent } from '@/components/markdown-content'
import { ArtifactList } from '@/components/artifact-list'
import { filterDuplicateTextArtifacts } from '@/lib/artifacts/filter-display-artifacts'
import { UserMessageBlock } from './UserMessageBlock'

interface TranscriptTurnProps {
  userMessageId: string
  replyIds: string[]
}

/**
 * Read-only view of a Turn that has no canonical root (history from before
 * the canonical Turn lifecycle): the user message and each agent's final
 * reply, without a trace or live state.
 */
function TranscriptTurnComponent({ userMessageId, replyIds }: TranscriptTurnProps) {
  const userEntity = useMessageStore((state) => state.entities[userMessageId])
  const replies = useMessageStore(useShallow((state) => replyIds.map((id) => state.entities[id])))
  if (userEntity?.messageType !== 'user') return null

  return (
    <div className="conversation-turn" data-turn-kind="transcript">
      <div className="conversation-user-sticky">
        <UserMessageBlock entity={userEntity} />
      </div>
      {replies.length > 0 ? (
        <div className="conversation-body-frame conversation-turn-content flex flex-col">
          {replies.map((reply) => reply ? (
            <div
              key={reply.id}
              className="conversation-content-body"
              data-message-id={reply.id}
              data-quote-message-id={reply.id}
              data-quote-agent-name={reply.senderName}
              data-quote-source-kind="agent"
              data-quote-agent-id={reply.agentId}
            >
              <div className="mb-1 text-xs font-medium" style={{ color: 'var(--conversation-text-muted)' }}>
                {reply.senderName}
              </div>
              {reply.content.trim() ? (
                <MarkdownContent className="conversation-markdown-body" content={reply.content} />
              ) : null}
              <ArtifactList artifacts={filterDuplicateTextArtifacts(reply.artifacts, reply.content) ?? []} />
            </div>
          ) : null)}
        </div>
      ) : null}
    </div>
  )
}

// The list rebuilds `replyIds` on every message-store change, so compare it by value.
export const TranscriptTurn = memo(TranscriptTurnComponent, (prev, next) => (
  prev.userMessageId === next.userMessageId
  && prev.replyIds.length === next.replyIds.length
  && prev.replyIds.every((id, index) => id === next.replyIds[index])
))

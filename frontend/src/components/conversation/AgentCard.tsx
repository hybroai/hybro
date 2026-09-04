'use client'

import Link from 'next/link'
import { useCachedAgentCatalog } from '@/hooks/room/useAgentCatalog'
import type { AgentDisplayProps, AgentTheme } from '@/lib/selectors/conversation-types'
import { getAgentAvatarUri } from '@/lib/agent-avatar'
import { AgentSourceBadge } from '@/components/agent-source-badge'
import { cn } from '@/lib/utils'
import type { ReactNode } from 'react'
import type { Agent } from '@/lib/types/agent'

interface AgentCardProps {
  messageId?: string
  agentName: string
  agentId?: string
  taskDescription: string
  theme: AgentTheme
  display: AgentDisplayProps
  selected?: boolean
  interactive?: boolean
  onOpen?: (messageId: string) => void
  rightAction?: ReactNode
  agentSource?: 'cloud' | 'local' | 'hub'
  /** Single-line strip row: tighter padding, no task row, no card shimmer. */
  compact?: boolean
  /** Appended to status label in compact mode (e.g. artifact count). */
  statusSuffix?: string
  lifecycleStatus?: 'running' | 'awaiting_input' | 'completed' | 'failed' | 'canceled'
}

function useAgentFromCatalog(agentId: string | undefined, agentName: string): Agent | undefined {
  const agents = useCachedAgentCatalog()
  if (agentId) return agents?.find(a => a.agent_id === agentId)

  const matchingAgents = agents?.filter(a => a.agent_card.name === agentName)
  return matchingAgents?.length === 1 ? matchingAgents[0] : undefined
}

function AgentAvatar({
  avatarId,
  iconUrl,
  theme,
  isAnimated,
  compact,
}: {
  avatarId: string
  iconUrl?: string
  theme: AgentTheme
  isAnimated?: boolean
  compact?: boolean
}) {
  return (
    <div className={cn('shrink-0 relative', compact ? 'w-6 h-6' : 'w-8 h-8', isAnimated && 'conversation-avatar-working')}>
      <div
        className="w-full h-full overflow-hidden"
        style={{ backgroundColor: theme.avatarLightBg, borderRadius: 'var(--chat-input-radius)' }}
      >
        {iconUrl ? (
          <img
            src={iconUrl}
            alt=""
            className="w-full h-full object-cover"
            onError={(e) => {
              (e.currentTarget as HTMLImageElement).style.display = 'none'
              const fallback = (e.currentTarget as HTMLImageElement).nextElementSibling as HTMLImageElement | null
              if (fallback) fallback.style.display = 'block'
            }}
          />
        ) : null}
        <img
          src={getAgentAvatarUri(avatarId)}
          alt=""
          className="w-full h-full"
          style={{ display: iconUrl ? 'none' : 'block' }}
        />
      </div>
    </div>
  )
}

export function AgentCard({
  messageId,
  agentName,
  agentId,
  taskDescription,
  theme,
  display,
  selected = false,
  interactive = true,
  onOpen,
  rightAction,
  agentSource,
  compact = false,
  statusSuffix,
  lifecycleStatus,
}: AgentCardProps) {
  const catalogAgent = useAgentFromCatalog(agentId, agentName)
  const profileAgentId = agentId ?? catalogAgent?.agent_id
  const iconUrl = catalogAgent?.agent_card?.iconUrl || undefined
  const isHubOnline = catalogAgent?.is_hub_online

  const toneColors: Record<AgentDisplayProps['tone'], string> = {
    accent: 'hsl(var(--color-primary))',
    muted: 'var(--conversation-agent-green)',
    danger: 'var(--conversation-danger)',
    warning: 'var(--conversation-agent-yellow)',
  }

  const canOpen = interactive && !!messageId && !!onOpen
  const className = cn(
    'conversation-agent-card relative border overflow-hidden',
    compact && 'conversation-agent-card-compact shrink-0',
    canOpen && 'w-full text-left cursor-pointer transition-colors hover:border-cyan-300/30 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-cyan-300/35',
    !compact && display.isAnimated && 'conversation-card-shimmer',
  )
  const style = {
    backgroundColor: selected
      ? 'hsl(var(--color-primary) / 0.12)'
      : theme.cardBg,
    borderColor: selected
      ? 'hsl(var(--color-primary) / 0.55)'
      : display.tone === 'danger'
        ? 'var(--conversation-danger-border)'
        : display.tone === 'warning'
          ? '#854d0e'
          : theme.cardBg,
    boxShadow: selected
      ? '0 0 0 1px hsl(var(--color-primary) / 0.28)'
      : 'none',
  }
  const content = (
    <div className={cn('relative z-[1]', canOpen && 'pointer-events-none')}>
      <div className={cn('flex items-center gap-2.5', compact && 'gap-2')}>
        {profileAgentId ? (
          <Link
            href={`/agents/${encodeURIComponent(profileAgentId)}`}
            aria-label={`View ${agentName} profile`}
            className="pointer-events-auto block shrink-0 rounded-[var(--chat-input-radius)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-cyan-300/35"
          >
            <AgentAvatar avatarId={profileAgentId} iconUrl={iconUrl} theme={theme} isAnimated={display.isAnimated} compact={compact} />
          </Link>
        ) : (
          <AgentAvatar avatarId={agentName} iconUrl={iconUrl} theme={theme} isAnimated={display.isAnimated} compact={compact} />
        )}
        {profileAgentId ? (
          <Link
            href={`/agents/${encodeURIComponent(profileAgentId)}`}
            className="pointer-events-auto text-[13px] font-medium hover:underline focus-visible:outline-none"
            style={{ color: 'var(--conversation-text-primary)' }}
          >
            {agentName}
          </Link>
        ) : (
          <span
            className="text-[13px] font-medium"
            style={{ color: 'var(--conversation-text-primary)' }}
          >
            {agentName}
          </span>
        )}
        {agentSource != null && (
          <AgentSourceBadge
            source={agentSource}
            isHubOnline={isHubOnline}
            className="h-3.5 w-3.5"
          />
        )}
        <span
          className="conversation-agent-status ml-auto font-medium"
          role="status"
          aria-label={display.ariaLabel}
          style={{ color: toneColors[display.tone], position: 'relative', zIndex: 1 }}
        >
          {display.label}
          {compact && statusSuffix ? ` · ${statusSuffix}` : null}
        </span>
        {rightAction && (
          <span className="conversation-agent-card-action pointer-events-auto">
            {rightAction}
          </span>
        )}
      </div>
      {!compact && taskDescription && (
        <div className="conversation-agent-task-row flex items-start gap-1.5 pl-[42px]">
          <span className="text-sm leading-none mt-px shrink-0" style={{ color: 'var(--conversation-text-dim)' }}>&#x2514;</span>
          <span className="conversation-agent-task-text text-[13px] font-medium truncate" style={{ color: 'var(--conversation-text-primary)' }}>
            {taskDescription}
          </span>
        </div>
      )}
    </div>
  )

  return (
    <div
      data-call-id={messageId?.split(':').at(-1)}
      data-status={lifecycleStatus}
      data-selected={selected ? 'true' : undefined}
      className={className}
      style={style}
    >
      {canOpen && (
        <button
          type="button"
          aria-label={`${selected ? 'Close' : 'Open'} ${agentName} response`}
          className="absolute inset-0 z-0 w-full cursor-pointer focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-cyan-300/35"
          onClick={() => onOpen?.(messageId!)}
        />
      )}
      {content}
    </div>
  )
}

'use client'

import { House, Cloud } from 'lucide-react'
import { cn } from '@/lib/utils'
import {
  Tooltip,
  TooltipTrigger,
  TooltipContent,
} from '@/components/ui/tooltip'

interface AgentSourceBadgeProps {
  source?: 'cloud' | 'local' | 'hub' | 'bundled'
  isHubOnline?: boolean
  className?: string
}

export function AgentSourceBadge({
  source,
  isHubOnline,
  className,
}: AgentSourceBadgeProps) {
  if (source === 'bundled') {
    return (
      <Tooltip>
        <TooltipTrigger asChild>
          <House aria-label="Sample agent" className={cn('shrink-0 text-emerald-500', className)} />
        </TooltipTrigger>
        <TooltipContent side="top">Sample agent included with Hybro</TooltipContent>
      </Tooltip>
    )
  }
  if (source === 'hub' || source === 'local') {
    const online = source === 'local' || (isHubOnline ?? false)
    return (
      <Tooltip>
        <TooltipTrigger asChild>
          <House
            aria-label="Local agent"
            className={cn(
              'shrink-0',
              online
                ? 'text-emerald-500'
                : 'text-muted-foreground/50',
              className
            )}
          />
        </TooltipTrigger>
        <TooltipContent side="top">
          {online
            ? 'Local agent'
            : 'Local agent offline'}
        </TooltipContent>
      </Tooltip>
    )
  }

  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Cloud
          aria-label="Remote agent"
          className={cn('shrink-0 text-sky-500', className)}
        />
      </TooltipTrigger>
      <TooltipContent side="top">
        Remote agent
      </TooltipContent>
    </Tooltip>
  )
}

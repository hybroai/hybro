'use client'

import { useState } from 'react'
import { ChevronRight, LoaderCircle } from 'lucide-react'
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible'
import { Marker, MarkerContent, MarkerIcon } from '@/components/ui/marker'
import { cn } from '@/lib/utils'
import { useTurnDurationLabel } from './turn-trace-time'

/**
 * Trace for a sent message whose canonical Turn has not arrived yet. Its
 * markup is the empty running state of `CanonicalTurnTrace`, so the swap to
 * the canonical Turn does not change what is on screen.
 */
export function PendingTurnHeader({ startedAt }: { startedAt?: string }) {
  const [isExpanded, setIsExpanded] = useState(true)
  const duration = useTurnDurationLabel({ startedAt, live: true })

  return (
    <Collapsible open={isExpanded} onOpenChange={setIsExpanded} asChild>
      <section className="canonical-turn-trace" data-canonical-turn-trace data-status="running">
        <Marker asChild className="conversation-trace-header">
          <CollapsibleTrigger aria-label={`Work Logs, Running, ${duration}`}>
            <MarkerContent className="conversation-trace-header-content">
              <span className="conversation-trace-run-state" data-status="running">Running</span>
              <span className="conversation-trace-duration">{duration}</span>
            </MarkerContent>
            <ChevronRight className={cn('conversation-trace-chevron', isExpanded && 'rotate-90')} />
          </CollapsibleTrigger>
        </Marker>
        <CollapsibleContent>
          <div className="conversation-trace-log canonical-turn-trace-log" role="log" aria-live="polite">
            <Marker data-kind="preparing" className="canonical-trace-marker">
              <MarkerIcon><LoaderCircle className="animate-spin motion-reduce:animate-none" /></MarkerIcon>
              <MarkerContent>
                <span className="conversation-trace-action-label">Preparing a response</span>
                <span className="conversation-trace-action-status">Running</span>
              </MarkerContent>
            </Marker>
          </div>
        </CollapsibleContent>
      </section>
    </Collapsible>
  )
}

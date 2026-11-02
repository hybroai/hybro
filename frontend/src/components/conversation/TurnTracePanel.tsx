'use client'

import { useEffect, useMemo, useRef, useState } from 'react'
import { ChevronRight, LoaderCircle } from 'lucide-react'
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible'
import { Marker, MarkerContent, MarkerIcon } from '@/components/ui/marker'
import { cn } from '@/lib/utils'
import type { ProcessingStatusLogEntry } from '@/stores/message-store/types'
import { useTurnDurationLabel } from './turn-trace-time'
import { progressStatusLogs } from '@/hooks/room/processing-status-log'
import { ProcessingStatusLog } from './ProcessingStatusLog'

interface TurnTracePanelProps {
  statusEntries: ProcessingStatusLogEntry[]
  isRunning?: boolean
  isWaiting?: boolean
  startedAt?: string
  durationMs?: number
}

export function TurnTracePanel({
  statusEntries,
  isRunning = false,
  isWaiting = false,
  startedAt,
  durationMs,
}: TurnTracePanelProps) {
  const [isExpanded, setIsExpanded] = useState(() => isRunning || isWaiting)
  const scrollRef = useRef<HTMLDivElement | null>(null)
  const observedDurationMs = useMemo(() => {
    if (durationMs !== undefined) return durationMs
    if (!startedAt || isRunning || isWaiting) return undefined
    const started = Date.parse(startedAt)
    if (!Number.isFinite(started)) return undefined
    const observed = statusEntries
      .map((entry) => Date.parse(entry.timestamp))
      .filter(Number.isFinite)
    if (observed.length === 0) return 0
    return Math.max(0, Math.max(...observed) - started)
  }, [durationMs, isRunning, isWaiting, startedAt, statusEntries])
  const duration = useTurnDurationLabel({
    startedAt,
    durationMs: observedDurationMs,
    live: isRunning || isWaiting,
  })
  const label = isRunning ? 'Running' : isWaiting ? 'Waiting for input' : 'Finished'

  useEffect(() => {
    const scroll = scrollRef.current
    if (!scroll || !isExpanded || !isRunning) return
    scroll.scrollTop = scroll.scrollHeight
  }, [isExpanded, isRunning])

  // The optimistic 'Thinking...' placeholder is not progress; the trace header
  // and "Preparing a response" marker cover that state.
  const progressEntries = progressStatusLogs(statusEntries)
  const workLogs = progressEntries.length > 0
    ? <ProcessingStatusLog entries={progressEntries} isRunning={isRunning || isWaiting} />
    : null

  return (
    <>
      {workLogs}
      {!workLogs ? (
        <Collapsible
          open={isExpanded}
          onOpenChange={setIsExpanded}
          className="conversation-trace"
          data-status={label.toLowerCase().replaceAll(' ', '-')}
        >
          <Marker asChild className="conversation-trace-header">
            <CollapsibleTrigger aria-label={`${label}, ${duration}`}>
              <MarkerContent className="conversation-trace-header-content">
                <span className="conversation-trace-run-state" data-status={label.toLowerCase().replaceAll(' ', '-')}>
                  {label}
                </span>
                <span className="conversation-trace-duration">{duration}</span>
              </MarkerContent>
              <ChevronRight className={cn('conversation-trace-chevron', isExpanded && 'rotate-90')} />
            </CollapsibleTrigger>
          </Marker>
          <CollapsibleContent>
            <div
              ref={scrollRef}
              className="conversation-trace-log"
              role={isRunning ? 'log' : undefined}
              aria-live={isRunning ? 'polite' : undefined}
            >
              {isRunning ? (
                <Marker data-kind="preparing" className="conversation-trace-action">
                  <MarkerIcon><LoaderCircle className="animate-spin motion-reduce:animate-none" /></MarkerIcon>
                  <MarkerContent>
                    <span className="conversation-trace-action-label">Preparing a response</span>
                    <span className="conversation-trace-action-status">Running</span>
                  </MarkerContent>
                </Marker>
              ) : null}
            </div>
          </CollapsibleContent>
        </Collapsible>
      ) : null}
    </>
  )
}

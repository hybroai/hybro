import type React from 'react'
import { cn } from '@/lib/utils'

export type NoticeTone = 'warning' | 'error' | 'progress' | 'neutral' | 'success'

const FRAMES: Record<NoticeTone, { frame: string; title: string; dot: string }> = {
  warning: { frame: 'border-status-warning/35 bg-status-warning/[0.07]', title: 'text-status-warning', dot: 'bg-status-warning' },
  error: { frame: 'border-status-error/40 bg-status-error/[0.07]', title: 'text-status-error', dot: 'bg-status-error' },
  progress: { frame: 'border-icon-action/35 bg-icon-action/[0.06]', title: 'text-icon-action', dot: 'bg-icon-action animate-pulse' },
  neutral: { frame: 'bg-card', title: 'text-foreground', dot: 'bg-muted-foreground' },
  success: { frame: 'bg-card', title: 'text-foreground', dot: 'bg-status-online' },
}

/** A one-line status card on the Settings pages: title, explanation and an optional action. */
export function SettingsNotice({ tone, title, children, action, role = 'status' }: {
  tone: NoticeTone
  title: React.ReactNode
  children?: React.ReactNode
  action?: React.ReactNode
  role?: 'status' | 'alert'
}) {
  const style = FRAMES[tone]
  return (
    <div role={role} data-tone={tone} className={cn('flex flex-wrap items-center gap-3 rounded-xl border px-4 py-3.5', style.frame)}>
      <span aria-hidden="true" className={cn('size-2 shrink-0 rounded-full', style.dot)} />
      <span className="flex min-w-0 flex-[1_1_280px] flex-col gap-0.5">
        <span className={cn('text-sm font-semibold', style.title)}>{title}</span>
        {children ? <span className="text-[13px] leading-[18px] text-muted-foreground">{children}</span> : null}
      </span>
      {action}
    </div>
  )
}

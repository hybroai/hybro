'use client'

import Link from 'next/link'
import { useEffect, useSyncExternalStore, type ReactNode } from 'react'
import { Settings, X, type LucideIcon } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { useSetupStatus } from '@/hooks/useSetupStatus'
import { routes } from '@/lib/routes'
import { isDismissed, readDismissed, setupNotice, subscribeDismissed, writeDismissed, type SetupNotice } from '@/lib/setup-notice'
import { cn } from '@/lib/utils'

const TONES: Record<SetupNotice['tone'], { frame: string; icon: string }> = {
  warning: { frame: 'border-status-warning/35 bg-status-warning/[0.07]', icon: 'text-status-warning' },
  error: { frame: 'border-status-error/40 bg-status-error/[0.07]', icon: 'text-status-error' },
}

/** A notice above a page's content, in the Overview banner's look. */
export function NoticeBanner({ tone, title, body, action, onDismiss, icon: Icon = Settings, testId = 'setup-banner' }: {
  tone: SetupNotice['tone']
  title: string
  body: ReactNode
  action?: ReactNode
  onDismiss?: () => void
  icon?: LucideIcon
  testId?: string
}) {
  const frame = TONES[tone]
  return (
    <div
      role="status"
      data-testid={testId}
      data-tone={tone}
      className={cn('flex flex-wrap items-center gap-3 rounded-xl border py-3 pr-3 pl-4', frame.frame)}
    >
      <Icon className={cn('size-[18px] shrink-0', frame.icon)} aria-hidden="true" />
      <span className="flex min-w-0 flex-[1_1_280px] flex-col gap-0.5 text-left">
        <span className="text-sm font-semibold text-foreground">{title}</span>
        <span className="text-[13px] leading-[18px] text-muted-foreground">{body}</span>
      </span>
      {action}
      {onDismiss ? (
        <Button variant="ghost" size="icon" className="size-8 text-muted-foreground" aria-label="Dismiss" onClick={onDismiss}>
          <X aria-hidden="true" />
        </Button>
      ) : null}
    </div>
  )
}

function SettingsLink({ label }: { label: string }) {
  return (
    <Button size="sm" asChild>
      <Link href={routes.settingsModelProvider}>{label}</Link>
    </Button>
  )
}

/** Overview banner: dismissible per browser until the status reports a new error code. */
export function OverviewSetupBanner() {
  const status = useSetupStatus()
  const notice = setupNotice(status.data)
  const dismissed = useSyncExternalStore(subscribeDismissed, readDismissed, () => null)
  const healthy = status.isSuccess && notice === null

  useEffect(() => {
    // A healthy status ends the dismissal, so the next error shows again.
    if (healthy && readDismissed() !== null) writeDismissed(null)
  }, [healthy])

  if (!notice || isDismissed(notice, dismissed)) return null
  return (
    <NoticeBanner
      tone={notice.tone}
      title={notice.title}
      body={notice.message}
      action={<SettingsLink label={notice.action} />}
      onDismiss={() => writeDismissed(notice.code)}
    />
  )
}

/** Room Chat banner: always shown above the composer while there is a notice. */
export function SetupBanner({ notice }: { notice: SetupNotice | null }) {
  return notice
    ? <NoticeBanner tone={notice.tone} title={notice.title} body={notice.chatMessage} action={<SettingsLink label={notice.action} />} />
    : null
}

/** Composer placeholder while the setup notice blocks Room Chat. */
export const BLOCKED_COMPOSER_PLACEHOLDER = 'Connect a model provider to start a chat'

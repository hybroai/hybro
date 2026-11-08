'use client'

import Link from 'next/link'
import { usePathname } from 'next/navigation'
import { Cpu, Monitor, SlidersHorizontal, type LucideIcon } from 'lucide-react'
import { PHASE_TEXT, providerPhase, type ProviderPhase } from '@/components/settings/model-provider-section'
import { useInstanceSettings } from '@/hooks/useInstanceSettings'
import { useSetupStatus } from '@/hooks/useSetupStatus'
import { routes } from '@/lib/routes'
import { cn } from '@/lib/utils'

const TABS: { href: string; label: string; icon: LucideIcon }[] = [
  { href: routes.settingsModelProvider, label: 'Model provider', icon: Cpu },
  { href: routes.settingsConfiguration, label: 'Configuration', icon: SlidersHorizontal },
  { href: routes.settingsSystem, label: 'System', icon: Monitor },
]

const PHASE_LABEL: Record<ProviderPhase, string> = {
  not_set_up: 'not set up',
  signing_in: 'waiting for sign-in',
  connected: 'connected',
  expired: 'sign-in expired',
}

/** The Settings sections as tabs, with the provider state and a pending restart as dots. */
export function SettingsTabs() {
  const pathname = usePathname()
  const status = useSetupStatus().data
  const settings = useInstanceSettings().data
  const phase = status ? providerPhase(status, null) : null
  const dots: Record<string, { color: string; label: string } | null> = {
    [routes.settingsModelProvider]: phase ? { color: PHASE_TEXT[phase], label: PHASE_LABEL[phase] } : null,
    [routes.settingsConfiguration]: settings?.restart_pending ? { color: 'text-status-warning', label: 'restart pending' } : null,
    [routes.settingsSystem]: null,
  }

  return (
    <nav aria-label="Settings sections" className="flex flex-wrap gap-1 border-b">
      {TABS.map(({ href, label, icon: Icon }) => {
        const current = pathname === href
        const dot = dots[href]
        return (
          <Link
            key={href}
            href={href}
            aria-current={current ? 'page' : undefined}
            aria-label={dot ? `${label} (${dot.label})` : undefined}
            className={cn(
              '-mb-px flex h-10 items-center gap-2 border-b-2 px-3 text-sm outline-none focus-visible:ring-[3px] focus-visible:ring-ring/50',
              current ? 'border-icon-action font-medium text-foreground' : 'border-transparent text-muted-foreground hover:text-foreground',
            )}
          >
            <Icon className="size-4 shrink-0" aria-hidden="true" />
            {label}
            {dot ? <span aria-hidden="true" className={cn('size-1.5 rounded-full bg-current', dot.color)} /> : null}
          </Link>
        )
      })}
    </nav>
  )
}

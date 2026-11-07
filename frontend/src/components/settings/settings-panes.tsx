'use client'

import { useState } from 'react'
import { usePathname } from 'next/navigation'
import { ConfigurationSection } from '@/components/settings/configuration-section'
import { ModelProviderSection } from '@/components/settings/model-provider-section'
import { SystemSection } from '@/components/settings/system-section'
import { routes } from '@/lib/routes'

type Pane = 'model-provider' | 'configuration' | 'system'

const PATHS: Record<Pane, string> = {
  'model-provider': routes.settingsModelProvider,
  configuration: routes.settingsConfiguration,
  system: routes.settingsSystem,
}

/**
 * The Settings sections. A section stays mounted once visited and is hidden
 * while another tab is open, so a ChatGPT sign-in in progress and unsaved
 * Configuration edits survive switching tabs.
 */
export function SettingsPanes() {
  const pathname = usePathname()
  const current = (Object.keys(PATHS) as Pane[]).find(pane => PATHS[pane] === pathname)
  const [visited, setVisited] = useState<Pane[]>([])
  if (current && !visited.includes(current)) setVisited([...visited, current])
  const shown = (pane: Pane) => visited.includes(pane) || pane === current

  return (
    <>
      {shown('model-provider') ? (
        <section aria-label="Model provider" hidden={current !== 'model-provider'}>
          <ModelProviderSection />
        </section>
      ) : null}
      {shown('configuration') ? (
        <section aria-label="Configuration" hidden={current !== 'configuration'} className="flex flex-col gap-4">
          <h2 className="text-lg font-semibold">Configuration</h2>
          <ConfigurationSection />
        </section>
      ) : null}
      {shown('system') ? (
        <section aria-label="System" hidden={current !== 'system'} className="flex flex-col gap-4">
          <h2 className="text-lg font-semibold">System</h2>
          <SystemSection />
        </section>
      ) : null}
    </>
  )
}

'use client'

import { useState } from 'react'
import { Button } from '@/components/ui/button'
import { SettingsNotice } from '@/components/settings/settings-notice'
import { isRestarting, useInstanceSettings, useRestartServices, useRestartStarting } from '@/hooks/useInstanceSettings'

function joinLabels(labels: string[]): string {
  if (labels.length <= 1) return labels.join('')
  return `${labels.slice(0, -1).join(', ')} and ${labels[labels.length - 1]}`
}

/** Restart state on every Settings tab: pending changes, a running restart, and its result. */
export function RestartBanner() {
  const { data: view } = useInstanceSettings()
  const restart = useRestartServices()
  const starting = useRestartStarting()
  // A completed restart is announced only when this page saw it run; each new run shows it again.
  const running = isRestarting(view)
  const [wasRunning, setWasRunning] = useState(false)
  const [sawRestart, setSawRestart] = useState(false)
  const [dismissed, setDismissed] = useState(false)
  // Browser settings are read once per page load, so a restart that applied them needs a reload here.
  const [reloadLabels, setReloadLabels] = useState<string[]>([])
  if (running !== wasRunning) {
    setWasRunning(running)
    if (running) {
      setSawRestart(true)
      setDismissed(false)
      setReloadLabels((view?.fields ?? []).filter(field => field.restart_pending && field.key.startsWith('frontend.')).map(field => field.label))
    }
  }
  if (!view) return null

  if (running || starting) {
    return (
      <SettingsNotice tone="progress" title="Restarting Hybro…">
        The backend, web app, MCP and agents are restarting. Stay on this tab; it reconnects by itself when they are back.
      </SettingsNotice>
    )
  }

  const restartNow = view.restart.available ? (
    <Button onClick={() => restart.mutate()}>Restart now</Button>
  ) : null

  if (view.restart.state === 'failed') {
    return (
      <SettingsNotice tone="error" role="alert" title="Restart failed" action={view.restart_pending ? restartNow : null}>
        The last restart failed: {view.restart.error ?? 'no reason was reported.'} Check the logs with <code className="font-mono">hybro logs</code>.
      </SettingsNotice>
    )
  }

  if (view.restart_pending) {
    const labels = view.fields.filter(field => field.restart_pending).map(field => field.label)
    const count = labels.length
    return (
      <SettingsNotice
        tone="warning"
        title={count ? `Restart to apply ${count} saved ${count === 1 ? 'change' : 'changes'}` : 'Restart to apply saved changes'}
        action={restartNow}
      >
        {count ? `${joinLabels(labels)} ${count === 1 ? 'is' : 'are'} saved. ` : ''}
        Hybro keeps using the old values until it restarts.
        {view.restart.available ? null : ' Restart Hybro from the terminal to apply them.'}
      </SettingsNotice>
    )
  }

  if (sawRestart && !dismissed && view.restart.state === 'completed') {
    if (reloadLabels.length) {
      return (
        <SettingsNotice
          tone="success"
          title="Hybro restarted."
          action={<Button onClick={() => window.location.reload()}>Reload page</Button>}
        >
          Reload this page to use the new {joinLabels(reloadLabels)}.
        </SettingsNotice>
      )
    }
    return (
      <SettingsNotice
        tone="success"
        title="Hybro restarted. All saved settings are in effect."
        action={<Button variant="ghost" size="sm" onClick={() => setDismissed(true)}>Dismiss</Button>}
      />
    )
  }
  return null
}

'use client'

import Link from 'next/link'
import type React from 'react'
import { Loader2, RotateCw } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { SettingsQueryState } from '@/components/settings/configuration-section'
import { isRestarting, useInstanceSettings, useRestartServices, useRestartStarting } from '@/hooks/useInstanceSettings'
import type { SystemInfo } from '@/lib/api/settings'
import { routes } from '@/lib/routes'

const LISTEN: Record<NonNullable<SystemInfo['listen']>, [string, string]> = {
  loopback: ['This computer only', 'Reachable at localhost. Set when Hybro starts.'],
  public: ['All network interfaces', 'Reachable from other computers on your network. Set when Hybro starts.'],
}

function Row({ label, value, note, mono }: { label: string; value: React.ReactNode; note?: React.ReactNode; mono?: boolean }) {
  return (
    <div className="flex flex-wrap justify-between gap-x-6 gap-y-1 border-t py-3.5 first:border-t-0">
      <dt className="flex-[1_1_200px] text-sm text-muted-foreground">{label}</dt>
      <dd className="flex flex-[1_1_280px] flex-col items-end text-right">
        <span className={mono ? 'font-mono text-[13px] font-medium' : 'text-sm font-medium'}>{value}</span>
        {note ? <span className="mt-0.5 text-xs text-muted-foreground">{note}</span> : null}
      </dd>
    </div>
  )
}

export function SystemSection() {
  const settings = useInstanceSettings()
  const restart = useRestartServices()
  const starting = useRestartStarting()
  const view = settings.data
  if (!view) return <SettingsQueryState query={settings} />
  const { system } = view
  const running = isRestarting(view) || starting

  return (
    <div className="flex flex-col gap-4">
      <section aria-labelledby="system-instance" className="rounded-xl border bg-card px-5 pt-1">
        <h3 id="system-instance" className="mt-4 mb-1 text-[15px] font-semibold">Instance</h3>
        <dl>
          <Row label="Version" value={`Hybro ${system.version}`} />
          <Row label="Runs as" value={system.deployment === 'image' ? 'Docker image' : 'Source checkout'} />
          {system.port !== null ? (
            <Row label="Port" value={String(system.port)} mono note={<>Set with <code className="font-mono">hybro --port</code>.</>} />
          ) : null}
          {system.listen ? <Row label="Listens on" value={LISTEN[system.listen][0]} note={LISTEN[system.listen][1]} /> : null}
          <Row label="Sign-in" value={system.auth_mode === 'clerk' ? 'Clerk' : 'Local user'} note="Set when Hybro starts." />
          {system.mcp_available !== null ? (
            <Row
              label="MCP gateway"
              value={(
                <span className="inline-flex items-center gap-1.5">
                  <span aria-hidden="true" className={system.mcp_available ? 'size-1.5 rounded-full bg-status-online' : 'size-1.5 rounded-full bg-status-error'} />
                  {system.mcp_available ? 'Ready' : 'Unavailable'}
                </span>
              )}
            />
          ) : null}
          <Row label="Configuration file" value={system.config_location} mono note="Secrets stay in auth.json beside it and are never shown here." />
        </dl>
      </section>

      <section aria-labelledby="system-restart" className="flex flex-wrap items-center justify-between gap-3 rounded-xl border bg-card p-5">
        <div className="flex flex-[1_1_320px] flex-col gap-1">
          <h3 id="system-restart" className="text-[15px] font-semibold">Restart</h3>
          <p className="text-[13px] leading-[18px] text-muted-foreground">
            {view.restart.available
              ? 'Restarts the backend, web app, MCP and agents in place, in a few seconds. The database stays up, and this tab reconnects by itself.'
              : 'Hybro runs from a source checkout here, so it cannot restart itself. Restart it from the terminal you started it in.'}
          </p>
        </div>
        {running ? (
          <span role="status" className="inline-flex items-center gap-2 text-sm text-icon-action">
            <Loader2 className="size-4 animate-spin" aria-hidden="true" />
            Restarting…
          </span>
        ) : view.restart.available ? (
          <Button variant="secondary" onClick={() => restart.mutate()}>
            <RotateCw aria-hidden="true" />
            Restart Hybro
          </Button>
        ) : null}
      </section>

      {view.notices.length ? (
        <section aria-labelledby="system-notices" className="rounded-xl border bg-card px-5 pt-1">
          <h3 id="system-notices" className="mt-4 mb-1 text-[15px] font-semibold">Notices from config.json</h3>
          <p className="mb-1 text-[13px] text-muted-foreground">Found when Hybro last read the file. Nothing here stops Hybro from running.</p>
          <ul>
            {view.notices.map(notice => (
              <li key={notice.code} className="flex gap-2.5 border-t py-3.5">
                <span aria-hidden="true" className="mt-1.5 size-1.5 shrink-0 rounded-full bg-status-warning" />
                <span className="flex min-w-0 flex-col gap-1">
                  <span className="text-sm">{notice.message}</span>
                  {notice.fields.map(key => <span key={key} className="font-mono text-xs break-all text-muted-foreground">{key}</span>)}
                </span>
              </li>
            ))}
          </ul>
        </section>
      ) : null}

      <section aria-labelledby="system-about" className="rounded-xl border bg-card p-5">
        <h3 id="system-about" className="mb-2 text-[15px] font-semibold">About</h3>
        <div className="flex flex-wrap gap-x-5 gap-y-2 text-sm">
          <Link href={routes.about} className="font-medium underline underline-offset-2">About Hybro</Link>
          <a href="https://github.com/hybroai/hybro" target="_blank" rel="noopener noreferrer" className="font-medium underline underline-offset-2">Source code on GitHub</a>
        </div>
      </section>
    </div>
  )
}

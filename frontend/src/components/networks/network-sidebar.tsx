'use client'

import { Plus, RotateCw } from 'lucide-react'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { SidebarTrigger } from '@/components/ui/sidebar'
import { Skeleton } from '@/components/ui/skeleton'
import type { NetworkSidebarProps } from './types'
import { McpConnectionDialog } from './mcp-connection-dialog'

export function NetworkSidebar({
  networks,
  selectedId,
  loading,
  refreshing,
  error,
  disabled,
  onSelect,
  onCreate,
  onRetry,
}: NetworkSidebarProps) {
  return (
    <aside className="network-sidebar" aria-label="Your networks">
      <div className="network-sidebar-header flex items-center gap-2">
        <SidebarTrigger className="md:hidden" aria-label="Open main navigation" />
        <Button className="network-create min-w-0 flex-1" aria-label="Create network" onClick={onCreate} disabled={disabled || loading || Boolean(error)}>
          <Plus data-icon="inline-start" aria-hidden="true" />
          Create
        </Button>
        {!loading && (!error || networks.length > 0) ? (
          <Badge variant="secondary" aria-label={`${networks.length} networks`}>
            {networks.length}
          </Badge>
        ) : null}
      </div>
      {loading ? (
        <div className="flex flex-col gap-2" role="status" aria-label="Loading networks">
          <Skeleton className="h-9 w-full" />
          <Skeleton className="h-9 w-full" />
          <Skeleton className="h-9 w-3/4" />
          <span className="sr-only">Loading networks…</span>
        </div>
      ) : null}
      {error ? (
        <div className="flex flex-col items-start gap-2">
          <p role="alert" className="text-sm text-destructive wrap-anywhere">{error}</p>
          <Button variant="outline" size="sm" onClick={onRetry} disabled={refreshing || disabled}>
            <RotateCw data-icon="inline-start" aria-hidden="true" />
            {refreshing ? 'Retrying…' : 'Retry'}
          </Button>
        </div>
      ) : null}
      {!loading ? (
        <nav className="network-sidebar-list flex min-h-0 flex-col gap-1 overflow-y-auto" aria-label="Network list">
          {networks.map(network => (
            <Button
              key={network.group_id}
              variant={selectedId === network.group_id ? 'secondary' : 'ghost'}
              className="w-full justify-start"
              aria-current={selectedId === network.group_id ? 'true' : undefined}
              onClick={() => onSelect(network.group_id)}
              title={network.name}
            >
              <span className="size-1.5 shrink-0 rounded-full bg-primary" aria-hidden="true" />
              <span className="min-w-0 flex-1 truncate text-left">{network.name}</span>
              <span className="text-xs text-muted-foreground" aria-label={`${network.agents.length} Agents`}>
                {network.agents.length}
              </span>
            </Button>
          ))}
          {!error && networks.length === 0 ? (
            <p className="py-3 text-sm text-muted-foreground">No networks yet. Create one above.</p>
          ) : null}
        </nav>
      ) : null}
      {refreshing && !loading && !error ? (
        <p role="status" className="text-xs text-muted-foreground">Refreshing networks…</p>
      ) : null}
      <McpConnectionDialog />
    </aside>
  )
}

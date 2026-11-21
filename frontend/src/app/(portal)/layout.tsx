import type React from 'react'
import type { Metadata } from 'next'
import '@/app/globals.css'
import { SidebarInset, SidebarProvider } from '@/components/ui/sidebar'
import { PortalSidebar } from '@/components/portal/portal-sidebar'
import { PortalHeader } from '@/components/portal/portal-header'
import { SettingsDialogProvider } from '@/components/settings/settings-dialog-provider'
import { AuthGate, AuthProvider } from '@/lib/auth'

export const metadata: Metadata = {
  title: 'HYBRO AI – Your Local & Remote Hybrid Agent Platform',
  description:
    'Work with local and remote AI agents in a unified, private platform. Connect on-device or cloud-based agents and get things done.',
}

export default function PortalLayout({
  children,
}: {
  children: React.ReactNode
}) {
  return (
    <div className="min-h-screen bg-background">
      {/* The one gate: every portal page needs the owner's sign-in. */}
      <AuthProvider>
        <AuthGate>
          <SidebarProvider>
            <SettingsDialogProvider>
              <PortalSidebar />
              <SidebarInset>
                <link rel="icon" href="/favicon.svg" type="image/svg+xml" />
                <PortalHeader />
                <div className="flex min-w-0 flex-1 flex-col">{children}</div>
              </SidebarInset>
            </SettingsDialogProvider>
          </SidebarProvider>
        </AuthGate>
      </AuthProvider>
    </div>
  )
}

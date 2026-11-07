import type React from 'react'
import { RestartBanner } from '@/components/settings/restart-banner'
import { SettingsPanes } from '@/components/settings/settings-panes'
import { SettingsTabs } from '@/components/settings/settings-tabs'

export default function SettingsLayout({ children }: { children: React.ReactNode }) {
  return (
    <div className="page-container">
      <div className="page-content flex flex-col gap-6">
        <h1 className="text-2xl font-semibold tracking-tight">Settings</h1>
        <SettingsTabs />
        <RestartBanner />
        <SettingsPanes />
        {children}
      </div>
    </div>
  )
}

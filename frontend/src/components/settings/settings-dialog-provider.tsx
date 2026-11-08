"use client"

import { createContext, useContext, useState, useCallback } from "react"
import { SettingsDialog } from "@/components/settings/settings-dialog"

interface SettingsDialogContextValue {
  openSettings: () => void
}

const SettingsDialogContext = createContext<SettingsDialogContextValue | null>(null)

export function useSettingsDialog() {
  const ctx = useContext(SettingsDialogContext)
  if (!ctx) {
    throw new Error("useSettingsDialog must be used within a SettingsDialogProvider.")
  }
  return ctx
}

/**
 * Provides the account dialog state and renders the <SettingsDialog /> as a
 * sibling of its children — outside the Sidebar / Sheet tree — so that
 * nested Radix Dialog conflicts are avoided on mobile.
 */
export function SettingsDialogProvider({ children }: { children: React.ReactNode }) {
  const [open, setOpen] = useState(false)
  const openSettings = useCallback(() => setOpen(true), [])

  return (
    <SettingsDialogContext.Provider value={{ openSettings }}>
      {children}
      <SettingsDialog open={open} onOpenChange={setOpen} />
    </SettingsDialogContext.Provider>
  )
}

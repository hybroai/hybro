"use client"

import { createContext, useContext, useState, useCallback, useEffect, Suspense } from "react"
import { useRouter, useSearchParams } from "next/navigation"
import { SettingsDialog } from "@/components/settings/settings-dialog"
import { parseSettingsSection, SETTINGS_PARAM, withoutSettings, type SettingsSection } from "@/lib/settings-url"

interface SettingsDialogContextValue {
  openSettings: (section?: SettingsSection) => void
}

const SettingsDialogContext = createContext<SettingsDialogContextValue | null>(null)

export function useSettingsDialog() {
  const ctx = useContext(SettingsDialogContext)
  if (!ctx) {
    throw new Error("useSettingsDialog must be used within a SettingsDialogProvider.")
  }
  return ctx
}

/** Opens the dialog when the address carries `?settings=<section>`, so banners can link to a section. */
function SettingsUrlSync({ onOpen }: { onOpen: (section: SettingsSection) => void }) {
  const requested = parseSettingsSection(useSearchParams().get(SETTINGS_PARAM))
  useEffect(() => {
    if (requested) onOpen(requested)
  }, [requested, onOpen])
  return null
}

/**
 * Provides settings dialog state and renders the <SettingsDialog /> as a
 * sibling of its children — outside the Sidebar / Sheet tree — so that
 * nested Radix Dialog conflicts are avoided on mobile.
 */
export function SettingsDialogProvider({ children }: { children: React.ReactNode }) {
  const router = useRouter()
  const [open, setOpen] = useState(false)
  const [section, setSection] = useState<SettingsSection | null>(null)

  const openSettings = useCallback((target?: SettingsSection) => {
    setSection(target ?? null)
    setOpen(true)
  }, [])

  const onOpenChange = useCallback((next: boolean) => {
    setOpen(next)
    // Closing drops the section from the address, so the link opens it again.
    const { pathname, search } = window.location
    if (!next && new URLSearchParams(search).has(SETTINGS_PARAM)) {
      router.replace(withoutSettings(pathname, search), { scroll: false })
    }
  }, [router])

  return (
    <SettingsDialogContext.Provider value={{ openSettings }}>
      {children}
      <Suspense fallback={null}>
        <SettingsUrlSync onOpen={openSettings} />
      </Suspense>
      <SettingsDialog open={open} onOpenChange={onOpenChange} section={section} />
    </SettingsDialogContext.Provider>
  )
}

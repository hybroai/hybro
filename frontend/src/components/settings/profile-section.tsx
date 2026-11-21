"use client"

import { useState } from "react"
import { toast } from "sonner"
import type { AuthUser } from "@/lib/auth"
import { useAuthRefresh } from "@/lib/auth"
import { accountErrorMessage, updateProfile } from "@/lib/api/account"

import { Input } from "@/components/ui/input"

import { SettingsCard } from "@/components/settings/settings-card"
import { LoadingButton } from "@/components/settings/loading-button"
import { FormGroup } from "@/components/settings/form-group"

export function ProfileSection({ user }: { user: AuthUser }) {
  const refresh = useAuthRefresh()
  const [name, setName] = useState(user.fullName)
  const [email, setEmail] = useState(user.primaryEmailAddress.emailAddress)
  const [saving, setSaving] = useState(false)

  const isDirty = name !== user.fullName || email !== user.primaryEmailAddress.emailAddress
  const canSave = isDirty && name.trim().length > 0 && email.trim().length > 0

  async function handleSave(e: React.FormEvent) {
    e.preventDefault()
    if (!canSave) return
    try {
      setSaving(true)
      await updateProfile({ name: name.trim(), email: email.trim() })
      await refresh()
      toast.success("Profile updated")
    } catch (err: unknown) {
      toast.error(accountErrorMessage(err, "Failed to update profile"))
    } finally {
      setSaving(false)
    }
  }

  return (
    <SettingsCard title="Profile" description="The owner's name and email. The email signs you in.">
      <form onSubmit={handleSave} className="space-y-4">
        <FormGroup id="name" label="Name">
          <Input id="name" value={name} onChange={(e) => setName(e.target.value)} autoComplete="name" />
        </FormGroup>
        <FormGroup id="email" label="Email">
          <Input id="email" type="email" value={email} onChange={(e) => setEmail(e.target.value)} autoComplete="email" />
        </FormGroup>
        <div className="flex justify-end">
          <LoadingButton type="submit" disabled={!canSave} loading={saving}>
            Save changes
          </LoadingButton>
        </div>
      </form>
    </SettingsCard>
  )
}

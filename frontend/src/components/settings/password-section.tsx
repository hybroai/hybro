"use client"

import { useState } from "react"
import { toast } from "sonner"
import type { AuthUser } from "@/lib/auth"
import { accountErrorMessage, changePassword } from "@/lib/api/account"

import { SettingsCard } from "@/components/settings/settings-card"
import { PasswordInput } from "@/components/settings/password-input"
import { LoadingButton } from "@/components/settings/loading-button"
import { FormGroup } from "@/components/settings/form-group"

// The backend's minimum (account.models).
const MIN_PASSWORD_LENGTH = 8

export function PasswordSection({ user }: { user: AuthUser }) {
  const [currentPassword, setCurrentPassword] = useState("")
  const [newPassword, setNewPassword] = useState("")
  const [confirmPassword, setConfirmPassword] = useState("")
  const [saving, setSaving] = useState(false)

  const passwordsMatch = newPassword === confirmPassword
  const isLongEnough = newPassword.length >= MIN_PASSWORD_LENGTH
  const canSubmit = currentPassword.length > 0 && isLongEnough && confirmPassword.length > 0 && passwordsMatch

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    if (!canSubmit) return
    try {
      setSaving(true)
      await changePassword({ current_password: currentPassword, new_password: newPassword })
      setCurrentPassword("")
      setNewPassword("")
      setConfirmPassword("")
      toast.success("Password changed. Other browsers are signed out.")
    } catch (err: unknown) {
      toast.error(accountErrorMessage(err, "Failed to change password"))
    } finally {
      setSaving(false)
    }
  }

  return (
    <SettingsCard
      title="Password"
      description="Changing it signs out every other browser."
      spacing={4}
    >
      <form onSubmit={handleSubmit} className="space-y-4">
        {/* For password managers. */}
        <input
          type="text"
          name="username"
          autoComplete="username"
          value={user.primaryEmailAddress.emailAddress}
          readOnly
          className="sr-only"
          tabIndex={-1}
          aria-hidden="true"
        />
        <FormGroup id="currentPassword" label="Current password">
          <PasswordInput
            id="currentPassword"
            value={currentPassword}
            onChange={(e) => setCurrentPassword(e.target.value)}
            autoComplete="current-password"
          />
        </FormGroup>
        <FormGroup
          id="newPassword"
          label="New password"
          error={newPassword.length > 0 && !isLongEnough
            ? `Password must be at least ${MIN_PASSWORD_LENGTH} characters`
            : undefined}
        >
          <PasswordInput
            id="newPassword"
            value={newPassword}
            onChange={(e) => setNewPassword(e.target.value)}
            placeholder={`At least ${MIN_PASSWORD_LENGTH} characters`}
            autoComplete="new-password"
          />
        </FormGroup>
        <FormGroup
          id="confirmPassword"
          label="Confirm new password"
          error={confirmPassword.length > 0 && !passwordsMatch ? "Passwords do not match" : undefined}
        >
          <PasswordInput
            id="confirmPassword"
            value={confirmPassword}
            onChange={(e) => setConfirmPassword(e.target.value)}
            autoComplete="new-password"
          />
        </FormGroup>
        <div className="flex justify-end">
          <LoadingButton type="submit" disabled={!canSubmit} loading={saving}>
            Change password
          </LoadingButton>
        </div>
      </form>
    </SettingsCard>
  )
}

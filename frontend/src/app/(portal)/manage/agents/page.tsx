import { redirect } from 'next/navigation'
import { routes } from '@/lib/routes'

// Deprecated redirect; it will be removed in a later release.
export default function LegacyManageAgentsPage() {
  redirect(routes.network)
}

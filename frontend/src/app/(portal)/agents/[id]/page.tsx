import { redirect } from 'next/navigation'
import { routes } from '@/lib/routes'

// Deprecated redirect; it will be removed in a later release.
export default async function LegacyAgentPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params
  redirect(routes.networkAgent(id))
}

import { Globe, Inbox, LayoutDashboard, MessagesSquare, Plug, Settings } from 'lucide-react'
import type { NavAgentItem } from '@/lib/nav-items'
import { routes } from '@/lib/routes'

export const CONSUMER_NAV: NavAgentItem[] = [
  { name: 'Overview', url: routes.overview, icon: LayoutDashboard, colorClass: 'text-icon-architecture', exact: true },
  { name: 'Network', url: routes.network, icon: Globe, colorClass: 'text-icon-network' },
  { name: 'Chats', url: routes.chat, activePaths: ['/room'], icon: MessagesSquare, colorClass: 'text-icon-create' },
  { name: 'Inbox', url: routes.sessions, activePaths: ['/sessions'], icon: Inbox, colorClass: 'text-icon-create' },
  { name: 'Access', url: routes.connectMcp, activePaths: ['/connect'], icon: Plug, colorClass: 'text-hybro-bro' },
  { name: 'Settings', url: routes.settingsModelProvider, activePaths: [routes.settings], icon: Settings, colorClass: 'text-icon-moon' },
]

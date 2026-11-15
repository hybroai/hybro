import { MessageCirclePlus } from "lucide-react"

export type NavAgentLink = {
  name: string
  url: string
  icon: typeof MessageCirclePlus
  colorClass: string
  activePaths?: string[]
  /** Match only the exact URL (used for the Overview at `/`). */
  exact?: boolean
  /** Count shown next to the label (rooms needing input). */
  badge?: number
}

export type NavAgentGroup = Omit<NavAgentLink, 'url'> & {
  items: NavAgentLink[]
}

export type NavAgentItem = NavAgentLink | NavAgentGroup


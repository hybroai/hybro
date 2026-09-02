import { Palmtree, BookOpen } from "lucide-react"
import type { LucideIcon } from "lucide-react"
import type { Agent } from "@/lib/types/agent"

export interface UseCaseAgent {
  agentId: string
  agentName: string
  iconUrl?: string
}

export interface UseCaseTemplate {
  id: string
  icon: LucideIcon
  title: string
  description: string
  agents: UseCaseAgent[]
  prefillMessage: string
  tag?: "new" | null
}

/**
 * Resolve template agents against the live agent catalog.
 * ID-first, name-fallback (case-insensitive). Throws if any agent unresolved.
 */
export function resolveTemplateAgents(
  templateAgents: UseCaseAgent[],
  catalog: Agent[],
): Agent[] {
  const idMap = new Map(catalog.map((a) => [a.agent_id, a]))
  const nameMap = new Map(
    catalog.map((a) => [a.agent_card.name.toLowerCase(), a]),
  )

  const resolved: Agent[] = []
  for (const ta of templateAgents) {
    const byId = idMap.get(ta.agentId)
    if (byId) {
      resolved.push(byId)
      continue
    }
    const byName = nameMap.get(ta.agentName.toLowerCase())
    if (byName) {
      resolved.push(byName)
      continue
    }
    throw new Error("Some agents in this template are unavailable")
  }
  return resolved
}

/** Storage-format message with template agent mentions + a typed prefix of prefillMessage. */
export function buildTemplateDemoMessage(
  template: UseCaseTemplate,
  textLength: number,
): string {
  const mentionPrefix = template.agents
    .map((a) => `<@${a.agentId}|${a.agentName}>`)
    .join('')
  const text = template.prefillMessage.slice(0, textLength)
  return text ? `${mentionPrefix} ${text}` : mentionPrefix
}

// TODO: Replace agentId placeholders with actual production agent IDs.
// Currently using agent names; the name-fallback ensures they resolve in dev.
export const useCaseTemplates: UseCaseTemplate[] = [
  {
    id: "travel-planner",
    icon: Palmtree,
    title: "Travel Planner",
    description:
      "Plan a multi-day trip with weather insights and full itinerary",
    agents: [
      {
        agentId: "Weather Agent",
        agentName: "Weather Agent",
      },
      {
        agentId: "Travel Planner Agent",
        agentName: "Travel Planner Agent",
      },
    ],
    prefillMessage:
      "Plan a 7-day trip to Oahu, Hawaii, for four people, and check Honolulu's weather forecast for the next 7 days",
    tag: "new",
  },
  {
    id: "story-and-image",
    icon: BookOpen,
    title: "Story & Image Creator",
    description:
      "Generate a creative story and an AI image inspired by it",
    agents: [
      {
        agentId: "Story Agent",
        agentName: "Story Agent",
      },
      {
        agentId: "Image Generator Agent",
        agentName: "Image Generator Agent",
      },
    ],
    prefillMessage:
      "Give me a short fun story about AI Agents and generate an Image based on the story",
    tag: null,
  },
]

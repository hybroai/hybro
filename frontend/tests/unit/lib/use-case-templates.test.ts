import { describe, it, expect } from "vitest"
import { Palmtree } from "lucide-react"
import { buildTemplateDemoMessage, resolveTemplateAgents, useCaseTemplates } from "@/lib/use-case-templates"
import type { Agent } from "@/lib/types/agent"
import type { UseCaseAgent, UseCaseTemplate } from "@/lib/use-case-templates"

function makeAgent(id: string, name: string): Agent {
  return {
    agent_id: id,
    agent_card: {
      name,
      description: "",
      url: `https://example.com/${id}`,
      version: "1.0.0",
      provider: { organization: "test", url: "https://test.com" },
      capabilities: {},
      protocolVersion: "1.0.0",
      skills: [],
      defaultInputModes: ["text"],
      defaultOutputModes: ["text"],
    },
  }
}

describe("resolveTemplateAgents", () => {
  const catalog: Agent[] = [
    makeAgent("agent-001", "YouTube Creator Finder Agent"),
    makeAgent("agent-002", "GPT-5-mini Agent"),
    makeAgent("agent-003", "travel planner Agent"),
  ]

  it("resolves agents by agentId match", () => {
    const templateAgents: UseCaseAgent[] = [
      { agentId: "agent-001", agentName: "YouTube Creator Finder Agent" },
    ]
    const result = resolveTemplateAgents(templateAgents, catalog)
    expect(result).toEqual([catalog[0]])
  })

  it("falls back to case-insensitive agentName when agentId not found", () => {
    const templateAgents: UseCaseAgent[] = [
      { agentId: "wrong-id", agentName: "youtube creator finder agent" },
    ]
    const result = resolveTemplateAgents(templateAgents, catalog)
    expect(result).toEqual([catalog[0]])
  })

  it("throws when any agent fails to resolve", () => {
    const templateAgents: UseCaseAgent[] = [
      { agentId: "agent-001", agentName: "YouTube Creator Finder Agent" },
      { agentId: "nonexistent", agentName: "Nonexistent Agent" },
    ]
    expect(() => resolveTemplateAgents(templateAgents, catalog)).toThrow(
      "Some agents in this template are unavailable"
    )
  })

  it("resolves multiple agents in order", () => {
    const templateAgents: UseCaseAgent[] = [
      { agentId: "agent-002", agentName: "GPT-5-mini Agent" },
      { agentId: "agent-001", agentName: "YouTube Creator Finder Agent" },
    ]
    const result = resolveTemplateAgents(templateAgents, catalog)
    expect(result).toEqual([catalog[1], catalog[0]])
  })

  it("resolves with empty catalog throws", () => {
    const templateAgents: UseCaseAgent[] = [
      { agentId: "agent-001", agentName: "YouTube Creator Finder Agent" },
    ]
    expect(() => resolveTemplateAgents(templateAgents, [])).toThrow()
  })
})

describe("buildTemplateDemoMessage", () => {
  const template: UseCaseTemplate = {
    id: "travel-planner",
    icon: Palmtree,
    title: "Travel Planner",
    description: "Plan a trip",
    agents: [
      { agentId: "Weather Agent", agentName: "Weather Agent" },
      { agentId: "Travel Planner Agent", agentName: "Travel Planner Agent" },
    ],
    prefillMessage: "Generate a travel plan",
  }

  it("prefixes mentions and slices the typed prompt", () => {
    expect(buildTemplateDemoMessage(template, 0)).toBe(
      "<@Weather Agent|Weather Agent><@Travel Planner Agent|Travel Planner Agent>",
    )
    expect(buildTemplateDemoMessage(template, 8)).toBe(
      "<@Weather Agent|Weather Agent><@Travel Planner Agent|Travel Planner Agent> Generate",
    )
  })
})

describe("featured use cases", () => {
  it("includes the hybro travel and story templates, not SaaS creator discovery", () => {
    expect(useCaseTemplates.map((t) => t.id)).toEqual(["travel-planner", "story-and-image"])
  })

  it("asks the travel agents for a supported location-specific forecast", () => {
    const travel = useCaseTemplates.find((template) => template.id === "travel-planner")

    expect(travel?.prefillMessage).toBe(
      "Plan a 7-day trip to Oahu, Hawaii, for four people, and check Honolulu's weather forecast for the next 7 days",
    )
    expect(travel?.prefillMessage).not.toContain("past month")
  })
})

'use client'
import { useState, useRef } from 'react'
import { ChevronDown, Globe, Users, Loader2, Pencil, Trash2, Plus, Waypoints } from 'lucide-react'
import { getAgentAvatarUri } from '@/lib/agent-avatar'
import { Button } from '@/components/ui/button'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from '@/components/ui/tooltip'
import { cn } from '@/lib/utils'
import type { AgentGroup } from '@/lib/types/agent-group'
import {
  BUILTIN_GROUP_ALL_AGENTS,
  BUILTIN_GROUP_ROOM_TEAM,
} from '@/lib/types/agent-group'

interface MentionedAgent {
  id: string
  name: string
}

interface GroupSelectorProps {
  selectedGroup: string
  selectedGroupName?: string
  /**
   * When set, the menu offers an explicit room-membership row (room_team).
   * All Agents always means network broadcast.
   */
  roomMembershipLabel?: string
  onGroupChange: (groupId: string) => void
  groups: AgentGroup[]
  loadingGroups?: boolean
  mentionedAgents?: MentionedAgent[]
  onCreateGroup?: () => void
  onEditGroup?: (group: AgentGroup) => void
  onDeleteGroup?: (group: AgentGroup) => void
  agentNameMap?: Record<string, string>
  className?: string
  disabled?: boolean
  /** When true, the dropdown opens normally but item clicks are no-ops. */
  readOnly?: boolean
}

export function GroupSelector({
  selectedGroup,
  selectedGroupName,
  roomMembershipLabel,
  onGroupChange,
  groups,
  loadingGroups = false,
  mentionedAgents = [],
  onCreateGroup,
  onEditGroup,
  onDeleteGroup,
  agentNameMap = {},
  className,
  disabled = false,
  readOnly = false,
}: GroupSelectorProps) {
  const hasMentions = mentionedAgents.length > 0

  const [tooltipOpen, setTooltipOpen] = useState(false)
  const [menuOpen, setMenuOpen] = useState(false)
  const ignoreTooltipRef = useRef(false)

  const handleTooltipOpenChange = (isOpen: boolean) => {
    if (menuOpen) {
      setTooltipOpen(false)
      return
    }
    if (isOpen && ignoreTooltipRef.current) {
      setTooltipOpen(false)
      return
    }
    setTooltipOpen(isOpen)
  }

  const handleGroupSelect = (groupId: string) => {
    ignoreTooltipRef.current = true
    setTooltipOpen(false)
    setMenuOpen(false)
    onGroupChange(groupId)
    setTimeout(() => {
      ignoreTooltipRef.current = false
    }, 500)
  }

  // Get the display info for current selection.
  const getDisplayInfo = () => {
    if (hasMentions) {
      return {
        icon: <Users className="h-3.5 w-3.5" />,
        label: mentionedAgents.length === 1
          ? `@${mentionedAgents[0].name}`
          : `${mentionedAgents.length} agents mentioned`,
        description: 'Mentioned agents',
      }
    }

    if (selectedGroup === BUILTIN_GROUP_ROOM_TEAM) {
      return {
        icon: <Users className="h-3.5 w-3.5" />,
        label: selectedGroupName ?? roomMembershipLabel ?? 'Room agents',
        description: 'Agents in this room',
      }
    }

    if (selectedGroup === BUILTIN_GROUP_ALL_AGENTS) {
      return {
        icon: <Globe className="h-3.5 w-3.5 text-icon-network" />,
        label: 'All agents',
        description: 'All currently available agents',
      }
    }

    // Saved team
    const customGroup = groups.find(g => g.group_id === selectedGroup)
    if (customGroup) {
      return {
        icon: <Waypoints className="h-3.5 w-3.5 text-hybro-bro" />,
        label: customGroup.name,
        description: `${customGroup.agents.length} agents`,
      }
    }

    // Preserve persisted provenance while the team catalog is loading or unavailable.
    if (selectedGroupName) {
      return {
        icon: <Waypoints className="h-3.5 w-3.5 text-hybro-bro" />,
        label: selectedGroupName,
        description: 'Room subnet',
      }
    }

    return {
      icon: <Globe className="h-3.5 w-3.5 text-icon-network" />,
      label: 'All agents',
      description: 'All currently available agents',
    }
  }

  const displayInfo = getDisplayInfo()

  // Filter groups by type
  const userGroups = groups.filter(g => g.type === 'user')

  if (loadingGroups && !selectedGroupName) {
    return (
      <div className={cn("flex items-center gap-2 px-3 py-1.5 text-sm text-muted-foreground", className)}>
        <Loader2 className="h-3.5 w-3.5 animate-spin" />
        <span>Loading subnets…</span>
      </div>
    )
  }

  // If mentions are present, show avatars + count
  if (hasMentions) {
    const visibleAvatars = mentionedAgents.slice(0, 3)
    return (
      <div className={cn("flex min-w-0 items-center", className)}>
        <div className="flex h-8 min-w-0 items-center gap-2 whitespace-nowrap px-3">
          <div className="flex items-center -space-x-1.5 shrink-0">
            {visibleAvatars.map((agent) => (
              /* eslint-disable-next-line @next/next/no-img-element */
              <img
                key={agent.id}
                src={getAgentAvatarUri(agent.id)}
                alt={agent.name}
                className="h-4 w-4 rounded-full border-[1.5px] border-background object-cover"
              />
            ))}
          </div>
          <span className="text-sm text-muted-foreground font-medium">
            {mentionedAgents.length} {mentionedAgents.length === 1 ? 'agent' : 'agents'}
          </span>
        </div>
      </div>
    )
  }

  return (
    <div className={cn("flex min-w-0 items-center gap-1", className)}>
      <TooltipProvider delayDuration={100}>
        <DropdownMenu open={menuOpen} onOpenChange={(open) => {
          setMenuOpen(open)
          if (open) {
            setTooltipOpen(false)
            ignoreTooltipRef.current = true
          } else {
            setTimeout(() => {
              ignoreTooltipRef.current = false
            }, 300)
          }
        }}>
          <Tooltip open={menuOpen ? false : tooltipOpen} onOpenChange={handleTooltipOpenChange}>
            <TooltipTrigger asChild>
              <DropdownMenuTrigger asChild disabled={disabled}>
                <Button
                  variant="ghost"
                  size="sm"
                  className="h-8 min-h-8 min-w-0 max-w-full flex-1 px-3 gap-1.5 font-normal hover:bg-muted/50 flex items-center border-none shadow-none focus-visible:ring-0 focus-visible:border-transparent"
                  onMouseLeave={() => {
                    ignoreTooltipRef.current = false
                  }}
                >
                  <span className="inline-flex h-4 w-4 shrink-0 items-center justify-center text-muted-foreground">
                    {displayInfo.icon}
                  </span>
                  <span className="min-w-0 truncate text-left font-medium">{displayInfo.label}</span>
                  <ChevronDown className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
                </Button>
              </DropdownMenuTrigger>
            </TooltipTrigger>
            <TooltipContent side="top">
              Select agents
            </TooltipContent>
          </Tooltip>
          <DropdownMenuContent
            side="top"
            align="start"
            className="w-[min(90vw,18rem)] sm:w-72 sm:max-w-88 border border-border/50 shadow-lg z-50 bg-background/95 backdrop-blur-md max-h-[70vh] sm:max-h-72 overflow-hidden overflow-x-hidden p-0 pb-1"
          >
            <div className="max-h-[calc(70vh-3rem)] sm:max-h-60 overflow-y-auto overflow-x-hidden">

              {/* Room membership — distinct from All Agents broadcast */}
              {roomMembershipLabel && (
                <Tooltip delayDuration={150}>
                  <TooltipTrigger asChild>
                    <DropdownMenuItem
                      onClick={readOnly ? undefined : () => handleGroupSelect(BUILTIN_GROUP_ROOM_TEAM)}
                      className={cn(
                        "flex items-start gap-3 py-2.5",
                        selectedGroup === BUILTIN_GROUP_ROOM_TEAM && "bg-accent",
                        readOnly && 'cursor-default',
                      )}
                    >
                      <Users className="h-4 w-4 mt-0.5 text-muted-foreground" />
                      <div className="flex-1 min-w-0">
                        <div className="font-medium truncate">{roomMembershipLabel}</div>
                        <div className="text-xs text-muted-foreground">Agents in this room</div>
                      </div>
                    </DropdownMenuItem>
                  </TooltipTrigger>
                  <TooltipContent
                    side="left"
                    align="end"
                    sideOffset={0}
                    alignOffset={0}
                    className="max-w-xs w-fit whitespace-normal wrap-break-word"
                  >
                    <div className="text-xs text-muted-foreground">
                      Route to the agents currently assigned to this room
                    </div>
                  </TooltipContent>
                </Tooltip>
              )}

              {/* All Agents — always network broadcast */}
              <Tooltip delayDuration={150}>
                <TooltipTrigger asChild>
                  <DropdownMenuItem
                    onClick={readOnly ? undefined : () => handleGroupSelect(BUILTIN_GROUP_ALL_AGENTS)}
                    className={cn(
                      "flex items-start gap-3 py-2.5",
                      selectedGroup === BUILTIN_GROUP_ALL_AGENTS && "bg-accent",
                      readOnly && 'cursor-default',
                    )}
                  >
                    <Globe className="h-4 w-4 mt-0.5 text-icon-network" />
                    <div className="flex-1">
                      <div className="font-medium">All agents</div>
                    </div>
                  </DropdownMenuItem>
                </TooltipTrigger>
                <TooltipContent
                  side="left"
                  align="end"
                  sideOffset={0}
                  alignOffset={0}
                  className="max-w-xs w-fit whitespace-normal wrap-break-word"
                >
                  <div className="text-xs text-muted-foreground">All currently available agents as candidate scope</div>
                </TooltipContent>
              </Tooltip>

              {/* User groups */}
              {userGroups.length > 0 && (
                <>
                  <DropdownMenuSeparator />
                  {userGroups.map(group => (
                    <Tooltip key={group.group_id} delayDuration={150}>
                      <TooltipTrigger asChild>
                        <DropdownMenuItem
                          onClick={readOnly ? undefined : () => handleGroupSelect(group.group_id)}
                          className={cn(
                            "flex items-start gap-3 py-2.5",
                            selectedGroup === group.group_id && "bg-accent",
                            readOnly && 'cursor-default',
                          )}
                        >
                          <Waypoints className="h-4 w-4 mt-0.5 text-hybro-bro" />
                          <div className="flex-1 min-w-0">
                            <div className="font-medium truncate">{group.name}</div>
                            <div className="text-xs text-muted-foreground truncate">
                              {group.agents.length} agent{group.agents.length !== 1 ? 's' : ''}
                            </div>
                          </div>
                          {(onEditGroup || onDeleteGroup) && (
                            <div className="flex items-center gap-1 mt-0.5">
                              {onEditGroup && (
                                <Button
                                  variant="ghost"
                                  size="icon"
                                  className="h-7 w-7"
                                  title={`Edit ${group.name}`}
                                  onClick={(e) => {
                                    e.preventDefault()
                                    e.stopPropagation()
                                    onEditGroup(group)
                                  }}
                                  onMouseDown={(e) => e.stopPropagation()}
                                >
                                  <Pencil className="h-4 w-4" />
                                </Button>
                              )}
                              {onDeleteGroup && (
                                <Button
                                  variant="ghost"
                                  size="icon"
                                  className="h-7 w-7 text-destructive hover:bg-destructive/10 hover:text-destructive"
                                  title={`Delete ${group.name}`}
                                  onClick={(e) => {
                                    e.preventDefault()
                                    e.stopPropagation()
                                    onDeleteGroup(group)
                                  }}
                                  onMouseDown={(e) => e.stopPropagation()}
                                >
                                  <Trash2 className="h-4 w-4" />
                                </Button>
                              )}
                            </div>
                          )}
                        </DropdownMenuItem>
                      </TooltipTrigger>
                      <TooltipContent
                        side="left"
                        align="end"
                        sideOffset={0}
                        alignOffset={0}
                        className="max-w-xs w-fit whitespace-normal wrap-break-word"
                      >
                        {group.agents.length === 0 ? (
                          <div className="text-xs text-muted-foreground">No agents in this subnet</div>
                        ) : (
                          <div className="space-y-0.5 max-h-40 overflow-y-auto">
                            {group.agents.map(agentId => (
                              <div key={agentId} className="text-xs text-muted-foreground">
                                {agentNameMap[agentId] || 'Unavailable agent'}
                              </div>
                            ))}
                          </div>
                        )}
                      </TooltipContent>
                    </Tooltip>
                  ))}
                </>
              )}
            </div>

            {/* Create group (sticky footer) */}
            {onCreateGroup && (
              <>
                <DropdownMenuSeparator />
                <DropdownMenuItem
                  onClick={(e) => {
                    e.preventDefault()
                    onCreateGroup()
                  }}
                  className="text-foreground font-medium gap-2 py-2.5 px-3"
                >
                  <Plus className="h-4 w-4" />
                  Create subnet
                </DropdownMenuItem>
              </>
            )}
          </DropdownMenuContent>
        </DropdownMenu>
      </TooltipProvider>
    </div>
  )
}

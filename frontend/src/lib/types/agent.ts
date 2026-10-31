/**
 * Agent-related types.
 *
 * A2A protocol types (AgentCard, AgentSkill, security schemes, etc.) are
 * imported from the canonical @a2a-js/sdk package.  Only backend-specific
 * agent wrapper types are defined here.
 */

// ── Re-export A2A protocol types from SDK ────────────────────────────────
export type {
  AgentCard,
  AgentCapabilities,
  AgentExtension,
  AgentProvider,
  AgentSkill,
  SecurityScheme,
  APIKeySecurityScheme,
  HTTPAuthSecurityScheme,
  OAuth2SecurityScheme,
  OpenIdConnectSecurityScheme,
  OAuthFlows,
  AuthorizationCodeOAuthFlow,
  ClientCredentialsOAuthFlow,
  ImplicitOAuthFlow,
  PasswordOAuthFlow,
} from '@a2a-js/sdk'

// ── Types NOT in SDK ─────────────────────────────────────────────────────

/**
 * The location of the API key.
 * (Not exported from @a2a-js/sdk as a standalone type.)
 */
export type In = "cookie" | "header" | "query";

export type AgentStatus = "active" | "inactive" | "deleted";

/** Why a bundled agent is inactive until setup provides what it needs. */
export type AgentInactiveReason = "provider_not_configured" | "image_model_not_configured";

/** One advertised agent skill, as the `/agent/*` routes return it. */
export interface AgentSkillView {
  id: string;
  name: string;
  description?: string | null;
  tags: string[];
  examples: string[];
}

export interface AgentCapabilitiesView {
  streaming: boolean;
  push_notifications: boolean;
  state_transition_history: boolean;
  extensions: boolean;
}

/** The public part of an agent card. Transport URLs are never included. */
export interface AgentCardView {
  name: string;
  description?: string | null;
  version?: string | null;
  provider_organization?: string | null;
  icon_url?: string | null;
  documentation_url?: string | null;
  skills: AgentSkillView[];
  capabilities: AgentCapabilitiesView;
  default_input_modes: string[];
  default_output_modes: string[];
}

/** An agent as the `/agent/*` routes return it (backend `AgentView`). */
export interface Agent extends AgentCardView {
  agent_id: string;
  provider_id?: string | null;
  /** Display name resolved from provider_id when the card names no provider. */
  provider_name?: string | null;
  status: AgentStatus;
  /** `bundled` is a sample agent the backend registers at boot. */
  source: "cloud" | "local" | "bundled";
  /** Set while a bundled agent is inactive for lack of setup. */
  inactive_reason?: AgentInactiveReason | null;
  is_public: boolean;
  public_url?: string | null;
  call_count: number;
  /** Maximum requests per user per hour (null = unlimited) */
  rate_limit_per_user_per_hour?: number | null;
  /** Maximum requests for entire system per hour (null = unlimited) */
  rate_limit_system_per_hour?: number | null;
}

/** Envelope for the `/agent/*` routes (backend `AgentRouteResponse`). */
export interface AgentRouteResponse {
  agent?: Agent | null;
  agents?: Agent[] | null;
  card?: AgentCardView | null;
  success: boolean;
  error?: string | null;
  error_code?: string | null;
  status_code?: number;
}

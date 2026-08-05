/** Shared REST/domain types for the Legal Helper backend. */

export type ProviderId = "anthropic" | "openai";
export type ReasoningEffort = "none" | "low" | "medium" | "high" | "xhigh";

export type ChatSettings = {
  provider: ProviderId;
  model: string;
  reasoning_effort?: ReasoningEffort;
  skill_hint?: string | null;
  enable_web_search?: boolean;
  enable_web_fetch?: boolean;
};

export type ChatSummary = {
  id: string;
  title: string;
  created_at: string;
  updated_at: string;
  archived?: boolean;
  project_id?: string | null;
  settings?: ChatSettings;
};

export type RuntimeOptions = {
  provider: ProviderId;
  providers: ProviderId[];
  models: Record<ProviderId, string[]>;
  reasoning_efforts: ReasoningEffort[];
  defaults: ChatSettings;
  skills: SkillInfo[];
  /** per-model context windows (input tokens) for the sidebar Context ring */
  context_windows?: Record<string, number>;
  default_context_window?: number;
};

export type SkillInfo = {
  name: string;
  description?: string;
  argument_hint?: string;
};

export type ProjectSummary = {
  id: string;
  name: string;
  slug?: string;
  jurisdiction?: string;
  domain_packs?: string[];
  brief?: string;
  memory_summary?: string;
};

export type UsageTotals = {
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  cache_write_tokens: number;
  total_tokens: number;
  requests: number;
};

export type UsageByModel = {
  provider: string;
  model: string;
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  cache_write_tokens: number;
  requests: number;
  estimated_cost_usd: number;
};

export type UsageSummary = {
  month: string;
  months?: string[];
  totals: UsageTotals;
  estimated_cost_usd: number;
  by_model: UsageByModel[];
  note?: string;
};

export type ActiveRun = {
  id: string;
  status: string;
  started_at?: string;
};

export type ChatDetail = {
  chat: ChatSummary;
  messages: Array<{
    id: string;
    role: "user" | "assistant" | "system";
    content: string;
    created_at: string;
    metadata?: Record<string, unknown>;
  }>;
  events: Array<{
    id: string;
    event_type: string;
    agent_name?: string;
    run_id?: string;
    created_at: string;
    data?: Record<string, unknown>;
  }>;
  artifacts: Array<{ id: string; filename: string; url?: string; origin?: string; message_id?: string }>;
  active_runs: ActiveRun[];
};

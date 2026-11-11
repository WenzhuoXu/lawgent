/** Shared REST/domain types for the Legal Helper backend. */

/** `claude_code`: the turn runs inside Claude Code on the claude.ai subscription. */
export type ProviderId = "anthropic" | "openai" | "claude_code";
export type ReasoningEffort = "none" | "low" | "medium" | "high" | "xhigh" | "max";
export type AnswerLength = "auto" | "brief" | "focused" | "full";

export type ChatSettings = {
  provider: ProviderId;
  model: string;
  reasoning_effort?: ReasoningEffort;
  answer_length?: AnswerLength;
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
  /** Claude Code turns: billed to the subscription; this is the API list price. */
  subscription_equivalent_usd?: number;
};

/** One limit window of the claude.ai plan (what Claude Code's `/usage` shows). */
export type PlanLimit = {
  /** e.g. "session" (the 5-hour window) or a weekly window */
  kind: string;
  group?: string | null;
  /** percent of the window used, 0–100 */
  percent?: number | null;
  /** ISO time the window resets */
  resets_at?: string | null;
  severity?: string;
  is_active?: boolean;
  scope?: string | null;
};

/** The claude.ai plan's usage, read from Claude Code (provider claude_code). */
export type PlanUsage = {
  source: "claude_code";
  subscription_type?: string | null;
  available?: boolean;
  limits: PlanLimit[];
  extra_usage?: {
    enabled?: boolean;
    utilization?: number | null;
    used_credits?: number | null;
    monthly_limit?: number | null;
    currency?: string | null;
  };
  fetched_at?: string;
};

export type UsageSummary = {
  month: string;
  months?: string[];
  totals: UsageTotals;
  estimated_cost_usd: number;
  subscription_equivalent_usd?: number;
  /** the plan's limits; null when Claude Code could not be asked */
  subscription?: PlanUsage | null;
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

/**
 * Typed schemas for the custom `data-*` message parts the transport emits for
 * everything that is NOT plain text / reasoning / a tool call. AI SDK renders
 * these via `message.parts` of type `data-<name>`; we render them with our own
 * part renderers (Task/Plan, AuditCard, ChainOfThought, artifacts, …).
 *
 * Kept as a standalone module so both the live translator (api/sse.ts) and the
 * rehydration builder (lib/eventsToUIMessages.ts) share one source of truth.
 */

export type PlanTask = {
  id: string;
  skillName?: string;
  title?: string;
  detail?: string;
  dependsOn?: string[];
  /** pending → running → complete | error */
  status: "pending" | "running" | "complete" | "error";
  /** live rolling preview tail (agent_delta), transient */
  tail?: string;
  totalChars?: number;
  responseChars?: number;
  toolCount?: number;
  durationMs?: number;
  /** retrieved-source chips for this step (FIG1 cot-srb) */
  sources?: { label: string; kind?: string }[];
};

export type PlanData = {
  title?: string;
  executionMode?: string;
  complexity?: "simple" | "standard" | "complex" | string;
  routingReason?: string;
  userLanguage?: string;
  taskIds: string[];
};

export type CiteCheckData = {
  stage?: string;
  current?: number;
  total?: number;
  message?: string;
  toolName?: string;
};

/** One per-citation row from the cite-check report (FIG4 inspector rows). */
export type AuditItem = {
  location: string;
  issueType: string;
  severity: "critical" | "nuanced" | "model_only" | string;
  claimed: string;
  sourceSays: string;
  fix?: string;
  jurisdiction?: string;
};

/** Structured coverage counts from the cite-check report. */
export type AuditCoverage = {
  totalCites?: number;
  confirmed?: number;
  couldNotCheck?: number;
  miscited?: number;
  misgrounded?: number;
};

export type AuditData = {
  ok?: boolean;
  skipped?: boolean;
  skipReason?: string;
  doNotFile?: boolean;
  doNotFileReason?: string;
  coverageLine?: string;
  coverage?: AuditCoverage;
  items?: AuditItem[];
  warnings?: string[];
  reportMarkdown?: string;
  error?: string;
};

export type CompactionData = {
  reason?: string;
  lookback?: number;
  transcriptMessages?: number;
};

export type MemoryData = { summary?: string };

export type ArtifactData = {
  id: string;
  filename: string;
  url: string;
  origin?: string;
  messageId?: string;
};

/** Per-turn token usage + wall-clock (drives the sidebar Context ring, the
 * collapsed run header, and the Actions duration chip). */
export type UsageData = {
  inputTokens?: number;
  outputTokens?: number;
  cacheReadTokens?: number;
  cacheWriteTokens?: number;
  /** prompt tokens actually sent this turn = context-window fill */
  contextTokens?: number;
  durationMs?: number;
  provider?: string;
  model?: string;
};

export type PacksData = { packs: string[] };

export type DegradedData = { failedTaskIds?: string[]; message?: string };

export type ErrorData = { message: string; outOfMoney?: boolean };

/** Live phase of the current turn — drives the status line while the model
 * plans/routes before any text or tool call is visible. */
export type PhaseId =
  | "routing"
  | "planning"
  | "gathering"
  | "integrating"
  | "auditing";

export type PhaseData = { phase: PhaseId; detail?: string };

/** The `data` part payload map — keys become `data-<key>` part types. */
export type LegalDataParts = {
  plan: PlanData;
  agentTask: PlanTask;
  citeCheck: CiteCheckData | null;
  audit: AuditData;
  compaction: CompactionData;
  memory: MemoryData;
  artifact: ArtifactData;
  packs: PacksData;
  degraded: DegradedData;
  error: ErrorData;
  phase: PhaseData;
  usage: UsageData;
};

/** Metadata carried on assistant messages (duration, run id, provider). */
export type LegalMessageMetadata = {
  runId?: string;
  provider?: string;
  model?: string;
  createdAt?: string;
  finishedAt?: string;
  durationMs?: number;
  reasoningPreview?: string;
  reasoningChars?: number;
};

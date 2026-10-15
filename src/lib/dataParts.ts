/**
 * Typed schemas for the custom `data-*` message parts the transport emits for
 * everything that is NOT plain text / reasoning / a tool call. AI SDK renders
 * these via `message.parts` of type `data-<name>`; we render them with our own
 * part renderers (WorkflowCard, AuditCard, artifacts, …).
 *
 * Kept as a standalone module so both the live translator (api/sse.ts) and the
 * rehydration builder (lib/eventsToUIMessages.ts) share one source of truth.
 */

/** One tool call a workflow specialist made, nested under its row. */
export type SubTool = {
  name: string;
  status: "running" | "complete" | "error";
  detail?: string;
};

/** One sub-agent of a tool call: a `run_workflow` specialist, or the
 * procedure behind `create_deck` / `run_procedure`. */
export type SpecialistTask = {
  id: string;
  /** the tool part this sub-agent belongs to */
  parentCallId: string;
  skillName?: string;
  title?: string;
  detail?: string;
  /** pending → running → complete | error */
  status: "pending" | "running" | "complete" | "error";
  /** completed | truncated | empty | refused | failed | reused | cancelled */
  outcome?: string;
  /** live rolling preview tail (agent_delta), transient */
  tail?: string;
  /** the latest action, e.g. "Searching the web · 民法典 第577条" */
  activity?: string;
  toolCount?: number;
  totalTokens?: number;
  durationMs?: number;
  elapsedMs?: number;
  error?: string;
  /** pages authored so far, and the target when one was set (decks) */
  current?: number;
  total?: number;
  tools?: SubTool[];
  /** retrieved-source chips for this step (FIG1 cot-srb) */
  sources?: { label: string; kind?: string }[];
};

/** A `run_workflow` call's objective and wall-clock, keyed by its tool part. */
export type WorkflowData = {
  partId: string;
  objective?: string;
  durationMs?: number;
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

/** Live phase of the current turn — drives the status line shown while the
 * turn works; cleared once the answer text begins. */
export type PhaseId =
  | "thinking"
  | "gathering"
  | "workflow"
  | "drafting"
  | "auditing";

export type PhaseData = { phase: PhaseId; detail?: string };

/** The `data` part payload map — keys become `data-<key>` part types. */
export type LegalDataParts = {
  workflow: WorkflowData;
  agentTask: SpecialistTask;
  citeCheck: CiteCheckData | null;
  audit: AuditData;
  compaction: CompactionData;
  memory: MemoryData;
  artifact: ArtifactData;
  packs: PacksData;
  degraded: DegradedData;
  phase: PhaseData;
  usage: UsageData;
};

/** Metadata carried on assistant messages (duration, run id, provider). */
export type LegalMessageMetadata = {
  runId?: string;
  provider?: string;
  model?: string;
  createdAt?: string;
  durationMs?: number;
};

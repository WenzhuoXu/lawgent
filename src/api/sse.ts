/**
 * Pure SSE parsing + translation of the Legal Helper Python backend's event
 * stream into AI SDK v5/v6 `UIMessageChunk`s. No DOM, no network — unit-testable
 * against recorded fixtures (tests/fixtures/frontend/*.sse).
 *
 * The translator is a small state machine (one instance per streamed turn).
 * Facts it relies on (verified against fixtures + server.py):
 *  - `delta` carries INCREMENTAL text (append per token) → one text-delta each.
 *  - `workflow_event` is an envelope; use normalizeEvent() to unwrap.
 *  - assistant message id arrives up-front via `message_saved` (role=assistant)
 *    and is repeated on `done`.
 *  - OpenAI local tool call = `tool_call` (direct) + `local_tool_call_started` +
 *    `local_tool_call_finished` (enveloped). Anthropic (pre-fix) = `tool_call`
 *    only; (post-fix) = same trio. `hosted_tool_call` is emitted twice
 *    (enveloped + direct) → dedupe.
 */

import { normalizeEvent } from "./events";

// Minimal UIMessageChunk typing — matches AI SDK's stream part union closely
// enough for our transport; the SDK validates on ingest.
export type UIMessageChunk =
  | { type: "start"; messageId?: string }
  | { type: "finish" }
  | { type: "error"; errorText: string }
  | { type: "text-start"; id: string }
  | { type: "text-delta"; id: string; delta: string }
  | { type: "text-end"; id: string }
  | { type: "reasoning-start"; id: string }
  | { type: "reasoning-delta"; id: string; delta: string }
  | { type: "reasoning-end"; id: string }
  | { type: "tool-input-available"; toolCallId: string; toolName: string; input: unknown }
  | { type: "tool-output-available"; toolCallId: string; output: unknown }
  | { type: "tool-output-error"; toolCallId: string; errorText: string }
  | { type: `data-${string}`; id?: string; data: unknown };

export type TranslatorState = {
  assistantId: string | null;
  sawStart: boolean;
  textId: string | null;
  textOpen: boolean;
  reasoningId: string | null;
  reasoningOpen: boolean;
  /** FIFO of open tool-call ids per `${agent}:${tool}` key */
  openToolIds: Map<string, string[]>;
  toolSeq: Map<string, number>;
  /** dedupe hosted tool calls by tool_id */
  hostedSeen: Set<string>;
  /** have we emitted a phase status yet this turn */
  sawPhase: boolean;
  finished: boolean;
};

export function createTranslatorState(): TranslatorState {
  return {
    assistantId: null,
    sawStart: false,
    textId: null,
    textOpen: false,
    reasoningId: null,
    reasoningOpen: false,
    openToolIds: new Map(),
    toolSeq: new Map(),
    hostedSeen: new Set(),
    sawPhase: false,
    finished: false,
  };
}

function toolKey(agent: string | undefined, name: string): string {
  return `${agent || "orchestrator"}:${name}`;
}

function looksLikeError(output: unknown): boolean {
  if (typeof output !== "string") return false;
  return /^\s*\{\s*"error"\s*:/.test(output);
}

/** Normalize backend retrieved-source chips ({label, kind}) to PlanTask.sources. */
function mapSources(raw: unknown): { label: string; kind?: string }[] | undefined {
  if (!Array.isArray(raw)) return undefined;
  const out: { label: string; kind?: string }[] = [];
  for (const s of raw) {
    if (s && typeof s === "object" && typeof (s as { label?: unknown }).label === "string") {
      const { label, kind } = s as { label: string; kind?: unknown };
      out.push(typeof kind === "string" ? { label, kind } : { label });
    }
  }
  return out.length ? out : undefined;
}

/** Open (or reuse) a tool call id for a key; returns the id. */
function openTool(state: TranslatorState, key: string): string {
  const seq = state.toolSeq.get(key) ?? 0;
  const id = `${key}#${seq}`;
  state.toolSeq.set(key, seq + 1);
  const open = state.openToolIds.get(key) ?? [];
  open.push(id);
  state.openToolIds.set(key, open);
  return id;
}

/** Pop the oldest open tool id for a key (FIFO); synthesize one if none open. */
function closeTool(state: TranslatorState, key: string): string {
  const open = state.openToolIds.get(key);
  if (open && open.length) {
    return open.shift() as string;
  }
  // No matching open call (e.g. Anthropic emitted no start, or hosted): make one.
  return openTool(state, key);
}

/**
 * Translate one normalized SSE (eventName, data) into zero or more
 * UIMessageChunks, mutating `state`.
 */
export function sseEventToChunks(
  state: TranslatorState,
  eventName: string,
  data: unknown,
): UIMessageChunk[] {
  const ev = normalizeEvent(eventName, data);
  const out: UIMessageChunk[] = [];
  const p = ev.payload;

  const ensureStart = (messageId?: string) => {
    if (!state.sawStart) {
      state.sawStart = true;
      if (messageId && !state.assistantId) state.assistantId = messageId;
      out.push({ type: "start", messageId: state.assistantId ?? messageId });
    }
  };
  const textId = () => (state.textId ??= `txt-${state.assistantId ?? "run"}`);
  const reasoningId = () => (state.reasoningId ??= `rsn-${state.assistantId ?? "run"}`);
  const closeText = () => {
    if (state.textOpen) {
      out.push({ type: "text-end", id: textId() });
      state.textOpen = false;
    }
  };
  const closeReasoning = () => {
    if (state.reasoningOpen) {
      out.push({ type: "reasoning-end", id: reasoningId() });
      state.reasoningOpen = false;
    }
  };

  switch (ev.type) {
    case "message_saved": {
      const role = p.role as string;
      if (role === "assistant" && p.id) {
        state.assistantId = p.id as string;
        ensureStart(p.id as string);
      }
      // user message is already optimistic in useChat → ignore.
      return out;
    }

    case "delta": {
      const text = (p.text as string) ?? "";
      if (!text) return out;
      ensureStart();
      closeReasoning();
      if (!state.textOpen) {
        out.push({ type: "text-start", id: textId() });
        state.textOpen = true;
        // Answer has begun — clear the planning status line.
        if (state.sawPhase) out.push({ type: "data-phase", id: "phase", data: null });
      }
      out.push({ type: "text-delta", id: textId(), delta: text });
      return out;
    }

    case "reasoning_delta": {
      const text = (p.text as string) ?? "";
      if (!text) return out;
      ensureStart();
      if (!state.reasoningOpen) {
        out.push({ type: "reasoning-start", id: reasoningId() });
        state.reasoningOpen = true;
      }
      out.push({ type: "reasoning-delta", id: reasoningId(), delta: text });
      return out;
    }

    case "provider_turn_finished": {
      closeReasoning();
      return out;
    }

    // --- Planning-phase lifecycle → live status so the assistant bubble shows
    // progress IMMEDIATELY instead of sitting empty during the (multi-second)
    // routing/planning LLM call. ensureStart() materializes the message now. ---
    case "provider_turn_started": {
      ensureStart(); // first event of the turn — show the bubble right away
      if (!state.sawPhase) {
        state.sawPhase = true;
        out.push({ type: "data-phase", id: "phase", data: { phase: "routing" } });
      }
      return out;
    }
    case "workflow_plan_routed": {
      ensureStart();
      state.sawPhase = true;
      const mode = p.execution_mode as string;
      out.push({
        type: "data-phase",
        id: "phase",
        data: {
          phase: mode === "agent_workflow" ? "planning" : "integrating",
          detail: p.routing_reason as string | undefined,
        },
      });
      return out;
    }
    case "workflow_plan_decomposed": {
      ensureStart();
      state.sawPhase = true;
      out.push({ type: "data-phase", id: "phase", data: { phase: "planning" } });
      return out;
    }
    case "skill_started": {
      ensureStart();
      state.sawPhase = true;
      out.push({ type: "data-phase", id: "phase", data: { phase: "gathering" } });
      return out;
    }
    case "workflow_direct_llm_started": {
      ensureStart();
      state.sawPhase = true;
      out.push({ type: "data-phase", id: "phase", data: { phase: "integrating" } });
      return out;
    }
    case "citation_audit_started": {
      ensureStart();
      state.sawPhase = true;
      out.push({ type: "data-phase", id: "phase", data: { phase: "auditing" } });
      return out;
    }

    case "tool_call": {
      ensureStart();
      const name = p.name as string;
      if (!name) return out;
      const key = toolKey(ev.agent ?? (p.agent as string), name);
      const id = openTool(state, key);
      out.push({
        type: "tool-input-available",
        toolCallId: id,
        toolName: name,
        input: p.arguments ?? {},
      });
      return out;
    }

    case "local_tool_call_started": {
      // OpenAI refinement: only open if `tool_call` didn't already open one.
      const name = (p.tool_name as string) ?? (p.name as string);
      if (!name) return out;
      const key = toolKey(ev.agent, name);
      const open = state.openToolIds.get(key);
      if (open && open.length) return out; // already opened by tool_call
      ensureStart();
      const id = openTool(state, key);
      out.push({
        type: "tool-input-available",
        toolCallId: id,
        toolName: name,
        input: p.arguments ?? {},
      });
      return out;
    }

    case "local_tool_call_finished": {
      const name = (p.tool_name as string) ?? (p.name as string);
      if (!name) return out;
      const key = toolKey(ev.agent, name);
      const id = closeTool(state, key);
      const preview = p.output_preview;
      if (looksLikeError(preview)) {
        out.push({ type: "tool-output-error", toolCallId: id, errorText: preview as string });
      } else {
        out.push({ type: "tool-output-available", toolCallId: id, output: preview ?? "" });
      }
      return out;
    }

    case "local_tool_call_failed": {
      const name = (p.tool_name as string) ?? (p.name as string);
      if (!name) return out;
      const key = toolKey(ev.agent, name);
      const id = closeTool(state, key);
      out.push({
        type: "tool-output-error",
        toolCallId: id,
        errorText: (p.message as string) ?? "tool failed",
      });
      return out;
    }

    case "hosted_tool_call": {
      const rawType = (p.raw_type as string) ?? "";
      // Anthropic emits TWO blocks per hosted search: the query-bearing
      // `server_tool_use` and a `*_tool_result` echo of the results. The echo
      // is not a distinct tool call — the workflow collector already excludes
      // it from `tool_count`, so surfacing it as its own tool card here would
      // both inflate the run trace and diverge from OpenAI (which emits no
      // echo). Drop it so the trace shows one card per real call on both paths.
      if (rawType.endsWith("_tool_result")) return out;
      const toolId = (p.tool_id as string) ?? "";
      const name = (p.tool_name as string) ?? "hosted_tool";
      const dedupeKey = toolId || toolKey(ev.agent, name);
      if (state.hostedSeen.has(dedupeKey)) return out; // emitted twice (envelope+direct)
      state.hostedSeen.add(dedupeKey);
      ensureStart();
      const id = `hosted:${dedupeKey}`;
      out.push({ type: "tool-input-available", toolCallId: id, toolName: name, input: { provider: p.provider, model: p.model } });
      out.push({ type: "tool-output-available", toolCallId: id, output: { tool_id: toolId } });
      return out;
    }

    case "workflow_plan": {
      const tasks = (p.agent_tasks as Array<Record<string, unknown>>) ?? [];
      // The plan's declared agent_tasks are only a PROPOSAL. The backend runs
      // them as visible sub-agents (emitting agent_task_started/finished) ONLY
      // on the genuine multi-agent path; the direct_answer and single-pass
      // paths answer inline and emit NO task lifecycle events. So we do NOT
      // seed checklist rows here — the plan card materializes only when real
      // agent_task_started events arrive (see below). This is the truthful
      // signal, not a task-count heuristic.
      out.push({
        type: "data-plan",
        id: "plan",
        data: {
          title: p.title,
          executionMode: p.execution_mode,
          complexity: p.complexity,
          routingReason: p.routing_reason,
          userLanguage: p.user_language,
          taskIds: tasks.map((t) => t.id as string),
        },
      });
      return out;
    }

    case "agent_task_started": {
      const id = (p.id as string) ?? (p.task_id as string);
      if (!id) return out;
      out.push({
        type: "data-agentTask",
        id,
        data: {
          id,
          skillName: p.skill_name,
          title: p.title ?? p.task,
          dependsOn: p.depends_on,
          detail: p.task,
          status: "running",
        },
      });
      return out;
    }

    case "agent_task_finished": {
      const id = p.task_id as string;
      if (!id) return out;
      out.push({
        type: "data-agentTask",
        id,
        data: {
          id,
          skillName: p.skill_name,
          status: "complete",
          responseChars: p.response_chars,
          toolCount: p.tool_count,
          durationMs: p.duration_ms,
          sources: mapSources(p.sources),
        },
      });
      return out;
    }

    case "agent_delta": {
      const id = p.task_id as string;
      if (!id) return out;
      out.push({
        type: "data-agentTask",
        id,
        data: {
          id,
          skillName: p.skill_name,
          status: "running",
          tail: p.tail,
          totalChars: p.total_chars,
          toolCount: p.tool_count,
          sources: mapSources(p.sources),
        },
      });
      return out;
    }

    case "cite_check_progress": {
      out.push({
        type: "data-citeCheck",
        id: "citeCheck",
        data: {
          stage: p.stage,
          current: p.current,
          total: p.total,
          message: p.message,
          toolName: p.tool_name,
        },
      });
      return out;
    }

    case "citation_audit":
    case "citation_audit_finished": {
      const rawItems = (p.items as Array<Record<string, unknown>>) ?? [];
      const items = rawItems.map((it) => ({
        location: it.location as string,
        issueType: it.issue_type as string,
        severity: it.severity as string,
        claimed: it.claimed as string,
        sourceSays: it.source_says as string,
        fix: it.fix as string | undefined,
        jurisdiction: it.jurisdiction as string | undefined,
      }));
      const rawCov = p.coverage as Record<string, unknown> | undefined;
      const coverage = rawCov
        ? {
            totalCites: rawCov.total_cites as number | undefined,
            confirmed: rawCov.confirmed as number | undefined,
            couldNotCheck: rawCov.could_not_check as number | undefined,
            miscited: rawCov.miscited as number | undefined,
            misgrounded: rawCov.misgrounded as number | undefined,
          }
        : undefined;
      out.push({
        type: "data-audit",
        id: "audit",
        data: {
          ok: p.ok,
          skipped: p.skipped,
          skipReason: p.skip_reason,
          doNotFile: p.do_not_file,
          doNotFileReason: p.do_not_file_reason,
          coverageLine: p.coverage_line,
          coverage,
          items: items.length ? items : undefined,
          warnings: p.warnings,
          reportMarkdown: p.report_markdown,
          error: p.error,
        },
      });
      // clear the transient progress card
      out.push({ type: "data-citeCheck", id: "citeCheck", data: null });
      return out;
    }

    case "citation_audit_skipped": {
      out.push({
        type: "data-audit",
        id: "audit",
        data: { skipped: true, skipReason: p.reason },
      });
      return out;
    }

    case "context_compaction": {
      out.push({
        type: "data-compaction",
        id: `compaction-${state.toolSeq.size}`,
        data: {
          reason: p.reason,
          lookback: p.lookback,
          transcriptMessages: p.transcript_messages,
        },
      });
      return out;
    }

    case "memory_updated": {
      out.push({ type: "data-memory", id: "memory", data: { summary: p.summary } });
      return out;
    }

    case "artifact": {
      const id = (p.id as string) ?? (p.filename as string);
      out.push({
        type: "data-artifact",
        id,
        data: {
          id,
          filename: (p.filename as string) ?? (p.name as string),
          url: p.url,
          origin: p.origin,
          messageId: p.message_id,
        },
      });
      return out;
    }

    case "active_domain_packs": {
      out.push({ type: "data-packs", id: "packs", data: { packs: p.packs ?? [] } });
      return out;
    }

    case "workflow_degraded": {
      out.push({
        type: "data-degraded",
        id: "degraded",
        data: { failedTaskIds: p.failed_task_ids, message: p.message },
      });
      return out;
    }

    case "error": {
      const message = (p.message as string) ?? "error";
      out.push({ type: "error", errorText: message });
      return out;
    }

    case "cancelled": {
      closeText();
      closeReasoning();
      if (!state.finished) {
        state.finished = true;
        out.push({ type: "finish" });
      }
      return out;
    }

    case "turn_usage": {
      // Per-turn token usage + wall-clock. Feeds the sidebar Context ring
      // (context_tokens) and the run-header / duration chip (durationMs).
      out.push({
        type: "data-usage",
        id: "usage",
        data: {
          inputTokens: p.input_tokens as number | undefined,
          outputTokens: p.output_tokens as number | undefined,
          cacheReadTokens: p.cache_read_tokens as number | undefined,
          cacheWriteTokens: p.cache_write_tokens as number | undefined,
          contextTokens: p.context_tokens as number | undefined,
          durationMs: p.duration_ms as number | undefined,
          provider: p.provider as string | undefined,
          model: p.model as string | undefined,
        },
      });
      return out;
    }

    case "done": {
      if (p.message_id && !state.assistantId) state.assistantId = p.message_id as string;
      closeText();
      closeReasoning();
      if (state.sawPhase) out.push({ type: "data-phase", id: "phase", data: null });
      if (!state.finished) {
        state.finished = true;
        out.push({ type: "finish" });
      }
      return out;
    }

    // heartbeat, skill_finished, plan-router notes, chat_updated, and other
    // structural log events carry no visible chunk.
    default:
      return out;
  }
}

/**
 * Incremental SSE frame parser. Feed it chunks of the response body text; it
 * calls `onEvent(name, data)` for each complete `\n\n`-terminated frame and
 * returns the unconsumed remainder to prepend next call. Ported from
 * main.jsx:139-157, typed.
 */
export function parseSSEBuffer(
  buffer: string,
  onEvent: (name: string, data: unknown) => void,
): string {
  const blocks = buffer.split("\n\n");
  const remainder = blocks.pop() ?? "";
  for (const block of blocks) {
    let event = "message";
    let data = "";
    for (const line of block.split("\n")) {
      if (line.startsWith("event:")) event = line.slice(6).trim();
      else if (line.startsWith("data:")) data += line.slice(5).trim();
    }
    if (!data) continue;
    try {
      onEvent(event, JSON.parse(data));
    } catch {
      onEvent(event, data);
    }
  }
  return remainder;
}

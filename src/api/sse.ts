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
 *  - A local tool call, on both providers, = `tool_call` (direct, carrying the
 *    provider's `call_id`) + `local_tool_call_started` + `local_tool_call_finished`
 *    (enveloped). A tool the server runs itself (message export) sends only
 *    the enveloped pair. `hosted_tool_call` is emitted twice (enveloped +
 *    direct) → dedupe.
 *  - A sub-agent runs inside one tool call: `run_workflow`'s specialists,
 *    `create_deck`'s ppt-master, `run_procedure`'s procedure. Every event it
 *    emits carries `task_id` and `parent_call_id` (the spawning call — the
 *    Agent SDK's `parent_tool_use_id` convention), so it nests under that tool
 *    part as a `data-agentTask` row instead of becoming a top-level tool card.
 *  - Reasoning is one part per segment: a tool call ends the segment, so the
 *    thoughts and the calls they led to alternate in stream order. Deltas are
 *    never stored; the `reasoning` event logged where a segment ends carries
 *    its whole text, which closes the streamed segment live and recreates it
 *    on reload.
 *  - Provider `claude_code` (the turn runs inside Claude Code) uses the same
 *    events, plus its own states: `claude_code_session` (model, login, tools),
 *    `todo_list` (its plan), `claude_code_status` (compacting, retrying),
 *    `rate_limit` (the window Claude Code reports while it runs), `plan_usage`
 *    (the plan's limits after the turn, as `/usage` shows them). Its sub-agents are the
 *    `Agent` tool: their calls nest under it exactly like a specialist's.
 *  - Answer text is segmented the same way. Text the model wrote before a tool
 *    call is commentary: the server ends it with a `commentary` event, which
 *    closes the streamed text part live (the next delta opens a new one) and
 *    recreates it on reload, each time with a `data-commentary` marker. The
 *    first N text parts of a turn, N the markers, are commentary; the rest is
 *    the answer (`answerText` in lib/foldChunks). The renderer shows commentary
 *    in the chain.
 */

import { normalizeEvent } from "./events";
import type { SpecialistTask, SubTool, WorkflowData } from "@/lib/dataParts";
import { displayToolName } from "@/lib/toolLabels";

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
  /** text segment counter: a `commentary` event ends one */
  textSeq: number;
  /** text streamed into the open segment, not yet ended by `commentary` */
  textStreamed: boolean;
  reasoningSeq: number;
  reasoningOpen: boolean;
  /** text streamed into the open (or just-closed) segment, not yet acknowledged
   * by its `reasoning` event */
  reasoningStreamed: boolean;
  /** FIFO of open tool-call ids per `${agent}:${tool}` key */
  openToolIds: Map<string, string[]>;
  toolSeq: Map<string, number>;
  /** dedupe hosted tool calls by tool_id */
  hostedSeen: Set<string>;
  /** have we emitted a phase status yet this turn */
  sawPhase: boolean;
  finished: boolean;
  /** provider call id → tool part id (pairs a specialist with its parent) */
  partByCallId: Map<string, string>;
  /** the part most recently opened per tool key */
  lastOpened: Map<string, string>;
  /** specialist snapshots by `${partId}:${taskId}` */
  tasks: Map<string, SpecialistTask>;
  workflows: Map<string, WorkflowData>;
  /** Claude Code's last reported status (requesting / compacting / retrying) */
  ccStatus: string | null;
};

export function createTranslatorState(): TranslatorState {
  return {
    assistantId: null,
    sawStart: false,
    textId: null,
    textOpen: false,
    textSeq: 0,
    textStreamed: false,
    reasoningSeq: 0,
    reasoningOpen: false,
    reasoningStreamed: false,
    openToolIds: new Map(),
    toolSeq: new Map(),
    hostedSeen: new Set(),
    sawPhase: false,
    finished: false,
    partByCallId: new Map(),
    lastOpened: new Map(),
    tasks: new Map(),
    workflows: new Map(),
    ccStatus: null,
  };
}

const WORKFLOW_TOOL = "run_workflow";

function toolKey(agent: string | undefined, name: string): string {
  return `${agent || "orchestrator"}:${name}`;
}

function looksLikeError(output: unknown): boolean {
  if (typeof output !== "string") return false;
  return /^\s*\{\s*"error"\s*:/.test(output);
}

/** Normalize backend retrieved-source chips ({label, kind}) to SpecialistTask.sources. */
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

/** Open a new tool part id for a key; returns the id. */
function openTool(state: TranslatorState, key: string): string {
  const seq = state.toolSeq.get(key) ?? 0;
  const id = `${key}#${seq}`;
  state.toolSeq.set(key, seq + 1);
  const open = state.openToolIds.get(key) ?? [];
  open.push(id);
  state.openToolIds.set(key, open);
  state.lastOpened.set(key, id);
  return id;
}

/** Remember which part a provider call id belongs to. */
function noteCallId(state: TranslatorState, key: string, callId: unknown): void {
  if (typeof callId !== "string" || !callId || state.partByCallId.has(callId)) return;
  const id = state.lastOpened.get(key);
  if (id) state.partByCallId.set(callId, id);
}

/** The `run_workflow` part a specialist event belongs to. */
function parentPart(state: TranslatorState, p: Record<string, unknown>): string | undefined {
  const call = p.parent_call_id;
  return typeof call === "string" ? state.partByCallId.get(call) : undefined;
}

/** A short argument summary for a nested tool row. */
function argDetail(args: unknown): string | undefined {
  if (!args || typeof args !== "object") return undefined;
  const a = args as Record<string, unknown>;
  for (const key of ["query", "q", "keyword", "title", "name", "url", "path", "script", "article", "section"]) {
    const v = a[key];
    if (typeof v === "string" && v.trim()) {
      const s = v.trim();
      return key === "path" || key === "script" ? s.split("/").pop() : s.slice(0, 80);
    }
  }
  return undefined;
}

/** Merge a patch into a specialist's snapshot and emit the whole row. */
function taskChunk(
  state: TranslatorState,
  partId: string | undefined,
  taskId: string,
  patch: Partial<SpecialistTask>,
): UIMessageChunk[] {
  if (!partId) return [];
  const key = `${partId}:${taskId}`;
  const prev = state.tasks.get(key) ?? { id: taskId, parentCallId: partId, status: "pending" as const };
  const next: SpecialistTask = { ...prev };
  for (const [k, v] of Object.entries(patch)) {
    if (v !== undefined) (next as Record<string, unknown>)[k] = v;
  }
  state.tasks.set(key, next);
  return [{ type: "data-agentTask", id: key, data: next }];
}

function taskOf(state: TranslatorState, partId: string | undefined, taskId: string): SpecialistTask | undefined {
  return partId ? state.tasks.get(`${partId}:${taskId}`) : undefined;
}

/** A specialist's own tool call, nested under its row. */
function subToolChunks(
  state: TranslatorState,
  p: Record<string, unknown>,
  name: string,
  status: SubTool["status"],
  detail?: string,
): UIMessageChunk[] {
  const taskId = p.task_id as string;
  const partId = parentPart(state, p);
  const tools = [...(taskOf(state, partId, taskId)?.tools ?? [])];
  if (status === "running") {
    tools.push({ name, status, detail });
  } else {
    const at = tools.findIndex((t) => t.name === name && t.status === "running");
    if (at >= 0) tools[at] = { ...tools[at], status };
    else tools.push({ name, status, detail });
  }
  const activity = status === "running"
    ? [displayToolName(name, "running"), detail].filter(Boolean).join(" · ")
    : undefined;
  return taskChunk(state, partId, taskId, {
    tools: tools.slice(-40),
    activity,
    toolCount: tools.length,
  });
}

/** Close the open tool part a finished call belongs to: by its call id when
 * the event carries one (parallel calls of one tool can finish out of order),
 * else the oldest open part for the key (FIFO). */
function closeTool(state: TranslatorState, key: string, callId?: unknown): string | undefined {
  const open = state.openToolIds.get(key);
  if (!open?.length) return undefined;
  const byCall = typeof callId === "string" ? state.partByCallId.get(callId) : undefined;
  const at = byCall ? open.indexOf(byCall) : -1;
  return at >= 0 ? open.splice(at, 1)[0] : open.shift();
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
  const textId = () => `${(state.textId ??= `txt-${state.assistantId ?? "run"}`)}-${state.textSeq}`;
  const reasoningId = () => `rsn-${state.assistantId ?? "run"}-${state.reasoningSeq}`;
  const openReasoning = () => {
    if (state.reasoningOpen) return;
    state.reasoningSeq += 1;
    out.push({ type: "reasoning-start", id: reasoningId() });
    state.reasoningOpen = true;
  };
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
        // Answer has begun — clear the status line.
        if (state.sawPhase) out.push({ type: "data-phase", id: "phase", data: null });
      }
      out.push({ type: "text-delta", id: textId(), delta: text });
      state.textStreamed = true;
      return out;
    }

    case "commentary": {
      if (p.task_id) return out;
      // Each marker ends one text segment: the first N text parts of the turn
      // are commentary, whether or not their tool part has arrived yet.
      const marker: UIMessageChunk = {
        type: "data-commentary",
        id: `cmt-${state.assistantId ?? "run"}-${state.textSeq}`,
        data: { segment: state.textSeq },
      };
      if (state.textStreamed) {
        // Live: the segment already streamed; a tool call follows it.
        closeText();
        out.push(marker);
        state.textStreamed = false;
        state.textSeq += 1;
        return out;
      }
      const text = ((p.text as string) ?? "").trim();
      if (!text) return out;
      ensureStart();
      closeReasoning();
      closeText();
      out.push({ type: "text-start", id: textId() });
      out.push({ type: "text-delta", id: textId(), delta: text });
      out.push({ type: "text-end", id: textId() });
      out.push(marker);
      state.textSeq += 1;
      return out;
    }

    case "reasoning_delta": {
      if (p.task_id) return out; // a sub-agent's thinking is not the author's
      const text = (p.text as string) ?? "";
      if (!text) return out;
      ensureStart();
      openReasoning();
      state.reasoningStreamed = true;
      out.push({ type: "reasoning-delta", id: reasoningId(), delta: text });
      return out;
    }

    case "reasoning": {
      if (p.task_id) return out;
      if (state.reasoningStreamed) {
        // Live: the segment already streamed; this only marks its end.
        state.reasoningStreamed = false;
        closeReasoning();
        return out;
      }
      const text = ((p.text as string) ?? "").trim();
      if (!text) return out;
      ensureStart();
      closeReasoning();
      openReasoning();
      out.push({ type: "reasoning-delta", id: reasoningId(), delta: text });
      closeReasoning();
      return out;
    }

    case "provider_turn_finished": {
      if (p.task_id) return out; // a specialist's turn, not the author's
      closeReasoning();
      return out;
    }

    // The author's model starts reading the turn: show the bubble and a live
    // status line right away instead of an empty message.
    case "author_started":
    case "provider_turn_started": {
      if (p.task_id) return out;
      ensureStart();
      if (!state.sawPhase) {
        state.sawPhase = true;
        out.push({ type: "data-phase", id: "phase", data: { phase: "thinking" } });
      }
      return out;
    }
    case "skill_started": {
      if (p.task_id) return out;
      ensureStart();
      state.sawPhase = true;
      out.push({ type: "data-phase", id: "phase", data: { phase: "gathering" } });
      return out;
    }
    // Long silent spans (a document write streaming its arguments, a hosted
    // search, a deck procedure authoring pages) used to show nothing for
    // p50 30 s before a write_docx call. These keep the status line moving.
    case "tool_progress": {
      if (p.task_id) {
        const partId = parentPart(state, p);
        const name = p.tool_name as string | undefined;
        return taskChunk(state, partId, p.task_id as string, {
          activity: name ? displayToolName(name, "running") : undefined,
        });
      }
      ensureStart();
      closeReasoning();
      state.sawPhase = true;
      const chars = p.chars_so_far as number | undefined;
      out.push({
        type: "data-phase",
        id: "phase",
        data: {
          phase: "drafting",
          detail: [p.tool_name as string | undefined, chars ? `${chars.toLocaleString()} chars` : ""]
            .filter(Boolean)
            .join(" · "),
        },
      });
      return out;
    }
    case "hosted_tool_progress": {
      if (p.task_id) {
        const partId = parentPart(state, p);
        const query = (p.query as string | undefined) ?? (p.url as string | undefined);
        return taskChunk(state, partId, p.task_id as string, {
          activity: [displayToolName("web_search", "running"), query].filter(Boolean).join(" · "),
        });
      }
      ensureStart();
      closeReasoning();
      state.sawPhase = true;
      out.push({
        type: "data-phase",
        id: "phase",
        data: { phase: "gathering", detail: (p.query as string | undefined) ?? (p.url as string | undefined) },
      });
      return out;
    }
    case "deck_started":
    case "procedure_started": {
      if (!p.task_id) return out;
      return taskChunk(state, parentPart(state, p), p.task_id as string, {
        title: p.title as string | undefined,
        skillName: (p.skill as string | undefined) ?? (p.task_id as string),
        total: (p.total as number | null | undefined) ?? undefined,
        status: "running",
      });
    }
    case "deck_progress": {
      ensureStart();
      state.sawPhase = true;
      const message = (p.message as string | undefined) ?? (p.stage as string | undefined);
      out.push({
        type: "data-phase",
        id: "phase",
        data: { phase: "drafting", detail: message ?? "ppt-master" },
      });
      if (p.task_id) {
        const elapsed = p.elapsed_s as number | undefined;
        out.push(
          ...taskChunk(state, parentPart(state, p), p.task_id as string, {
            activity: message,
            current: p.current as number | undefined,
            total: (p.total as number | null | undefined) ?? undefined,
            toolCount: p.calls as number | undefined,
            elapsedMs: elapsed !== undefined ? Math.round(elapsed * 1000) : undefined,
          }),
        );
      }
      return out;
    }
    case "citation_audit_started": {
      ensureStart();
      state.sawPhase = true;
      out.push({ type: "data-phase", id: "phase", data: { phase: "auditing" } });
      return out;
    }

    case "tool_call": {
      const name = p.name as string;
      if (!name) return out;
      if (p.task_id) return subToolChunks(state, p, name, "running", argDetail(p.arguments));
      ensureStart();
      closeReasoning();
      const key = toolKey(ev.agent ?? (p.agent as string), name);
      const id = openTool(state, key);
      noteCallId(state, key, p.call_id);
      out.push({
        type: "tool-input-available",
        toolCallId: id,
        toolName: name,
        input: p.arguments ?? {},
      });
      if (name === WORKFLOW_TOOL) {
        state.sawPhase = true;
        out.push({ type: "data-phase", id: "phase", data: { phase: "workflow" } });
      }
      return out;
    }

    case "local_tool_call_started": {
      // The provider's `tool_call` has already opened the part; a tool the
      // server runs itself opens it here.
      const name = p.tool_name as string;
      if (!name) return out;
      if (p.task_id) return subToolChunks(state, p, name, "running", argDetail(p.arguments));
      const key = toolKey(ev.agent, name);
      const open = state.openToolIds.get(key);
      if (open && open.length) {
        noteCallId(state, key, p.call_id); // already opened by tool_call
        return out;
      }
      ensureStart();
      closeReasoning();
      const id = openTool(state, key);
      noteCallId(state, key, p.call_id);
      out.push({
        type: "tool-input-available",
        toolCallId: id,
        toolName: name,
        input: p.arguments ?? {},
      });
      if (name === WORKFLOW_TOOL) {
        state.sawPhase = true;
        out.push({ type: "data-phase", id: "phase", data: { phase: "workflow" } });
      }
      return out;
    }

    case "local_tool_call_finished": {
      const name = p.tool_name as string;
      if (!name) return out;
      if (p.task_id) {
        return subToolChunks(state, p, name, looksLikeError(p.output_preview) ? "error" : "complete");
      }
      const id = closeTool(state, toolKey(ev.agent, name), p.call_id);
      if (!id) return out;
      const preview = p.output_preview;
      if (looksLikeError(preview)) {
        out.push({ type: "tool-output-error", toolCallId: id, errorText: preview as string });
      } else {
        out.push({ type: "tool-output-available", toolCallId: id, output: preview ?? "" });
      }
      return out;
    }

    case "local_tool_call_failed": {
      const name = p.tool_name as string;
      if (!name) return out;
      if (p.task_id) return subToolChunks(state, p, name, "error");
      const id = closeTool(state, toolKey(ev.agent, name), p.call_id);
      if (!id) return out;
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
      if (p.task_id) return subToolChunks(state, p, name, "complete", argDetail(p.arguments));
      ensureStart();
      closeReasoning();
      const id = `hosted:${dedupeKey}`;
      out.push({ type: "tool-input-available", toolCallId: id, toolName: name, input: p.arguments ?? {} });
      out.push({ type: "tool-output-available", toolCallId: id, output: "" });
      return out;
    }

    case "workflow_started": {
      const partId = parentPart(state, p);
      if (!partId) return out;
      const wf: WorkflowData = { partId, objective: p.objective as string | undefined };
      state.workflows.set(partId, wf);
      out.push({ type: "data-workflow", id: partId, data: { ...wf } });
      for (const t of (p.tasks as Array<Record<string, unknown>>) ?? []) {
        out.push(
          ...taskChunk(state, partId, t.id as string, {
            title: t.title as string | undefined,
            skillName: t.skill_name as string | undefined,
            status: "pending",
          }),
        );
      }
      return out;
    }

    case "workflow_finished": {
      const partId = parentPart(state, p);
      if (!partId) return out;
      const next: WorkflowData = {
        ...(state.workflows.get(partId) ?? { partId }),
        durationMs: p.duration_ms as number | undefined,
      };
      state.workflows.set(partId, next);
      out.push({ type: "data-workflow", id: partId, data: next });
      return out;
    }

    case "agent_task_started": {
      const id = p.task_id as string;
      if (!id) return out;
      return taskChunk(state, parentPart(state, p), id, {
        skillName: p.skill_name as string | undefined,
        title: (p.title ?? p.task) as string | undefined,
        detail: p.task as string | undefined,
        status: "running",
      });
    }

    case "agent_task_finished": {
      const id = p.task_id as string;
      if (!id) return out;
      const outcome = p.outcome as string | undefined;
      const good = !outcome || ["completed", "truncated", "reused"].includes(outcome);
      return taskChunk(state, parentPart(state, p), id, {
        skillName: p.skill_name as string | undefined,
        status: good ? "complete" : "error",
        outcome,
        toolCount: p.tool_count as number | undefined,
        totalTokens: p.total_tokens as number | undefined,
        durationMs: p.duration_ms as number | undefined,
        sources: mapSources(p.sources),
        activity: "",
      });
    }

    case "agent_task_failed": {
      const id = p.task_id as string;
      if (!id) return out;
      return taskChunk(state, parentPart(state, p), id, {
        title: p.title as string | undefined,
        status: "error",
        outcome: "failed",
        error: p.message as string | undefined,
        activity: "",
      });
    }

    case "agent_delta": {
      const id = p.task_id as string;
      if (!id) return out;
      const partId = parentPart(state, p);
      const prev = taskOf(state, partId, id);
      return taskChunk(state, partId, id, {
        skillName: p.skill_name as string | undefined,
        status: prev?.status === "complete" || prev?.status === "error" ? prev.status : "running",
        tail: p.tail as string | undefined,
        toolCount: (p.tool_count as number | undefined) || prev?.toolCount,
        elapsedMs: p.elapsed_ms as number | undefined,
        sources: mapSources(p.sources),
      });
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
          trigger: p.trigger,
          preTokens: p.pre_tokens,
        },
      });
      return out;
    }

    // --- Claude Code's own states (provider claude_code) ---------------------
    case "claude_code_session": {
      ensureStart();
      out.push({
        type: "data-session",
        id: "session",
        data: {
          sessionId: p.session_id,
          model: p.model,
          auth: p.auth,
          version: p.version,
          toolCount: p.tool_count,
          mcpServers: p.mcp_servers,
        },
      });
      return out;
    }
    case "todo_list": {
      ensureStart();
      closeReasoning();
      out.push({ type: "data-todos", id: "todos", data: { todos: Array.isArray(p.todos) ? p.todos : [] } });
      return out;
    }
    case "rate_limit": {
      out.push({
        type: "data-rateLimit",
        id: "rateLimit",
        data: {
          status: p.status,
          rateLimitType: p.rate_limit_type,
          utilization: p.utilization,
          resetsAt: p.resets_at,
        },
      });
      return out;
    }
    case "plan_usage": {
      const limits = Array.isArray(p.limits) ? (p.limits as Array<Record<string, unknown>>) : [];
      out.push({
        type: "data-planUsage",
        id: "planUsage",
        data: {
          subscriptionType: p.subscription_type,
          limits: limits.map((l) => ({
            kind: l.kind as string,
            group: l.group as string | null | undefined,
            percent: l.percent as number | null | undefined,
            resetsAt: l.resets_at as string | null | undefined,
            severity: l.severity as string | undefined,
            isActive: l.is_active as boolean | undefined,
          })),
        },
      });
      return out;
    }
    case "claude_code_status": {
      const status = (p.status as string | undefined) ?? "requesting";
      const was = state.ccStatus;
      state.ccStatus = status;
      if (status === "compacting" || status === "retrying") {
        ensureStart();
        state.sawPhase = true;
        const attempt = p.attempt as number | undefined;
        const max = p.max_retries as number | undefined;
        out.push({
          type: "data-phase",
          id: "phase",
          data: {
            phase: status,
            detail: status === "retrying" && attempt ? `${attempt}${max ? `/${max}` : ""}` : undefined,
          },
        });
      } else if (was === "compacting" || was === "retrying") {
        out.push({ type: "data-phase", id: "phase", data: { phase: "thinking" } });
      }
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

    // heartbeat, skill_finished, chat_updated and other structural log events
    // carry no visible chunk.
    default:
      return out;
  }
}

/**
 * Incremental SSE frame parser. Feed it chunks of the response body text; it
 * calls `onEvent(name, data)` for each complete `\n\n`-terminated frame and
 * returns the unconsumed remainder to prepend next call.
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

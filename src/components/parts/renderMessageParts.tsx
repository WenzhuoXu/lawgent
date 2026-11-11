/**
 * Renders an assistant message's ordered `parts`: the activity chain (the
 * author's thinking and the text it wrote before each tool call, with the
 * calls each step made under it and any sub-agent as a step of its own), then
 * the answer — the text after the commentary; the citation audit and
 * artifacts pin below the answer.
 */

import type { ReactNode } from "react";
import { Download, FileText } from "lucide-react";
import { MessageResponse as Response } from "@/components/ai-elements/message";
import { AuditCard } from "@/components/parts/AuditCard";
import {
  ActivityChain,
  thoughtSections,
  toolName,
  type ChainStep,
  type ToolPart,
} from "@/components/parts/ActivityChain";
import { ProcedureCard } from "@/components/parts/ProcedureCard";
import {
  CompactionNote,
  PlanUsageNotice,
  RateLimitNotice,
  SessionNotice,
  TodoList,
} from "@/components/parts/ClaudeCodeParts";
import { WorkflowCard } from "@/components/parts/WorkflowCard";
import { SourcesList, splitSources, parseSourcesTable } from "@/components/parts/SourcesList";
import { rewriteSandboxHref, artifactOrigin } from "@/lib/artifacts";
import { commentaryFlags, type MessagePart } from "@/lib/foldChunks";
import type {
  AuditData,
  CiteCheckData,
  CompactionData,
  PhaseData,
  PlanUsageData,
  RateLimitData,
  SessionData,
  SpecialistTask,
  TodosData,
  WorkflowData,
} from "@/lib/dataParts";

type DataPart = Extract<MessagePart, { type: `data-${string}` }>;

/** Tools whose work is a sub-agent: each is a step of the chain, not a row.
 * `Agent` (`Task` on older CLIs) is Claude Code's sub-agent. */
const PROCEDURE_TOOLS = new Set(["create_deck", "run_procedure", "Agent", "Task"]);

export function RenderMessageParts({
  parts,
  isStreaming,
}: {
  parts: MessagePart[];
  isStreaming: boolean;
}) {
  // Everything renders in stream order with STABLE keys so React never
  // remounts mid-stream. A sub-agent's rows render inside the tool part that
  // started it (matched by tool part id), not as parts of their own.
  const agentTasks = parts
    .filter((p) => p.type === "data-agentTask")
    .map((p) => (p as DataPart).data as SpecialistTask);
  const tasksByParent = new Map<string, SpecialistTask[]>();
  for (const t of agentTasks) {
    tasksByParent.set(t.parentCallId, [...(tasksByParent.get(t.parentCallId) ?? []), t]);
  }
  const workflows = new Map<string, WorkflowData>();
  for (const p of parts) {
    if (p.type === "data-workflow") {
      const wf = (p as DataPart).data as WorkflowData;
      workflows.set(wf.partId, wf);
    }
  }
  const auditPart = parts.find((p) => p.type === "data-audit") as DataPart | undefined;
  const citeCheckPart = parts.find((p) => p.type === "data-citeCheck") as DataPart | undefined;
  const artifactParts = parts.filter((p) => p.type === "data-artifact") as DataPart[];
  const phasePart = parts.find((p) => p.type === "data-phase") as DataPart | undefined;
  const phase = (phasePart?.data as PhaseData | null) ?? null;
  const usageData = (parts.find((p) => p.type === "data-usage") as DataPart | undefined)?.data as
    | { durationMs?: number }
    | undefined;
  const toolParts = parts.filter((p) => p.type.startsWith("tool-")) as ToolPart[];
  // Claude Code's own states (provider claude_code).
  const todos = (parts.find((p) => p.type === "data-todos") as DataPart | undefined)?.data as TodosData | undefined;
  const rateLimit = (parts.find((p) => p.type === "data-rateLimit") as DataPart | undefined)?.data as
    | RateLimitData
    | undefined;
  const planUsage = (parts.find((p) => p.type === "data-planUsage") as DataPart | undefined)?.data as
    | PlanUsageData
    | undefined;
  const session = (parts.find((p) => p.type === "data-session") as DataPart | undefined)?.data as
    | SessionData
    | undefined;
  const compactions = parts
    .filter((p) => p.type === "data-compaction")
    .map((p) => (p as DataPart).data as CompactionData);
  const commentary = commentaryFlags(parts);

  // The chain: each reasoning section opens a step, and the plain tool calls
  // that follow it hang under that step; a sub-agent call is a step itself.
  const steps: ChainStep[] = [];
  const body: ReactNode[] = [];
  const openStep = (): Extract<ChainStep, { kind: "thought" }> => {
    const last = steps.at(-1);
    if (last && last.kind === "thought") return last;
    const step: ChainStep = { key: `step-${steps.length}`, kind: "thought", body: "", tools: [] };
    steps.push(step);
    return step;
  };
  for (let i = 0; i < parts.length; i++) {
    const p = parts[i];
    if (p.type === "reasoning") {
      const text = (p as Extract<MessagePart, { type: "reasoning" }>).text;
      thoughtSections(text || "").forEach((sec, j) => {
        steps.push({ key: `rsn-${i}-${j}`, kind: "thought", title: sec.title, body: sec.body, tools: [] });
      });
    } else if (p.type.startsWith("tool-")) {
      const tp = p as ToolPart;
      const name = toolName(tp);
      const running = tp.state === "input-available";
      if (name === "run_workflow") {
        steps.push({
          key: tp.toolCallId,
          kind: "agent",
          running: running && isStreaming,
          node: (
            <WorkflowCard
              workflow={workflows.get(tp.toolCallId)}
              tasks={tasksByParent.get(tp.toolCallId) ?? []}
              running={running}
              streaming={isStreaming}
              errorText={tp.errorText}
            />
          ),
        });
      } else if (PROCEDURE_TOOLS.has(name)) {
        steps.push({
          key: tp.toolCallId,
          kind: "agent",
          running: running && isStreaming,
          node: <ProcedureCard part={tp} task={tasksByParent.get(tp.toolCallId)?.[0]} streaming={isStreaming} />,
        });
      } else {
        openStep().tools.push(tp);
      }
    } else if (p.type === "text") {
      const text = (p as Extract<MessagePart, { type: "text" }>).text;
      if (!text) continue;
      if (commentary[i]) {
        // Written before a tool call: commentary on the way to it, a step of
        // the chain with the calls that followed under it.
        steps.push({ key: `cmt-${i}`, kind: "thought", body: text, tools: [] });
        continue;
      }
      const { body: prose, sources } = splitSources(text);
      const sourceRows = sources ? parseSourcesTable(sources) : null;
      // Answers cite inline with full links, so the body renders as-is; the
      // sources section below it renders as a list with status dots.
      const proseText = rewriteLinks(sourceRows ? prose : text);
      body.push(
        <div key={`txt-${i}`}>
          <Response className="prose-cjk" animated isAnimating={isStreaming}>
            {proseText}
          </Response>
          {sourceRows ? <SourcesList markdown={sources!} /> : null}
        </div>,
      );
    }
    // citeCheck/audit/artifact handled below as pinned slots.
  }

  return (
    <div className="flex flex-col gap-2">
      {/* The live status line sits at the chain's foot, where the work is, so
          the bubble is never blank before the answer begins. */}
      <ActivityChain
        steps={steps}
        streaming={isStreaming}
        status={isStreaming && phase ? <StatusLine phase={phase} /> : undefined}
        durationMs={usageData?.durationMs}
        toolCount={toolParts.length}
        taskCount={agentTasks.length}
      />

      {session ? <SessionNotice data={session} /> : null}
      {todos?.todos?.length ? <TodoList todos={todos.todos} streaming={isStreaming} /> : null}
      {compactions.map((c, i) => (
        <CompactionNote key={`compaction-${i}`} data={c} />
      ))}

      {body}

      {/* Citation audit — pinned below the answer */}
      {(auditPart || citeCheckPart) && (
        <AuditCard
          audit={auditPart?.data as AuditData | undefined}
          citeCheck={citeCheckPart?.data as CiteCheckData | null | undefined}
        />
      )}

      {/* Generated artifacts */}
      {artifactParts.length > 0 ? (
        <div className="not-prose flex flex-col gap-1.5">
          {artifactParts.map((p) => {
            const a = p.data as { id: string; filename: string; url: string; origin?: string };
            return (
              <a
                key={a.id}
                href={rewriteSandboxHref(a.url)}
                download
                className="flex items-center gap-2 text-sm text-primary hover:underline"
              >
                <FileText className="size-4" />
                <span>{a.filename}</span>
                <span className="rounded-full border px-2 text-xs text-muted-foreground">
                  {artifactOrigin(a.origin)}
                </span>
                <Download className="size-3.5" />
              </a>
            );
          })}
        </div>
      ) : null}

      {rateLimit ? <RateLimitNotice data={rateLimit} /> : null}
      {planUsage ? <PlanUsageNotice data={planUsage} /> : null}
    </div>
  );
}

const PHASE_LABEL: Record<PhaseData["phase"], string> = {
  thinking: "思考中 / Thinking",
  gathering: "检索与研究 / Researching",
  workflow: "多智能体工作流 / Running workflow",
  drafting: "生成文件 / Producing the file",
  auditing: "核验引证 / Verifying citations",
  compacting: "压缩上下文 / Compacting context",
  retrying: "重试请求 / Retrying the request",
};

function StatusLine({ phase }: { phase: PhaseData }) {
  return (
    <div className="flex min-w-0 items-center gap-1.5 font-mono text-[12px] leading-5 text-content-secondary">
      <span className="shrink-0">{PHASE_LABEL[phase.phase] ?? phase.phase}…</span>
      {phase.detail ? <span className="truncate text-content-faint">· {phase.detail}</span> : null}
    </div>
  );
}

/** Rewrite sandbox:/raw artifact links inside markdown to /api/artifacts/… */
function rewriteLinks(text: string): string {
  return text.replace(/\]\((sandbox:[^)]+|[^)]*\/outputs\/chat_artifacts\/[^)]+)\)/g, (_m, href) => {
    return `](${rewriteSandboxHref(href)})`;
  });
}

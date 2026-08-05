/**
 * Renders an assistant message's ordered `parts` using Vercel AI Elements.
 * Three visual registers: reasoning (dimmed) · tool/action (cards) · answer
 * (markdown). Structural data-* parts render via Task/Plan, ChainOfThought,
 * Sources, and our bespoke AuditCard.
 */

import { useState, type ReactNode } from "react";
import {
  CheckCircle2,
  ChevronRight,
  Circle,
  Download,
  FileText,
  Loader2,
  type LucideIcon,
} from "lucide-react";
import { MessageResponse as Response } from "@/components/ai-elements/message";
import {
  Reasoning,
  ReasoningContent,
  ReasoningTrigger,
} from "@/components/ai-elements/reasoning";
import {
  Tool,
  ToolContent,
  ToolHeader,
  ToolInput,
  ToolOutput,
} from "@/components/ai-elements/tool";
import { Task, TaskContent, TaskItem, TaskTrigger } from "@/components/ai-elements/task";
import {
  ChainOfThought,
  ChainOfThoughtContent,
  ChainOfThoughtHeader,
  ChainOfThoughtSearchResult,
  ChainOfThoughtSearchResults,
  ChainOfThoughtStep,
} from "@/components/ai-elements/chain-of-thought";
import { AuditCard } from "@/components/parts/AuditCard";
import { SourcesList, splitSources, parseSourcesTable } from "@/components/parts/SourcesList";
import { buildInlineCitations } from "@/components/parts/InlineCite";
import { displayToolName } from "@/lib/toolLabels";
import { rewriteSandboxHref, artifactOrigin } from "@/lib/artifacts";
import { cn, formatDuration } from "@/lib/utils";
import type { MessagePart } from "@/lib/foldChunks";
import type { AuditData, CiteCheckData, PhaseData, PlanData, PlanTask } from "@/lib/dataParts";

type ToolPart = Extract<MessagePart, { type: `tool-${string}` }>;
type DataPart = Extract<MessagePart, { type: `data-${string}` }>;

function toolName(p: ToolPart): string {
  return p.type.replace(/^tool-/, "");
}

const STATUS_ICON: Record<PlanTask["status"], ReactNode> = {
  pending: <Circle className="size-3.5 text-muted-foreground/50" />,
  running: <Loader2 className="size-3.5 animate-spin text-primary" />,
  complete: <CheckCircle2 className="size-3.5 text-[var(--ok)]" />,
  error: <Circle className="size-3.5 text-[var(--dnf)]" />,
};

const COT_STATUS: Record<PlanTask["status"], "pending" | "active" | "complete"> = {
  pending: "pending",
  running: "active",
  complete: "complete",
  error: "complete",
};

// Per-status colored step dots (FIG1): running → spinner, complete → ok green,
// error → dnf red, pending → hollow. Overrides ChainOfThoughtStep's flat DotIcon.
// ChainOfThoughtStep's `icon` prop is typed as LucideIcon and rendered as
// `<Icon className="size-4" />`; these stable module-level components accept
// that className and add their own color, cast to LucideIcon at the type level.
type IconLike = (props: { className?: string }) => ReactNode;
const DotRunning: IconLike = ({ className }) => (
  <Loader2 className={cn(className, "animate-spin text-primary")} />
);
const DotComplete: IconLike = ({ className }) => (
  <CheckCircle2 className={cn(className, "text-[var(--ok)]")} />
);
const DotError: IconLike = ({ className }) => <Circle className={cn(className, "text-[var(--dnf)]")} />;
const DotPending: IconLike = ({ className }) => (
  <Circle className={cn(className, "text-muted-foreground/40")} />
);
function stepIcon(status: PlanTask["status"]): LucideIcon {
  const c =
    status === "running"
      ? DotRunning
      : status === "complete"
        ? DotComplete
        : status === "error"
          ? DotError
          : DotPending;
  return c as unknown as LucideIcon;
}

// F8: bilingual reasoning label — localized at the call site so the primitive
// stays generic. "思考中…" while streaming, "已思考 N 秒" once settled.
function thinkingMessage(isStreaming: boolean, duration?: number): ReactNode {
  if (isStreaming || duration === 0) return <span>思考中… / Thinking…</span>;
  if (duration === undefined) return <span>已思考片刻 / Thought for a moment</span>;
  return <span>已思考 {duration} 秒 / Thought for {duration}s</span>;
}

export function RenderMessageParts({
  parts,
  isStreaming,
}: {
  parts: MessagePart[];
  isStreaming: boolean;
}) {
  // Aggregate widgets (plan/agent-task checklist + audit) pin to fixed slots;
  // everything else renders INLINE in stream order with STABLE keys so React
  // never remounts mid-stream (that was the source of the flicker/choppiness).
  // Agent tasks come ONLY from real agent_task_started/finished events, so each
  // one has a genuine completion signal — no heuristic status coercion needed.
  const agentTasks = parts
    .filter((p) => p.type === "data-agentTask")
    .map((p) => (p as DataPart).data as PlanTask);
  const auditPart = parts.find((p) => p.type === "data-audit") as DataPart | undefined;
  const auditItems = (auditPart?.data as AuditData | undefined)?.items;
  const citeCheckPart = parts.find((p) => p.type === "data-citeCheck") as DataPart | undefined;
  const artifactParts = parts.filter((p) => p.type === "data-artifact") as DataPart[];
  const phasePart = parts.find((p) => p.type === "data-phase") as DataPart | undefined;
  const phase = (phasePart?.data as PhaseData | null) ?? null;
  const planData = (parts.find((p) => p.type === "data-plan") as DataPart | undefined)?.data as
    | PlanData
    | undefined;
  const usageData = (parts.find((p) => p.type === "data-usage") as DataPart | undefined)?.data as
    | { durationMs?: number }
    | undefined;
  const hasPlan = agentTasks.length > 0;
  const toolParts = parts.filter((p) => p.type.startsWith("tool-")) as ToolPart[];

  // Track whether we've already emitted the aggregated plan/timeline widget, so
  // it renders exactly once (at the position of the first plan/agentTask part).
  let planEmitted = false;

  // The activity trajectory (reasoning · plan/CoT · tool cards) renders inline
  // in stream order while streaming, then collapses behind a one-line run header
  // once complete (FIG2). Answer text / audit / artifacts always render below.
  const trajectory: ReactNode[] = [];
  const body: ReactNode[] = [];
  for (let i = 0; i < parts.length; i++) {
    const p = parts[i];
    if (p.type === "reasoning") {
      const text = (p as Extract<MessagePart, { type: "reasoning" }>).text;
      if (!text) continue;
      trajectory.push(
        <Reasoning key={`rsn-${i}`} isStreaming={isStreaming} className="w-full">
          <ReasoningTrigger getThinkingMessage={thinkingMessage} />
          <ReasoningContent>{text}</ReasoningContent>
        </Reasoning>,
      );
    } else if (p.type === "data-plan" || p.type === "data-agentTask") {
      if (planEmitted || !hasPlan) continue;
      planEmitted = true;
      trajectory.push(
        <div key="plan-timeline" className="flex flex-col gap-2">
          <PlanChecklist tasks={agentTasks} />
          <ChainOfThought defaultOpen>
            <ChainOfThoughtHeader>
              研究过程 / Chain of thought
              <CotSubtitle plan={planData} taskCount={agentTasks.length} />
            </ChainOfThoughtHeader>
            <ChainOfThoughtContent>
              {agentTasks.map((t) => (
                <ChainOfThoughtStep
                  key={t.id}
                  icon={stepIcon(t.status)}
                  status={COT_STATUS[t.status]}
                  label={t.title || t.skillName || t.id}
                  description={stepDescription(t, isStreaming)}
                >
                  {t.sources && t.sources.length ? (
                    <ChainOfThoughtSearchResults>
                      {t.sources.map((s, si) => (
                        <ChainOfThoughtSearchResult key={si}>{s.label}</ChainOfThoughtSearchResult>
                      ))}
                    </ChainOfThoughtSearchResults>
                  ) : null}
                </ChainOfThoughtStep>
              ))}
            </ChainOfThoughtContent>
          </ChainOfThought>
        </div>,
      );
    } else if (p.type.startsWith("tool-")) {
      const tp = p as ToolPart;
      trajectory.push(<ToolCard key={tp.toolCallId} part={tp} />);
    } else if (p.type === "text") {
      const text = (p as Extract<MessagePart, { type: "text" }>).text;
      if (!text) continue;
      const { body: prose, sources } = splitSources(text);
      const sourceRows = sources ? parseSourcesTable(sources) : null;
      // F3: turn [n] markers in the answer body into InlineCitation hover-cards
      // keyed to the parsed sources, colored by audit status. Only when we have
      // a numbered sources table AND the turn is settled (avoids rewriting
      // half-streamed markers). Falls back to plain prose otherwise.
      const cites =
        !isStreaming && sourceRows ? buildInlineCitations(sourceRows, auditItems) : null;
      const proseText = rewriteLinks(sourceRows ? prose : text);
      body.push(
        <div key={`txt-${i}`}>
          <Response
            className="prose-cjk"
            animated
            isAnimating={isStreaming}
            components={cites?.components}
          >
            {cites ? cites.rewrite(proseText) : proseText}
          </Response>
          {sourceRows ? <SourcesList markdown={sources!} /> : null}
        </div>,
      );
    }
    // citeCheck/audit/artifact handled below as pinned slots.
  }

  const hasTrajectory = trajectory.length > 0;
  const sourceCount = agentTasks.reduce((n, t) => n + (t.sources?.length ?? 0), 0);

  return (
    <div className="flex flex-col gap-2">
      {/* Live status line — shows what the model is doing during the planning
          phase, BEFORE any text/tool output, so the bubble is never blank. */}
      {isStreaming && phase ? <StatusLine phase={phase} /> : null}

      {hasTrajectory ? (
        isStreaming ? (
          <div className="flex flex-col gap-2">{trajectory}</div>
        ) : (
          <RunHeader
            durationMs={usageData?.durationMs}
            taskCount={agentTasks.length}
            toolCount={toolParts.length}
            sourceCount={sourceCount}
          >
            {trajectory}
          </RunHeader>
        )
      ) : null}

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
    </div>
  );
}

const PHASE_LABEL: Record<PhaseData["phase"], string> = {
  routing: "分析问题 / Routing",
  planning: "制定研究计划 / Planning",
  gathering: "检索与研究 / Researching",
  integrating: "撰写答复 / Writing",
  auditing: "核验引证 / Verifying citations",
};

function StatusLine({ phase }: { phase: PhaseData }) {
  return (
    <div className="flex items-center gap-2 rounded-lg bg-tool-surface px-3 py-2 font-mono text-xs text-content-secondary">
      <Loader2 className="size-3.5 shrink-0 animate-spin text-primary" />
      <span>{PHASE_LABEL[phase.phase] ?? phase.phase}…</span>
      {phase.detail ? (
        <span className="truncate text-content-faint">· {phase.detail}</span>
      ) : null}
    </div>
  );
}

const COMPLEXITY_LABEL: Record<string, string> = {
  simple: "简单",
  standard: "标准",
  complex: "复杂",
};

/** CoT header subtitle "· {complexity} · N 专家" (FIG1). */
function CotSubtitle({ plan, taskCount }: { plan?: PlanData; taskCount: number }) {
  const bits: string[] = [];
  if (plan?.complexity) bits.push(COMPLEXITY_LABEL[plan.complexity] ?? plan.complexity);
  if (taskCount > 0) bits.push(`${taskCount} 专家`);
  if (!bits.length) return null;
  return <span className="ml-1 font-normal text-content-faint">· {bits.join(" · ")}</span>;
}

/** Per-step meta line "{skill} · N 次工具 · {duration|运行中}" (FIG1 cot-desc). */
function stepDescription(t: PlanTask, isStreaming: boolean): ReactNode {
  if (t.status === "running") {
    return t.tail ?? [t.skillName, "运行中…"].filter(Boolean).join(" · ");
  }
  const bits: string[] = [];
  if (t.skillName) bits.push(t.skillName);
  if (t.toolCount) bits.push(`${t.toolCount} 次工具`);
  if (t.durationMs != null) bits.push(formatDuration(t.durationMs));
  else if (t.responseChars) bits.push(`${t.responseChars} chars`);
  const meta = bits.filter(Boolean).join(" · ");
  return meta || (isStreaming ? t.tail : undefined);
}

/** Collapsed one-line run summary (FIG2) that expands the full trajectory. */
function RunHeader({
  durationMs,
  taskCount,
  toolCount,
  sourceCount,
  children,
}: {
  durationMs?: number;
  taskCount: number;
  toolCount: number;
  sourceCount: number;
  children: ReactNode;
}) {
  const [open, setOpen] = useState(false);
  const bits: string[] = [];
  const dur = formatDuration(durationMs);
  if (dur) bits.push(`用时 ${dur}`);
  if (taskCount > 0) bits.push(`${taskCount} 项任务`);
  if (toolCount > 0) bits.push(`${toolCount} 次工具调用`);
  if (sourceCount > 0) bits.push(`${sourceCount} 个来源`);
  return (
    <div className="not-prose flex flex-col gap-2">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex items-center gap-2 rounded-xl border bg-tool-surface px-3 py-2 text-left text-[13px] text-content-secondary transition-colors hover:border-primary/40"
      >
        <span className="text-[var(--ok)]">✓</span>
        <span className="truncate">{bits.join(" · ") || "运行轨迹 / Trajectory"}</span>
        <span className="ml-auto flex items-center gap-1 text-content-faint">
          {open ? "收起轨迹" : "展开轨迹"}
          <ChevronRight
            className={cn("size-3.5 transition-transform", open ? "rotate-90" : "rotate-0")}
          />
        </span>
      </button>
      {open ? <div className="flex flex-col gap-2">{children}</div> : null}
    </div>
  );
}

function PlanChecklist({ tasks }: { tasks: PlanTask[] }) {
  const done = tasks.filter((t) => t.status === "complete").length;
  return (
    <Task defaultOpen>
      <TaskTrigger title={`研究计划 / Plan · ${done}/${tasks.length}`} />
      <TaskContent>
        {tasks.map((t) => (
          <TaskItem key={t.id}>
            <span className="mr-2 inline-flex align-middle">{STATUS_ICON[t.status]}</span>
            {t.title || t.skillName || t.id}
          </TaskItem>
        ))}
      </TaskContent>
    </Task>
  );
}

function ToolCard({ part }: { part: ToolPart }) {
  const name = toolName(part);
  return (
    <Tool>
      <ToolHeader
        type={`tool-${name}` as `tool-${string}`}
        state={part.state}
        title={displayToolName(name, part.state === "input-available" ? "running" : "complete")}
      />
      <ToolContent>
        {part.input != null ? <ToolInput input={part.input} /> : null}
        {part.output != null || part.errorText ? (
          <ToolOutput output={part.output as ReactNode} errorText={part.errorText} />
        ) : null}
      </ToolContent>
    </Tool>
  );
}

/** Rewrite sandbox:/raw artifact links inside markdown to /api/artifacts/… */
function rewriteLinks(text: string): string {
  return text.replace(/\]\((sandbox:[^)]+|[^)]*\/outputs\/chat_artifacts\/[^)]+)\)/g, (_m, href) => {
    return `](${rewriteSandboxHref(href)})`;
  });
}

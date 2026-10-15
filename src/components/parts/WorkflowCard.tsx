/**
 * One `run_workflow` call: the specialists the author started, each a row
 * that names its method and shows what it is doing now, collapsing to a
 * one-line receipt ("brief · 9 次工具 · 12k tokens · 45s") once done.
 *
 * Layout follows ZCode's sub-agent row (a single summary line per agent,
 * tied to the parent call, latest action shimmering while it runs) and the
 * chain-of-thought steps of docs/ui-proposal/ai-elements-build.html. Status
 * is never shown by colour alone: every state has its own icon and word.
 */

import { useState } from "react";
import {
  Bot,
  CheckCircle2,
  ChevronRight,
  Circle,
  Loader2,
  Network,
  XCircle,
} from "lucide-react";
import { Shimmer } from "@/components/ai-elements/shimmer";
import { displayToolName } from "@/lib/toolLabels";
import { cn, formatDuration } from "@/lib/utils";
import type { SpecialistTask, SubTool, WorkflowData } from "@/lib/dataParts";

const STATUS_WORD: Record<SpecialistTask["status"], string> = {
  pending: "等待中",
  running: "执行中",
  complete: "已完成",
  error: "执行失败",
};

function StatusIcon({ status, className }: { status: SpecialistTask["status"] | SubTool["status"]; className?: string }) {
  if (status === "running") return <Loader2 className={cn("size-3.5 animate-spin text-primary", className)} />;
  if (status === "complete") return <CheckCircle2 className={cn("size-3.5 text-[var(--ok)]", className)} />;
  if (status === "error") return <XCircle className={cn("size-3.5 text-[var(--dnf)]", className)} />;
  return <Circle className={cn("size-3.5 text-muted-foreground/50", className)} />;
}

function tokens(n?: number): string {
  if (!n) return "";
  return n >= 1000 ? `${(n / 1000).toFixed(n >= 10_000 ? 0 : 1)}k tokens` : `${n} tokens`;
}

function receipt(t: SpecialistTask): string {
  const bits: string[] = [];
  if (t.skillName) bits.push(t.skillName);
  if (t.toolCount) bits.push(`${t.toolCount} 次工具`);
  const tok = tokens(t.totalTokens);
  if (tok) bits.push(tok);
  const dur = formatDuration(t.durationMs ?? t.elapsedMs);
  if (dur) bits.push(dur);
  if (t.outcome && !["completed", "reused"].includes(t.outcome)) bits.push(t.outcome);
  if (t.outcome === "reused") bits.push("沿用上次结果");
  return bits.join(" · ");
}

function SpecialistRow({ task, index, streaming }: { task: SpecialistTask; index: number; streaming: boolean }) {
  const [open, setOpen] = useState(false);
  const running = task.status === "running";
  const activity = (task.activity || "").trim() || (running ? (task.tail || "").trim().split("\n").pop() : "");
  const canOpen = Boolean(task.tools?.length || task.sources?.length || task.detail || task.error);
  return (
    <div className="flex flex-col">
      <button
        type="button"
        onClick={() => canOpen && setOpen((v) => !v)}
        aria-expanded={open}
        className={cn(
          "group flex w-full items-start gap-2 rounded-md px-2 py-1.5 text-left text-[13px]",
          canOpen ? "hover:bg-muted/60" : "cursor-default",
        )}
      >
        <StatusIcon status={task.status} className="mt-0.5 shrink-0" />
        <div className="min-w-0 flex-1">
          <div className="flex min-w-0 items-center gap-1.5">
            <Bot className="size-3.5 shrink-0 text-content-faint" />
            <span className="shrink-0 text-content-faint">子智能体 {index + 1}</span>
            <span className="truncate font-medium text-foreground">· {task.title || task.skillName || task.id}</span>
            <span className="ml-auto shrink-0 pl-2 text-[12px] text-content-faint">
              {running || task.status === "pending" ? STATUS_WORD[task.status] : receipt(task) || STATUS_WORD[task.status]}
            </span>
            {canOpen ? (
              <ChevronRight
                className={cn(
                  "size-3.5 shrink-0 text-content-faint opacity-0 transition-transform group-hover:opacity-100",
                  open && "rotate-90 opacity-100",
                )}
              />
            ) : null}
          </div>
          {running && activity ? (
            <div className="mt-0.5 truncate font-mono text-[12px]">
              {streaming ? (
                <Shimmer as="span" className="text-[12px]">{activity.slice(0, 160)}</Shimmer>
              ) : (
                <span className="text-content-faint">{activity.slice(0, 160)}</span>
              )}
            </div>
          ) : null}
          {running && task.toolCount ? (
            <div className="mt-0.5 text-[12px] text-content-faint">
              {[task.skillName, `${task.toolCount} 次工具`, formatDuration(task.elapsedMs)].filter(Boolean).join(" · ")}
            </div>
          ) : null}
          {task.status === "error" && task.error ? (
            <div className="mt-0.5 truncate text-[12px] text-[var(--dnf)]">{task.error}</div>
          ) : null}
        </div>
      </button>
      {open ? (
        <div className="ml-[22px] flex flex-col gap-1 border-l pl-3 pb-1.5">
          {task.detail ? (
            <p className="whitespace-pre-wrap text-[12px] text-content-secondary">{task.detail}</p>
          ) : null}
          {(task.tools ?? []).map((tool, i) => (
            <div key={i} className="flex min-w-0 items-center gap-1.5 text-[12px] text-content-secondary">
              <StatusIcon status={tool.status} className="size-3 shrink-0" />
              <span className="shrink-0">{displayToolName(tool.name, tool.status === "running" ? "running" : "complete")}</span>
              {tool.detail ? <span className="truncate font-mono text-content-faint">· {tool.detail}</span> : null}
            </div>
          ))}
          {task.sources?.length ? (
            <div className="flex flex-wrap gap-1 pt-1">
              {task.sources.map((s, i) => (
                <span key={i} className="rounded-full border px-2 py-0.5 text-[11px] text-content-secondary">
                  {s.label}
                </span>
              ))}
            </div>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

export function WorkflowCard({
  workflow,
  tasks,
  running,
  streaming,
  errorText,
}: {
  workflow?: WorkflowData;
  tasks: SpecialistTask[];
  /** the tool call has not returned yet */
  running: boolean;
  streaming: boolean;
  errorText?: string;
}) {
  const [open, setOpen] = useState(true);
  const done = tasks.filter((t) => t.status === "complete").length;
  const failed = tasks.filter((t) => t.status === "error").length;
  const status = errorText ? "执行失败" : running ? "执行中" : "已完成";
  const bits = [`${tasks.length} 个子智能体`];
  if (tasks.length) bits.push(`${done}/${tasks.length}`);
  if (failed) bits.push(`${failed} 个失败`);
  const dur = formatDuration(workflow?.durationMs);
  if (dur && !running) bits.push(dur);
  return (
    <div className="not-prose w-full rounded-xl border bg-tool-surface">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className="flex w-full items-center gap-2 px-3 py-2 text-left text-[13px]"
      >
        <Network className="size-4 shrink-0 text-primary" />
        <span className="shrink-0 font-medium text-foreground">多智能体工作流</span>
        <span className="truncate text-content-faint">· {bits.join(" · ")}</span>
        <span className="ml-auto flex shrink-0 items-center gap-1.5 text-[12px] text-content-faint">
          {running ? <Loader2 className="size-3.5 animate-spin text-primary" /> : null}
          {status}
          <ChevronRight className={cn("size-3.5 transition-transform", open && "rotate-90")} />
        </span>
      </button>
      {open ? (
        <div className="flex flex-col gap-0.5 border-t px-1.5 py-1.5">
          {workflow?.objective ? (
            <p className="px-2 pb-1 text-[12px] text-content-secondary">{workflow.objective}</p>
          ) : null}
          {tasks.map((t, i) => (
            <SpecialistRow key={t.id} task={t} index={i} streaming={streaming} />
          ))}
          {!tasks.length && running ? (
            <div className="px-2 py-1.5 text-[12px] text-content-faint">正在分派子智能体…</div>
          ) : null}
          {errorText ? (
            <div className="px-2 py-1.5 text-[12px] text-[var(--dnf)]">{errorText.slice(0, 400)}</div>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

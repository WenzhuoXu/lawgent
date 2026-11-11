/**
 * What a turn served by Claude Code (provider `claude_code`) shows beyond the
 * shared chain: its plan (TodoWrite), its own context compaction, the
 * subscription's limits when one nears or hits its cap (`PlanLimitBars` is
 * also the usage panel's view of the plan), and a notice
 * when the session was not what it should be (billed to an API key, a legal
 * tool server that failed to connect).
 */

import { useState } from "react";
import { Archive, CheckCircle2, ChevronRight, Circle, CircleDot, Gauge, ListTodo, TriangleAlert } from "lucide-react";
import { Shimmer } from "@/components/ai-elements/shimmer";
import { cn } from "@/lib/utils";
import type { CompactionData, PlanUsageData, RateLimitData, SessionData, TodoItem } from "@/lib/dataParts";

function TodoIcon({ status }: { status: string }) {
  if (status === "completed") return <CheckCircle2 className="mt-0.5 size-3.5 shrink-0 text-[var(--ok)]" />;
  if (status === "in_progress") return <CircleDot className="mt-0.5 size-3.5 shrink-0 text-primary" />;
  return <Circle className="mt-0.5 size-3.5 shrink-0 text-content-faint" />;
}

/** The plan Claude Code keeps with TodoWrite: open while the turn runs, folded
 * to a one-line receipt once every step is done. */
export function TodoList({ todos, streaming }: { todos: TodoItem[]; streaming: boolean }) {
  const done = todos.filter((t) => t.status === "completed").length;
  const finished = done === todos.length;
  const [open, setOpen] = useState(false);
  if (!todos.length) return null;
  const expanded = open || (streaming && !finished);
  return (
    <div className="w-full rounded-xl border bg-tool-surface">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={expanded}
        className="flex w-full items-center gap-2 px-3 py-2 text-left text-[13px]"
      >
        <ListTodo className="size-4 shrink-0 text-primary" />
        <span className="font-medium text-foreground">计划 / Plan</span>
        <span className="text-[12px] text-content-faint">
          {done}/{todos.length}
        </span>
        <ChevronRight className={cn("ml-auto size-3.5 text-content-faint transition-transform", expanded && "rotate-90")} />
      </button>
      {expanded ? (
        <ol className="flex flex-col gap-1 border-t px-3 py-2">
          {todos.map((t, i) => {
            const active = t.status === "in_progress";
            const label = active && t.activeForm ? t.activeForm : t.content;
            return (
              <li key={i} className="flex min-w-0 items-start gap-1.5 text-[12.5px] leading-5">
                <TodoIcon status={t.status} />
                {active && streaming ? (
                  <Shimmer as="span" className="text-[12.5px]">{label}</Shimmer>
                ) : (
                  <span
                    className={cn(
                      "min-w-0",
                      t.status === "completed" ? "text-content-faint line-through" : "text-content-secondary",
                    )}
                  >
                    {label}
                  </span>
                )}
              </li>
            );
          })}
        </ol>
      ) : null}
    </div>
  );
}

function fmtTokens(n?: number): string | undefined {
  if (!n) return undefined;
  return n >= 1000 ? `${Math.round(n / 1000)}K tokens` : `${n} tokens`;
}

/** Claude Code summarised the conversation to keep going. */
export function CompactionNote({ data }: { data: CompactionData }) {
  if (data.reason !== "claude_code_auto_compact") return null;
  const size = fmtTokens(data.preTokens);
  return (
    <div className="flex items-center gap-1.5 text-[12px] text-content-faint">
      <Archive className="size-3.5 shrink-0" />
      <span>
        上下文已压缩 / Context compacted{size ? ` · ${size}` : ""}
        {data.trigger === "manual" ? " · manual" : ""}
      </span>
    </div>
  );
}

const WINDOW_LABEL: Record<string, string> = {
  five_hour: "5 小时窗口 / 5-hour window",
  seven_day: "7 天窗口 / 7-day window",
  seven_day_opus: "7 天 Opus 窗口 / 7-day Opus window",
  seven_day_sonnet: "7 天 Sonnet 窗口 / 7-day Sonnet window",
};

function resetTime(unix?: number | null): string | undefined {
  if (!unix) return undefined;
  const d = new Date(unix * 1000);
  const sameDay = d.toDateString() === new Date().toDateString();
  return sameDay
    ? d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
    : d.toLocaleString([], { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

/** The subscription's usage window — shown only when it nears or hits the limit. */
export function RateLimitNotice({ data }: { data: RateLimitData }) {
  if (!data || data.status === "allowed") return null;
  const rejected = data.status === "rejected";
  const window = WINDOW_LABEL[data.rateLimitType ?? ""] ?? data.rateLimitType ?? "";
  const pct = typeof data.utilization === "number" ? `${Math.round(data.utilization * 100)}%` : undefined;
  const resets = resetTime(data.resetsAt);
  return (
    <div
      className={cn(
        "flex items-center gap-2 rounded-lg border px-3 py-2 text-[12.5px]",
        rejected ? "border-[var(--dnf)] text-[var(--dnf)]" : "text-content-secondary",
      )}
    >
      <Gauge className="size-4 shrink-0" />
      <span>
        {rejected ? "Claude 订阅额度已用完 / Subscription limit reached" : "Claude 订阅额度接近上限 / Nearing the subscription limit"}
        {window ? ` · ${window}` : ""}
        {pct ? ` · ${pct}` : ""}
        {resets ? ` · ${resets} 重置 / resets` : ""}
      </span>
    </div>
  );
}

/** Says so when the session was not what it should be; silent otherwise. */
export function SessionNotice({ data }: { data: SessionData }) {
  const problems: string[] = [];
  if (data.auth && data.auth !== "subscription") {
    problems.push(`本轮按 API 计费（${data.auth}），未使用订阅 / Billed to an API key, not the subscription`);
  }
  for (const s of data.mcpServers ?? []) {
    if (s.status !== "connected") problems.push(`工具服务 ${s.name} 未连接（${s.status}）/ Tool server ${s.name}: ${s.status}`);
  }
  if (!problems.length) return null;
  return (
    <div className="flex flex-col gap-1 rounded-lg border border-[var(--dnf)] px-3 py-2 text-[12.5px] text-[var(--dnf)]">
      {problems.map((p) => (
        <div key={p} className="flex items-center gap-2">
          <TriangleAlert className="size-4 shrink-0" />
          <span>{p}</span>
        </div>
      ))}
    </div>
  );
}

/** A plan limit window, whatever the CLI calls it. */
const LIMIT_LABEL: Record<string, string> = {
  session: "5 小时会话 / 5-hour session",
  five_hour: "5 小时会话 / 5-hour session",
  weekly: "每周 / Weekly",
  seven_day: "每周 / Weekly",
  weekly_opus: "每周 Opus / Weekly Opus",
  seven_day_opus: "每周 Opus / Weekly Opus",
  weekly_sonnet: "每周 Sonnet / Weekly Sonnet",
  seven_day_sonnet: "每周 Sonnet / Weekly Sonnet",
};

export type PlanLimitRow = PlanUsageData["limits"][number];

function limitLabel(l: PlanLimitRow): string {
  return LIMIT_LABEL[l.kind] ?? LIMIT_LABEL[l.group ?? ""] ?? l.kind.replaceAll("_", " ");
}

function resetsLabel(iso?: string | null): string | undefined {
  if (!iso) return undefined;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return undefined;
  const sameDay = d.toDateString() === new Date().toDateString();
  return sameDay
    ? d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
    : d.toLocaleString([], { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

function needsAttention(l: PlanLimitRow): boolean {
  return (l.severity && l.severity !== "normal") || (typeof l.percent === "number" && l.percent >= 90) || false;
}

/** The plan's limit windows as bars: percent used and when each resets. */
export function PlanLimitBars({ limits }: { limits: PlanLimitRow[] }) {
  const shown = limits.filter((l) => l.isActive !== false);
  if (!shown.length) return <div className="text-[12px] text-content-faint">暂无额度数据 / No limit data</div>;
  return (
    <div className="flex flex-col gap-2">
      {shown.map((l, i) => {
        const pct = typeof l.percent === "number" ? Math.max(0, Math.min(100, Math.round(l.percent))) : undefined;
        const hot = needsAttention(l);
        const resets = resetsLabel(l.resetsAt);
        return (
          <div key={`${l.kind}-${i}`} className="flex flex-col gap-1">
            <div className="flex items-baseline gap-2 text-[12px]">
              <span className="text-foreground">{limitLabel(l)}</span>
              <span className={cn("tabular-nums", hot ? "text-[var(--dnf)]" : "text-content-secondary")}>
                {pct !== undefined ? `${pct}%` : "—"}
              </span>
              {resets ? <span className="ml-auto text-[11px] text-content-faint">{resets} 重置 / resets</span> : null}
            </div>
            <div className="h-1.5 w-full overflow-hidden rounded-full bg-muted">
              <div
                className={cn("h-full rounded-full transition-all", hot ? "bg-[var(--dnf)]" : "bg-primary")}
                style={{ width: `${pct ?? 0}%` }}
              />
            </div>
          </div>
        );
      })}
    </div>
  );
}

/** In the chat, the plan's limits only when one needs attention. */
export function PlanUsageNotice({ data }: { data: PlanUsageData }) {
  const hot = (data.limits ?? []).filter((l) => l.isActive !== false && needsAttention(l));
  if (!hot.length) return null;
  return (
    <div className="flex flex-col gap-2 rounded-lg border px-3 py-2">
      <div className="flex items-center gap-2 text-[12.5px] text-content-secondary">
        <Gauge className="size-4 shrink-0" />
        <span>Claude 订阅额度 / Subscription limits</span>
      </div>
      <PlanLimitBars limits={hot} />
    </div>
  );
}

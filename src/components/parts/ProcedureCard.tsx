/**
 * A `create_deck` or `run_procedure` call: the procedure's sub-agent at work
 * inside one card. It reads "running" until the tool itself returns (the
 * export found, opened and linted), never earlier, and shows pages authored
 * against the target, the latest step, and every call the sub-agent made.
 */

import { useState } from "react";
import { ChevronRight, FileCog, Presentation } from "lucide-react";
import { Shimmer } from "@/components/ai-elements/shimmer";
import { StateIcon, toolName, type ToolPart } from "@/components/parts/ActivityChain";
import { displayToolName } from "@/lib/toolLabels";
import { cn, formatDuration } from "@/lib/utils";
import type { SpecialistTask } from "@/lib/dataParts";

function outputField(output: unknown, key: string): string | undefined {
  if (typeof output !== "string") return undefined;
  const m = output.match(new RegExp(`"${key}"\\s*:\\s*("([^"]*)"|\\d+)`));
  if (!m) return undefined;
  return m[2] ?? m[1];
}

export function ProcedureCard({
  part,
  task,
  streaming,
}: {
  part: ToolPart;
  task?: SpecialistTask;
  streaming: boolean;
}) {
  const [open, setOpen] = useState(false);
  const name = toolName(part);
  const deck = name === "create_deck";
  const input = (part.input ?? {}) as { title?: string; skill?: string };
  const running = part.state === "input-available";
  const status = outputField(part.output, "status");
  const failed = part.state === "output-error" || (status !== undefined && status !== "ok");
  const state = running ? "running" : failed ? "error" : "complete";
  const label = deck ? "制作演示文稿" : `文书流程 · ${input.skill ?? task?.skillName ?? ""}`;
  const title = input.title || task?.title;

  const bits: string[] = [];
  if (task?.current) bits.push(task.total ? `第 ${task.current}/${task.total} 页` : `已写 ${task.current} 页`);
  else if (running && task?.total) bits.push(`0/${task.total} 页`);
  const slides = outputField(part.output, "slides");
  if (!running && slides) bits.push(`${slides} 页`);
  if (task?.toolCount) bits.push(`${task.toolCount} 次工具`);
  const dur = formatDuration(task?.elapsedMs);
  if (dur) bits.push(dur);
  if (!running && status && status !== "ok") bits.push(status);

  const pct = task?.total && task.current ? Math.min(100, Math.round((task.current / task.total) * 100)) : 0;
  const tools = task?.tools ?? [];
  const activity = running ? (task?.activity || "").trim() : "";

  return (
    <div className="w-full rounded-xl border bg-tool-surface">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className="flex w-full items-center gap-2 px-3 py-2 text-left text-[13px]"
      >
        {deck ? (
          <Presentation className="size-4 shrink-0 text-primary" />
        ) : (
          <FileCog className="size-4 shrink-0 text-primary" />
        )}
        <span className="shrink-0 font-medium text-foreground">{label}</span>
        {title ? <span className="min-w-0 truncate text-content-faint">· {title}</span> : null}
        <span className="ml-auto flex shrink-0 items-center gap-1.5 text-[12px] text-content-faint">
          <StateIcon state={state} className="size-3.5" />
          {running ? "执行中" : failed ? "失败" : "已完成"}
          <ChevronRight className={cn("size-3.5 transition-transform", open && "rotate-90")} />
        </span>
      </button>
      {bits.length || activity || pct ? (
        <div className="flex flex-col gap-1 px-3 pb-2">
          {pct ? (
            <div className="h-1 w-full overflow-hidden rounded-full bg-muted">
              <div className="h-full rounded-full bg-primary transition-all" style={{ width: `${pct}%` }} />
            </div>
          ) : null}
          {bits.length ? <div className="text-[12px] text-content-faint">{bits.join(" · ")}</div> : null}
          {activity ? (
            <div className="truncate font-mono text-[12px]">
              {streaming ? (
                <Shimmer as="span" className="text-[12px]">{activity.slice(0, 160)}</Shimmer>
              ) : (
                <span className="text-content-faint">{activity.slice(0, 160)}</span>
              )}
            </div>
          ) : null}
        </div>
      ) : null}
      {open ? (
        <div className="flex flex-col gap-0.5 border-t px-3 py-1.5">
          {tools.length ? (
            tools.map((t, i) => (
              <div key={i} className="flex min-w-0 items-center gap-1.5 text-[12px] text-content-secondary">
                <StateIcon state={t.status} />
                <span className="shrink-0">{displayToolName(t.name, t.status === "running" ? "running" : "complete")}</span>
                {t.detail ? <span className="min-w-0 truncate font-mono text-content-faint">· {t.detail}</span> : null}
              </div>
            ))
          ) : (
            <div className="text-[12px] text-content-faint">{running ? "子智能体启动中…" : "没有记录到子智能体的调用"}</div>
          )}
          {part.errorText ? (
            <div className="pt-1 text-[12px] text-[var(--dnf)]">{part.errorText.slice(0, 400)}</div>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

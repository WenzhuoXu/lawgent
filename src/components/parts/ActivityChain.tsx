/**
 * A turn's activity as one chain of thought: each reasoning step is a node of
 * the chain, the tool calls it led to hang under it as one-line rows, and a
 * call that runs a sub-agent (a workflow, a deck, a procedure) is a node of
 * its own with its progress inside. Thinking is the spine; tools are detail.
 *
 * While the turn runs the chain is open and its newest step shimmers; once it
 * is done the chain folds behind a one-line receipt.
 */

import { useState, type ReactNode } from "react";
import {
  CheckCircle2,
  ChevronRight,
  FileText,
  Globe,
  Loader2,
  Scale,
  Search,
  Wrench,
  XCircle,
} from "lucide-react";
import { Shimmer } from "@/components/ai-elements/shimmer";
import { ToolInput, ToolOutput } from "@/components/ai-elements/tool";
import { displayToolName, isSourceRetrievalTool } from "@/lib/toolLabels";
import { cn, formatDuration } from "@/lib/utils";
import type { MessagePart } from "@/lib/foldChunks";

export type ToolPart = Extract<MessagePart, { type: `tool-${string}` }>;

export type ChainStep =
  | { key: string; kind: "thought"; title?: string; body: string; tools: ToolPart[] }
  | { key: string; kind: "agent"; node: ReactNode; running: boolean };

export function toolName(p: ToolPart): string {
  return p.type.replace(/^tool-/, "");
}

const HEADING = /^\s*\*\*(.+?)\*\*\s*$/;

/** A reasoning segment's sections. OpenAI summaries open each with a bold
 * heading line; a segment without one is a single untitled section. */
export function thoughtSections(text: string): { title?: string; body: string }[] {
  const out: { title?: string; body: string }[] = [];
  let title: string | undefined;
  let body: string[] = [];
  const flush = () => {
    const joined = body.join("\n").trim();
    if (title || joined) out.push({ title, body: joined });
  };
  for (const line of text.split("\n")) {
    const m = line.match(HEADING);
    if (m) {
      flush();
      title = m[1].trim();
      body = [];
    } else {
      body.push(line);
    }
  }
  flush();
  return out;
}

/** A short argument summary for a tool row. */
function argDetail(input: unknown): string | undefined {
  if (!input || typeof input !== "object") return undefined;
  const a = input as Record<string, unknown>;
  for (const key of ["query", "q", "keyword", "title", "name", "url", "path", "section", "article", "source"]) {
    const v = a[key];
    if (typeof v === "string" && v.trim()) {
      const s = v.trim();
      return key === "path" ? s.split("/").pop() : s.slice(0, 120);
    }
  }
  return undefined;
}

function ToolGlyph({ name }: { name: string }) {
  const cls = "size-3 shrink-0 text-content-faint";
  if (name.startsWith("web_") || name === "web_search_call") return <Globe className={cls} />;
  if (name.startsWith("tool_search")) return <Search className={cls} />;
  if (isSourceRetrievalTool(name)) return <Scale className={cls} />;
  if (name.startsWith("read_") || name.startsWith("load_") || name.startsWith("inspect_")) {
    return <FileText className={cls} />;
  }
  return <Wrench className={cls} />;
}

export function StateIcon({ state, className }: { state: "running" | "complete" | "error"; className?: string }) {
  if (state === "running") return <Loader2 className={cn("size-3 shrink-0 animate-spin text-primary", className)} />;
  if (state === "error") return <XCircle className={cn("size-3 shrink-0 text-[var(--dnf)]", className)} />;
  return <CheckCircle2 className={cn("size-3 shrink-0 text-[var(--ok)]", className)} />;
}

function partState(p: ToolPart): "running" | "complete" | "error" {
  if (p.state === "input-available") return "running";
  if (p.state === "output-error") return "error";
  return "complete";
}

function hasKeys(v: unknown): boolean {
  return Boolean(v && typeof v === "object" && Object.keys(v as object).length);
}

function ToolRow({ part }: { part: ToolPart }) {
  const [open, setOpen] = useState(false);
  const name = toolName(part);
  const state = partState(part);
  const detail = argDetail(part.input);
  const canOpen = hasKeys(part.input) || Boolean(part.output) || Boolean(part.errorText);
  return (
    <div className="flex flex-col">
      <button
        type="button"
        onClick={() => canOpen && setOpen((v) => !v)}
        aria-expanded={open}
        className={cn(
          "group flex min-w-0 items-center gap-1.5 rounded px-1 py-0.5 text-left text-[12px] text-content-secondary",
          canOpen ? "hover:bg-muted/60" : "cursor-default",
        )}
      >
        <StateIcon state={state} />
        <ToolGlyph name={name} />
        <span className="shrink-0">{displayToolName(name, state === "running" ? "running" : "complete")}</span>
        {detail ? <span className="min-w-0 truncate font-mono text-content-faint">· {detail}</span> : null}
        {canOpen ? (
          <ChevronRight
            className={cn(
              "ml-auto size-3 shrink-0 text-content-faint opacity-0 transition-transform group-hover:opacity-100",
              open && "rotate-90 opacity-100",
            )}
          />
        ) : null}
      </button>
      {open ? (
        <div className="ml-5 mt-1 mb-1.5 space-y-2 text-[12px]">
          {hasKeys(part.input) ? <ToolInput input={part.input} /> : null}
          {part.output || part.errorText ? (
            <ToolOutput output={part.output as ReactNode} errorText={part.errorText} />
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

function ThoughtBody({ text, active }: { text: string; active: boolean }) {
  const [expanded, setExpanded] = useState(false);
  const plain = text.replace(/\*\*(.+?)\*\*/g, "$1");
  const long = plain.length > 220;
  const clamp = !active && !expanded && long;
  return (
    <p
      onClick={() => long && !active && setExpanded((v) => !v)}
      className={cn(
        "whitespace-pre-wrap text-[12.5px] leading-relaxed text-content-secondary",
        clamp && "line-clamp-3 cursor-pointer",
        long && !active && "cursor-pointer",
      )}
    >
      {plain}
    </p>
  );
}

function Node({ last, dot, children }: { last: boolean; dot: ReactNode; children: ReactNode }) {
  return (
    <li className={cn("relative flex gap-2.5", !last && "pb-3")}>
      <div className="flex w-3.5 shrink-0 flex-col items-center">
        <div className="flex h-5 items-center">{dot}</div>
        {!last ? <span className="w-px flex-1 bg-border" /> : null}
      </div>
      <div className="min-w-0 flex-1">{children}</div>
    </li>
  );
}

function Dot({ active }: { active: boolean }) {
  return (
    <span
      className={cn(
        "size-2 rounded-full",
        active ? "animate-pulse bg-primary" : "bg-content-faint/60",
      )}
    />
  );
}

export function ActivityChain({
  steps,
  streaming,
  status,
  durationMs,
  toolCount,
  taskCount,
}: {
  steps: ChainStep[];
  streaming: boolean;
  /** the live status line, shown at the foot of the chain while it runs */
  status?: ReactNode;
  durationMs?: number;
  toolCount: number;
  taskCount: number;
}) {
  const [open, setOpen] = useState<boolean | null>(null);
  const expanded = open ?? streaming;
  const bits: string[] = [];
  const dur = formatDuration(durationMs);
  bits.push(streaming ? "思考中 / Thinking" : dur ? `已思考 ${dur}` : "已思考 / Thought");
  if (toolCount > 0) bits.push(`${toolCount} 次工具调用`);
  if (taskCount > 0) bits.push(`${taskCount} 个子智能体`);
  if (!steps.length && !status) return null;

  return (
    <div className="not-prose flex flex-col gap-2">
      <button
        type="button"
        onClick={() => setOpen(!expanded)}
        aria-expanded={expanded}
        className="flex items-center gap-2 text-left text-[13px] text-content-secondary"
      >
        {streaming ? (
          <Loader2 className="size-3.5 shrink-0 animate-spin text-primary" />
        ) : (
          <span className="text-[var(--ok)]">✓</span>
        )}
        <span className="truncate">
          {streaming ? <Shimmer as="span" className="text-[13px]">{bits.join(" · ")}</Shimmer> : bits.join(" · ")}
        </span>
        <ChevronRight
          className={cn("size-3.5 shrink-0 text-content-faint transition-transform", expanded && "rotate-90")}
        />
      </button>
      {expanded ? (
        <ol className="ml-0.5 flex flex-col">
          {steps.map((step, i) => {
            const last = i === steps.length - 1 && !(streaming && status);
            const active = streaming && i === steps.length - 1;
            if (step.kind === "agent") {
              return (
                <Node key={step.key} last={last} dot={<Dot active={step.running} />}>
                  {step.node}
                </Node>
              );
            }
            return (
              <Node key={step.key} last={last} dot={<Dot active={active} />}>
                {step.title ? (
                  <div className="text-[13px] font-medium leading-5 text-foreground">
                    {active ? <Shimmer as="span" className="text-[13px]">{step.title}</Shimmer> : step.title}
                  </div>
                ) : null}
                {step.body ? <ThoughtBody text={step.body} active={active} /> : null}
                {step.tools.length ? (
                  <div className={cn("flex flex-col gap-0.5", (step.title || step.body) && "mt-1")}>
                    {step.tools.map((t) => (
                      <ToolRow key={t.toolCallId} part={t} />
                    ))}
                  </div>
                ) : null}
              </Node>
            );
          })}
          {streaming && status ? (
            <Node last dot={<Loader2 className="size-3 animate-spin text-primary" />}>
              {status}
            </Node>
          ) : null}
        </ol>
      ) : null}
    </div>
  );
}

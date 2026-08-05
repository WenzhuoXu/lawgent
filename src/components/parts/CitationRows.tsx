/**
 * Shared per-citation row (gold FIG4 `.cit-row`): status glyph · serif cite ·
 * mono meta (source · validity · round-trip) · action link. Rendered both in
 * the AuditCard review/DNF bands (F2) and the tabbed Inspector 来源 tab (F4),
 * so the two verification surfaces stay pixel-consistent.
 */

import type { ReactNode } from "react";
import { cn } from "@/lib/utils";
import type { AuditItem } from "@/lib/dataParts";
import type { SourceRow } from "@/components/parts/SourcesList";
import { rewriteSandboxHref } from "@/lib/artifacts";

export type RowTone = "ok" | "review" | "bad";

const GLYPH: Record<RowTone, string> = { ok: "☑", review: "⚠", bad: "✕" };
const GLYPH_COLOR: Record<RowTone, string> = {
  ok: "text-[var(--ok)]",
  review: "text-[var(--review)]",
  bad: "text-[var(--dnf)]",
};
const BORDER: Record<RowTone, string> = {
  ok: "border-border",
  review: "border-review",
  bad: "border-dnf",
};

/** Map a cite-check item's severity/issue-type to a row tone + action label. */
export function itemTone(item: AuditItem): RowTone {
  const sev = (item.severity ?? "").toLowerCase();
  const issue = (item.issueType ?? "").toLowerCase();
  if (sev === "critical" || /miscit|misground|not[_\s-]?found|unsourc|无法|未能溯源|不符|错误/.test(issue))
    return "bad";
  if (/confirm|verified|一致|现行有效/.test(issue)) return "ok";
  return "review"; // nuanced / model_only / could-not-check
}

/** Action link text per tone (gold: 打开原文 / 查看差异 / 重新检索). */
function actionLabel(tone: RowTone): string {
  if (tone === "bad") return "重新检索 / Re-search";
  if (tone === "review") return "查看差异 / View diff";
  return "打开原文 ↗";
}

/** Compact mono meta line: jurisdiction · issue-type · what the source says. */
function itemMeta(item: AuditItem): string {
  return [item.jurisdiction, item.issueType, item.sourceSays]
    .map((s) => (s ?? "").trim())
    .filter(Boolean)
    .join(" · ");
}

export function CitationRow({
  tone,
  cite,
  meta,
  action,
  actionHref,
  onAction,
}: {
  tone: RowTone;
  cite: string;
  meta?: string;
  action?: string;
  actionHref?: string;
  onAction?: () => void;
}) {
  const actionNode: ReactNode = action ? (
    actionHref ? (
      <a
        href={rewriteSandboxHref(actionHref)}
        target="_blank"
        rel="noreferrer"
        className="ml-auto shrink-0 self-center whitespace-nowrap text-[11.5px] text-primary hover:underline"
      >
        {action}
      </a>
    ) : (
      <button
        type="button"
        onClick={onAction}
        className="ml-auto shrink-0 cursor-pointer self-center whitespace-nowrap text-[11.5px] text-primary hover:underline"
      >
        {action}
      </button>
    )
  ) : null;

  return (
    <div className={cn("flex gap-2.5 rounded-[10px] border bg-card px-2.5 py-2", BORDER[tone])}>
      <span className={cn("mt-0.5 shrink-0", GLYPH_COLOR[tone])}>{GLYPH[tone]}</span>
      <div className="min-w-0 flex-1">
        <div className="font-serif text-[13.5px] leading-snug text-foreground">{cite}</div>
        {meta ? <div className="mt-0.5 font-mono text-[11.5px] text-content-faint">{meta}</div> : null}
      </div>
      {actionNode}
    </div>
  );
}

/** Render the flagged cite-check items[] as rows. */
export function AuditItemRows({
  items,
  onAction,
}: {
  items: AuditItem[];
  onAction?: (item: AuditItem) => void;
}) {
  return (
    <div className="flex flex-col gap-1.5">
      {items.map((it, i) => {
        const tone = itemTone(it);
        return (
          <CitationRow
            key={i}
            tone={tone}
            cite={it.claimed || it.location || "引证 / Citation"}
            meta={itemMeta(it)}
            action={actionLabel(tone)}
            onAction={onAction ? () => onAction(it) : undefined}
          />
        );
      })}
    </div>
  );
}

/** SourceRow (parsed from the prose 资料来源 table) tone → row tone. */
function sourceTone(status: SourceRow["status"]): RowTone {
  if (status === "bad") return "bad";
  if (status === "warn") return "review";
  return "ok";
}

/** Render parsed prose-source rows (the full citation list, with pinpoint link). */
export function SourceRows({ rows }: { rows: SourceRow[] }) {
  return (
    <div className="flex flex-col gap-1.5">
      {rows.map((r, i) => {
        const tone = sourceTone(r.status);
        const cite = `${r.index ? `[${r.index}] ` : ""}${r.claim}`.trim() || r.pinpoint;
        const meta = [r.pinpoint, r.note].map((s) => (s ?? "").trim()).filter(Boolean).join(" · ");
        return (
          <CitationRow
            key={i}
            tone={tone}
            cite={cite}
            meta={meta}
            action={r.pinpointHref ? "打开原文 ↗" : undefined}
            actionHref={r.pinpointHref}
          />
        );
      })}
    </div>
  );
}

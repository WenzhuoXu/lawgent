/**
 * AuditCard — the citation-audit verdict UI. The one bespoke component (no OSS
 * equivalent): renders the staged progress bar while auditing and the
 * pass / needs-review / DO-NOT-FILE verdict bands afterward. Faithful port of
 * CitationAuditPanel (main.jsx:1349-1439), restyled onto the paper/ink tokens.
 */

import { useMemo, useRef } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { cn } from "@/lib/utils";
import { AuditItemRows } from "@/components/parts/CitationRows";
import type { AuditData, CiteCheckData } from "@/lib/dataParts";

const STAGE_LABELS: Record<string, string> = {
  starting: "启动核验 / Starting",
  reading_draft: "读取草稿 / Reading draft",
  extracting: "提取引证 / Extracting citations",
  validating: "校验引证结构 / Validating",
  fetching: "抓取原始文献 / Fetching sources",
  roundtripping: "原文回环比对 / Round-tripping",
  auditing: "审计溯源标签 / Auditing",
  composing: "生成核验报告 / Composing report",
  logging: "登记已核验引证 / Logging",
};

export function AuditCard({
  audit,
  citeCheck,
}: {
  audit?: AuditData;
  citeCheck?: CiteCheckData | null;
}) {
  // In-progress: no verdict yet, progress present.
  if (citeCheck && !audit) {
    const pct =
      citeCheck.total && citeCheck.current
        ? Math.round((citeCheck.current / citeCheck.total) * 100)
        : 8;
    return (
      <div className="not-prose my-3 rounded-xl border-l-[3px] border-l-primary bg-tool-surface p-3">
        <div className="flex items-center gap-2 font-medium text-sm">
          <span>⚖ 引证核验中 / Citation audit</span>
          <span className="ml-auto font-mono text-xs text-muted-foreground">
            {STAGE_LABELS[citeCheck.stage ?? ""] ?? citeCheck.stage}
            {citeCheck.total ? ` · ${citeCheck.current ?? 0}/${citeCheck.total}` : ""}
          </span>
        </div>
        <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-accent">
          <span className="block h-full rounded-full bg-primary transition-all" style={{ width: `${pct}%` }} />
        </div>
      </div>
    );
  }

  if (!audit) return null;

  if (audit.skipped) {
    return (
      <div className="not-prose my-3 text-xs text-muted-foreground">
        — 未核验（{audit.skipReason ?? "一般性咨询"}）/ Audit skipped
      </div>
    );
  }

  const tone = audit.doNotFile
    ? "dnf"
    : audit.ok
      ? "pass"
      : "review";

  // Flagged per-citation rows (B1 structured items) — shown inside the
  // review/DNF bands. Anchored so the DNF "查看问题引证" button can scroll here.
  const rowsRef = useRef<HTMLDivElement>(null);
  const items = audit.items ?? [];
  const flaggedCount = items.length;
  const scrollToRows = () =>
    rowsRef.current?.scrollIntoView({ behavior: "smooth", block: "center" });

  return (
    <div
      className={cn(
        "not-prose my-3 rounded-xl border p-3.5 text-sm",
        tone === "pass" && "border-ok bg-[var(--ok-tint)]",
        tone === "review" && "border-review bg-[var(--review-tint)]",
        tone === "dnf" && "border-dnf bg-[var(--dnf-tint)] shadow-lg",
      )}
    >
      <AuditHeader tone={tone} audit={audit} />

      {/* DO-NOT-FILE band action buttons (gold FIG4): jump to the flagged rows
          + re-run the audit. Export gating lives in MessageActions. */}
      {tone === "dnf" ? (
        <div className="mt-2 flex gap-2">
          {flaggedCount > 0 ? (
            <button
              type="button"
              onClick={scrollToRows}
              className="rounded-lg border border-dnf px-3 py-1 text-xs font-semibold text-[var(--dnf)] hover:bg-[var(--dnf-tint)]"
            >
              查看问题引证 ({flaggedCount}) / Review flagged
            </button>
          ) : null}
        </div>
      ) : null}

      {/* Structured per-citation rows (review + DNF). Falls back to the legacy
          warning strings when the backend didn't surface structured items. */}
      {tone !== "pass" && flaggedCount > 0 ? (
        <div ref={rowsRef} className="mt-2.5">
          <AuditItemRows items={items} />
        </div>
      ) : tone === "review" && audit.warnings?.length ? (
        <ul className="mt-2 space-y-1">
          {audit.warnings.slice(0, 3).map((w, i) => (
            <li key={i} className="font-mono text-xs text-content-secondary">
              · {w}
            </li>
          ))}
          {audit.warnings.length > 3 ? (
            <li className="text-xs text-muted-foreground">+{audit.warnings.length - 3} more</li>
          ) : null}
        </ul>
      ) : null}
      {audit.reportMarkdown ? <AuditReport markdown={audit.reportMarkdown} /> : null}
    </div>
  );
}

function AuditHeader({ tone, audit }: { tone: string; audit: AuditData }) {
  if (tone === "pass") {
    return (
      <div className="font-semibold text-sm text-[var(--ok)]">
        ☑ 引证核验通过 / Citation audit passed
        {audit.coverageLine ? <span className="ml-2 font-normal text-xs">· {audit.coverageLine}</span> : null}
      </div>
    );
  }
  if (tone === "dnf") {
    return (
      <div>
        <div className="font-semibold text-[var(--dnf)]">⛔ DO NOT FILE — 禁止对外提交</div>
        <div className="mt-1 text-xs text-content-secondary">
          {audit.doNotFileReason ? (
            <>{audit.doNotFileReason}</>
          ) : (
            <>
              核验发现无法溯源或与原文不符的引证，已触发提交前升级流程。
              <br />
              Unverifiable or misquoted citations found; pre-filing escalation triggered.
            </>
          )}
        </div>
      </div>
    );
  }
  const flagged = audit.items?.length ?? audit.warnings?.length;
  const cov = audit.coverage;
  const passLine =
    cov?.totalCites != null && cov.confirmed != null
      ? `${cov.confirmed}/${cov.totalCites} 通过`
      : audit.coverageLine;
  return (
    <div className="font-semibold text-sm text-[var(--review)]">
      ⚠ {flagged ?? "若干"} 条引证需人工复核 / citations need review
      {passLine ? <span className="ml-2 font-normal text-xs">· {passLine}</span> : null}
    </div>
  );
}

function AuditReport({ markdown }: { markdown: string }) {
  const rendered = useMemo(
    () => (
      <ReactMarkdown remarkPlugins={[remarkGfm]}>{markdown}</ReactMarkdown>
    ),
    [markdown],
  );
  return (
    <details className="mt-2">
      <summary className="cursor-pointer text-xs font-medium text-muted-foreground hover:underline">
        查看核验报告 / View verification report
      </summary>
      <div className="prose-cjk mt-2 max-h-96 overflow-y-auto rounded-md bg-card p-3 text-[12.5px] leading-relaxed">
        {rendered}
      </div>
    </details>
  );
}

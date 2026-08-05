/**
 * Inline citation markers in answer prose (gold FIG2). A `[n]` marker becomes a
 * superscript accent badge + colored status dot with a hover-card that shows the
 * matching 资料来源 row. Wired into Streamdown via a `components.a` override on a
 * `#cite-n` sentinel href (fragments survive Streamdown's sanitizer, unlike a
 * custom `cite:` protocol). The override is only installed when the answer has a
 * parsed numbered sources table, so ordinary prose links keep default behavior.
 */

import type { ComponentProps, ReactNode } from "react";
import { HoverCardTrigger } from "@/components/ui/hover-card";
import {
  InlineCitation,
  InlineCitationCard,
  InlineCitationCardBody,
  InlineCitationSource,
} from "@/components/ai-elements/inline-citation";
import { cn } from "@/lib/utils";
import { rewriteSandboxHref } from "@/lib/artifacts";
import type { SourceRow } from "@/components/parts/SourcesList";
import type { AuditItem } from "@/lib/dataParts";
import { itemTone } from "@/components/parts/CitationRows";

type CiteStatus = SourceRow["status"];

const DOT: Record<CiteStatus, string> = {
  ok: "bg-[var(--ok)]",
  warn: "bg-[var(--review)]",
  bad: "bg-[var(--dnf)]",
  none: "bg-border",
};

/** Per-marker status: start from the row's own verified/checked signal, then
 * downgrade if a cite-check item clearly refers to this citation (bad wins). */
function statusForRow(row: SourceRow, items?: AuditItem[]): CiteStatus {
  let status = row.status;
  if (items?.length) {
    const hay = `${row.claim} ${row.pinpoint}`.toLowerCase();
    for (const it of items) {
      const needle = `${it.claimed ?? ""} ${it.location ?? ""}`.toLowerCase().trim();
      if (!needle) continue;
      // Match either direction — the item cite may be a substring of the row or vice versa.
      const claimed = (it.claimed ?? it.location ?? "").toLowerCase().trim();
      if (claimed && (hay.includes(claimed) || (row.pinpoint && needle.includes(row.pinpoint.toLowerCase())))) {
        const tone = itemTone(it);
        if (tone === "bad") return "bad";
        if (tone === "review" && status === "ok") status = "warn";
      }
    }
  }
  return status;
}

function InlineCite({ row, status }: { row: SourceRow; status: CiteStatus }) {
  return (
    <InlineCitation>
      <InlineCitationCard>
        <HoverCardTrigger asChild>
          <sup className="cursor-pointer whitespace-nowrap align-super text-[11px] font-semibold text-primary">
            [{row.index}]
            <span
              className={cn("ml-0.5 inline-block size-1.5 rounded-full align-middle", DOT[status])}
            />
          </sup>
        </HoverCardTrigger>
        <InlineCitationCardBody>
          <div className="space-y-2 p-3">
            <InlineCitationSource title={row.claim} description={row.note}>
              {row.pinpointHref ? (
                <a
                  href={rewriteSandboxHref(row.pinpointHref)}
                  target="_blank"
                  rel="noreferrer"
                  className="text-xs text-primary hover:underline"
                >
                  {row.pinpoint} ↗
                </a>
              ) : (
                <p className="truncate text-muted-foreground text-xs">{row.pinpoint}</p>
              )}
            </InlineCitationSource>
          </div>
        </InlineCitationCardBody>
      </InlineCitationCard>
    </InlineCitation>
  );
}

const MARKER = /\[(\d{1,3})\](?![([:])/g;

/**
 * Build the pieces needed to render inline citations from parsed source rows.
 * Returns null when there is nothing to key markers to. `rewrite` converts
 * `[n]` markers in prose to `[n](#cite-n)` sentinel links; `components` is the
 * Streamdown `components.a` override that turns those sentinels into hover-cards.
 */
export function buildInlineCitations(
  rows: SourceRow[] | null,
  items?: AuditItem[],
): {
  rewrite: (prose: string) => string;
  components: { a: (props: ComponentProps<"a"> & { node?: unknown }) => ReactNode };
} | null {
  if (!rows || rows.length === 0) return null;
  // Key by explicit index column when present, else 1-based row position.
  const byIndex = new Map<string, SourceRow>();
  rows.forEach((r, i) => {
    const key = (r.index ?? String(i + 1)).trim();
    if (key) byIndex.set(key, { ...r, index: r.index ?? String(i + 1) });
  });

  const rewrite = (prose: string): string =>
    prose.replace(MARKER, (full, n, offset: number, str: string) => {
      const prev = str[offset - 1];
      if (prev === "!" || prev === "]") return full; // image / footnote-ref / link label
      return byIndex.has(String(n)) ? `[${n}](#cite-${n})` : full;
    });

  const components = {
    a: ({ href, children, node: _node, ...props }: ComponentProps<"a"> & { node?: unknown }) => {
      if (typeof href === "string" && href.startsWith("#cite-")) {
        const key = href.slice("#cite-".length);
        const row = byIndex.get(key);
        if (row) return <InlineCite row={row} status={statusForRow(row, items)} />;
      }
      return (
        <a href={href} target="_blank" rel="noreferrer" {...props}>
          {children}
        </a>
      );
    },
  };

  return { rewrite, components };
}

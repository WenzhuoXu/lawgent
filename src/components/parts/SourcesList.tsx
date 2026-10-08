/**
 * Renders the answer's sources section (`## 资料来源` / `## Sources`) as a clean
 * LIST with per-source status dots (matching the approved artifact FIG 2).
 *
 * Answers write one numbered line per source — `1. [Source, pinpoint](URL) —
 * status` — with no claim column (the body already states the claims). Older
 * answers in the database carry a pipe table with a claim column; both parse.
 */

import { rewriteSandboxHref } from "@/lib/artifacts";

export type SourceRow = {
  index?: string;
  claim: string;
  pinpoint: string;
  pinpointHref?: string;
  status: "ok" | "warn" | "bad" | "none";
  note?: string;
};

const SOURCE_HEADING = /(^|\n)\s*#{0,4}\s*(资料来源[^\n]*|参考(?:资料|文献)[^\n]*|Sources?[^\n]*)\s*(\n|$)/i;

/** Split answer text into { body, sourcesMarkdown } at the sources heading. */
export function splitSources(text: string): { body: string; sources?: string } {
  const m = text.match(SOURCE_HEADING);
  if (!m || m.index == null) return { body: text };
  const cut = m.index + (m[1] ? 1 : 0);
  return { body: text.slice(0, cut).trimEnd(), sources: text.slice(cut) };
}

/** Extract a [label](url) markdown link → {label, href}; else plain text. */
function parseLinkCell(cell: string): { label: string; href?: string } {
  const link = cell.match(/\[([^\]]+)\]\(([^)]+)\)/);
  if (link) {
    const rest = cell.replace(link[0], "").replace(/^[，,、\s]+/, "").trim();
    return { label: `${link[1]}${rest ? ` ${rest}` : ""}`, href: link[2] };
  }
  return { label: cell.trim() };
}

function statusOf(cell: string): SourceRow["status"] {
  const c = cell.trim();
  // The user's own files: no online check applies, so no amber dot.
  if (/用户提供|user-provided/i.test(c)) return "none";
  if (/✔|✓|已?(在线)?核验|verified|checked|pass/i.test(c) && !/未|not|un/i.test(c)) return "ok";
  if (/未核验|unverified|unavailable|not|无法|待/i.test(c)) return "warn";
  if (/✕|✗|fail|错误|不符|未能溯源/i.test(c)) return "bad";
  return "none";
}

const LIST_ITEM = /^\s*(?:(\d+)[.)、]|[-*•])\s+(.+)$/;
// "— status" at the end of a line (em/en dash, or a spaced hyphen).
const TRAILING_STATUS = /\s+(?:—|–|-{1,2})\s+([^—–]+?)\s*$/;

/** Parse the one-line-per-source list form. */
function parseSourcesList(md: string): SourceRow[] | null {
  const out: SourceRow[] = [];
  for (const raw of md.split("\n").slice(1)) {
    const m = raw.match(LIST_ITEM);
    if (!m) continue;
    let rest = m[2].trim();
    let statusText = "";
    const st = rest.match(TRAILING_STATUS);
    if (st && st.index !== undefined) {
      statusText = st[1];
      rest = rest.slice(0, st.index).trim();
    }
    const pin = parseLinkCell(rest);
    out.push({
      index: m[1],
      claim: "",
      pinpoint: pin.label,
      pinpointHref: pin.href,
      status: statusText ? statusOf(statusText) : "none",
      note: statusText || undefined,
    });
  }
  return out.length ? out : null;
}

/** Parse the sources section into structured rows: the list form, or the
 * legacy claim/pinpoint table. Returns null when neither matches (the caller
 * falls back to plain markdown). */
export function parseSourcesTable(md: string): SourceRow[] | null {
  const lines = md.split("\n").map((l) => l.trim()).filter(Boolean);
  const rows = lines.filter((l) => l.startsWith("|") && l.endsWith("|"));
  if (rows.length < 2) return parseSourcesList(md);

  const cells = (line: string) =>
    line.slice(1, -1).split("|").map((c) => c.trim());

  const header = cells(rows[0]).map((h) => h.toLowerCase());
  // separator row is all dashes
  const dataRows = rows.slice(1).filter((r) => !/^\|[\s:|-]+\|$/.test(r));

  // Locate columns by header keywords.
  const col = (kw: string[]) => header.findIndex((h) => kw.some((k) => h.includes(k)));
  const iClaim = col(["主张", "claim", "内容"]);
  const iPin = col(["依据", "pinpoint", "出处", "条", "url"]);
  const iChk = col(["核验", "checked", "online", "状态", "status"]);
  const iNote = col(["备注", "note", "说明"]);
  const iIdx = col(["#", "序"]);
  if (iClaim < 0 || iPin < 0) return null;

  const out: SourceRow[] = [];
  for (const r of dataRows) {
    const c = cells(r);
    if (c.length < 2) continue;
    const pin = parseLinkCell(c[iPin] ?? "");
    out.push({
      index: iIdx >= 0 ? c[iIdx] : undefined,
      claim: c[iClaim] ?? "",
      pinpoint: pin.label,
      pinpointHref: pin.href,
      status: iChk >= 0 ? statusOf(c[iChk] ?? "") : "none",
      note: iNote >= 0 ? c[iNote] : undefined,
    });
  }
  return out.length ? out : null;
}

const DOT: Record<SourceRow["status"], string> = {
  ok: "bg-[var(--ok)]",
  warn: "bg-[var(--review)]",
  bad: "bg-[var(--dnf)]",
  none: "bg-border",
};

/** The section's own heading text (资料来源 or Sources), not a bilingual label. */
function headingOf(md: string): string {
  const first = md.trim().split("\n")[0] ?? "";
  const text = first.replace(/^#+\s*/, "").trim();
  return text || "资料来源";
}

export function SourcesList({ markdown }: { markdown: string }) {
  const rows = parseSourcesTable(markdown);
  if (!rows) return null;

  return (
    <section className="not-prose mt-4">
      <h3 className="mb-2 flex items-center gap-2 font-serif text-base font-semibold">
        <span>📚</span> {headingOf(markdown)}
        <span className="rounded-full bg-accent px-2 py-0.5 text-xs font-normal text-content-secondary">
          {rows.length}
        </span>
      </h3>
      <ol className="flex flex-col gap-2">
        {rows.map((r, i) => (
          <li
            key={i}
            className="flex gap-2.5 rounded-lg border bg-panel px-3 py-2.5 text-sm"
          >
            <span className={`mt-1.5 size-2 shrink-0 rounded-full ${DOT[r.status]}`} title={r.status} />
            <div className="min-w-0 flex-1">
              {r.claim ? (
                <div className="font-serif leading-snug text-foreground">
                  {r.index ? <span className="mr-1 font-mono text-xs text-content-faint">[{r.index}]</span> : null}
                  {r.claim}
                </div>
              ) : null}
              <div className={r.claim ? "mt-1 text-xs text-content-secondary" : "text-sm text-foreground"}>
                {!r.claim && r.index ? (
                  <span className="mr-1 font-mono text-xs text-content-faint">[{r.index}]</span>
                ) : null}
                {r.pinpointHref ? (
                  <a
                    href={rewriteSandboxHref(r.pinpointHref)}
                    target="_blank"
                    rel="noreferrer"
                    className="text-primary hover:underline"
                  >
                    {r.pinpoint} ↗
                  </a>
                ) : (
                  <span>{r.pinpoint}</span>
                )}
              </div>
              {r.note ? <div className="mt-0.5 text-xs text-content-faint">{r.note}</div> : null}
            </div>
          </li>
        ))}
      </ol>
    </section>
  );
}

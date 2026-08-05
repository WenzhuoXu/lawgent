# Tabular review methodology

Deep reference for `/tabular-review`. Read the section you need; do not
load the whole file when a single question is at issue.

## 1. Document-set intake

- **Row identity.** One row per reviewable instrument, not per file. A
  contract plus its amendments is normally ONE row (state which amendment
  a pinpoint comes from, e.g. `Amend.2 第3条`); unrelated documents in one
  PDF are split into separate rows. Key each row as
  `<filename> — <parties> — <date>` so the grid survives renames.
- **File handling.** `read_document` covers .pdf / .docx / .txt / .md /
  .xlsx / .csv. For PDFs, capture page numbers while reading — they are
  the fallback pinpoint when the document has no numbered clauses. A
  scanned PDF that yields no text layer is not skipped silently: keep the
  row and fill every cell with `UNREADABLE — no text layer`.
- **Rejects.** Images, code, spreadsheets that are data (not instruments),
  and duplicate copies go to `Out of scope` with one line of reasoning.
- **Order.** Keep the user's ordering if they gave one; otherwise sort by
  document date, oldest first.

## 2. Column design

Type every column before extraction begins. Three types:

| Type | Answer discipline | Example |
|---|---|---|
| **extract** | Verbatim or tightly normalized value from the document | governing-law clause text; LoL cap amount |
| **classify** | One value from a CLOSED enum defined in the column spec | `mutual / one-way / none`; contract type |
| **flag** | `YES / NO` + a ≤15-word risk note when YES | "uncapped indemnity?"; "residuals clause?" |

Rules:

- **Normalization is part of the column spec.** Dates → `YYYY-MM-DD`;
  money → `CCY 1,234,567` (original currency, no silent conversion);
  durations → months. The consistency pass (§6) enforces these.
- **Enums are closed.** If a document does not fit, the answer is
  `AMBIGUOUS — <why>`, not a new enum value invented mid-run.
- **One question per column.** "Term and renewal and termination" is
  three columns. Compound columns produce uncitable cells.
- **Comparative-law columns** ("is this liquidated-damages clause
  enforceable under PRC law?") are allowed but expensive: they need an
  authority citation in the bundle's `Sources` block on top of the
  in-document pinpoint. Prefer running them on the shortlist of rows the
  flag columns surface, not on the whole set.

## 3. Per-cell pinpoint formats

The `X — 依据` cell must locate the answer inside the row's document:

- **PRC-style instruments**: 第N条 / 第N条第M款 / 第N条第M款第(P)项;
  附件/附录 by name + clause. Quote 条 numbers as printed (中文数字 stays
  中文数字).
- **English-style contracts**: `cl. 12.3(b)`, `s. 4`, `Sch. 2 ¶1`,
  `Exhibit A §3`. Use the document's own labelling scheme.
- **Unnumbered documents / letters**: `p. N ¶M` (PDF page) or the heading
  path (`"Confidentiality" ¶2`) for .docx without page fidelity.
- **Multiple loci**: separate with `;` — `第8条; 第15条第2款` — when the
  answer genuinely rests on more than one clause (e.g. definition +
  operative clause).
- Never write only the document name, "throughout", or "general terms".
  If the answer truly has no citable locus, the cell is `NOT FOUND` or
  `AMBIGUOUS`, not a vague pinpoint.

Verbatim quotes inside a value cell must round-trip: re-find the exact
string in the document before writing it. Paraphrase (no quote marks)
when the wording is long; quote only decisive language.

## 4. Unresolved-cell taxonomy

| Marker | Meaning | Basis cell |
|---|---|---|
| `NOT FOUND` | Searched the whole document; provision absent | `—` |
| `AMBIGUOUS — <why>` | Conflicting or unclear provisions | cite ALL competing loci |
| `N/A — <why>` | Question inapplicable to this document type | `—` |
| `UNREADABLE — <why>` | Text not extractable (scan, corruption) | `—` |

`NOT FOUND` is a substantive finding (a missing clause is often the risk),
so it feeds the portfolio-level Findings bullets. Count all four markers
into `unresolved_cells` in the bundle.

## 5. Grid assembly (`write_xlsx`)

- **Sheet `Review Grid`.** Row 1 headers: `Document`, then each column
  pair `X`, `X — 依据`. One row per document, in intake order.
  `freeze_panes: "B2"` keeps headers and the document column visible.
- **Sheet `Notes`.** Legend for the four unresolved markers, one row per
  column with its type + question text (+ enum values), the preset name
  and file if one was used, extraction date, and the not-legal-advice
  line.
- Optional conditional formatting: highlight cells equal to `NOT FOUND`
  or beginning `AMBIGUOUS` so reviewers see gaps at a glance.
- Header language follows the user's language (presets carry 中文 and
  English headers; pick one, do not double up in the grid itself).

## 6. Consistency and QA pass

1. **Column-wise scan.** For each column, read the finished column top to
   bottom: identical questions must yield identically normalized answers.
   An outlier (one row's cap in USD when others are CNY; one enum
   spelling variant) is re-checked against its source, not averaged away.
2. **Re-read the artifact.** `inspect_xlsx` on the written file: sheet
   names, dimensions (rows = documents + 1, columns = 1 + 2×questions),
   and the preview rows match intent.
3. **Spot check.** Re-open at least 3 cells' pinpoints (pick 1 flag, 1
   extract, 1 unresolved) and confirm the cited clause supports the cell.
4. **Bundle.** Findings bullets are portfolio-level patterns ("7 of 9
   leases lack X [Doc3 第12条; …]"), not per-cell restatements. The
   `Sources` block lists each reviewed document once plus any external
   authorities, in the general playbook §5 line format — this is what
   `audit_citations` and the downstream cite-check gate see.

## 7. Scale and batching

- Inside one specialist run, extract **one document at a time**, all
  columns per pass — column-at-a-time re-reads every document N times.
- Large sets arrive as orchestrator fan-out: each sub-task gets a slice
  of documents and the SAME frozen column spec, returns its rows, and the
  merging pass runs §6 across the union. Do not spawn concurrency
  yourself; the workflow layer owns fan-out and its concurrency caps.
- If a document exceeds the context comfortably (very long facilities),
  extract per section using the clause buckets in the column spec, then
  fill remaining columns with a second targeted pass — never answer from
  memory of "typical" contracts.

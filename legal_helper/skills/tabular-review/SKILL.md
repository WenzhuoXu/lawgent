---
name: tabular-review
description: Review a set of documents as a grid — one row per document, one column per review question — with a mandatory pinpoint citation for every substantive cell. Use for portfolio / batch review across many documents (合同批量审查, 多文档尽调, data-room screen, contract portfolio comparison, NDA screening at scale, policy-pack comparison) when the user supplies several documents plus questions or a column preset and wants an .xlsx review grid with a findings summary. For a single-document deep review use /review-contract instead.
argument-hint: "<document set (files or directory)> [--columns <preset | question list>]"
allowed_connectors: [ecfr_search, courtlistener_search, eurlex_search, "pkulaw_*", flk_npc_search]
rag_collections: [general]
---

# /tabular-review — Multi-Document Grid Review

Docs-as-rows, questions-as-columns. Produce an auditable review grid where
every substantive answer carries its own pinpoint, plus a compact findings
bundle for the orchestrator.

**Not legal advice.** The grid is review work product; qualified counsel
must verify flagged cells before reliance.

## Output Contract (binding)

Emit exactly one block — nothing before, nothing after:

````
## Grid
- path: <absolute .xlsx path under outputs/>
- rows: <document count>  columns: <question count>
- unresolved_cells: <count marked NOT FOUND / AMBIGUOUS / UNREADABLE>

## Findings
- [portfolio-level claim, ONE sentence, inline pinpoint `[Doc, art./§/p.]`] (label: treaty | regulation | case | guidance | local-law | best-practice | risk | drafting)

## Out of scope
- ...

## Sources
- [document or authority] — [URL or local path] — supports findings #X[, online-checked: yes/no, pinpoint: confirmed/unavailable]
````

Hard ceiling **≤ 2000 characters**. Cell-level detail lives in the
workbook, not the bundle — per general playbook §4 the orchestrator owns
the user-facing presentation.

## Per-cell citation contract (binding)

- Every substantive column `X` is written as a pair: `X` and `X — 依据`.
- The 依据 (basis) cell holds the pinpoint inside that row's document —
  第N条第M款 / clause N.M / section / § / p. N — never a bare document name.
- An unanswered cell is exactly one of: `NOT FOUND` (searched, absent),
  `AMBIGUOUS — <why>` (conflicting clauses; cite both), `N/A — <why>`
  (question inapplicable to this document type), `UNREADABLE — <why>`.
  Never leave a cell blank.
- Verbatim quotes in cells must round-trip against the document text.
- Columns that test external law (e.g. enforceability of a clause) cite
  the authority in the `Sources` block per general playbook §2 + §5.

## Inputs

- **Document set** — chat attachments or a directory. `read_document`
  handles .pdf / .docx / .txt / .md; record page numbers for PDFs.
- **Column set** — the user's question list, or a preset from
  `resources/grid_templates/` (`generic_contract.yaml`, `nda_screen.yaml`).
  Confirm the final column list before extraction when the ask is vague.
- **Client side / risk posture**, when flag columns need a threshold.

## Workflow

1. Enumerate documents; one grid row each, keyed by filename + parties +
   date. List rejected non-document files under `Out of scope`.
2. Resolve columns: preset or user prompts; type every column as
   extract / classify / flag per `references/methodology.md`.
3. Per-document extraction pass, one document at a time: answer every
   column from that document only, reasoning per general playbook §3a;
   record value + basis pinpoint together, never the value alone.
4. Consistency pass column-by-column across rows: same normalization
   (dates, currency, enums), outliers re-checked against their source.
5. Assemble with `write_xlsx`: sheet `Review Grid` (header row =
   `Document` then each column pair, freeze panes `B2`), sheet `Notes`
   (legend, column definitions, preset provenance).
6. Re-read via `inspect_xlsx`; spot-check at least 3 cells against their
   sources; count unresolved cells. Then emit the bundle.

## Pointers

- Full methodology — column design, answer-type discipline, PRC / US / EU
  pinpoint formats, unresolved-cell taxonomy, grid layout, QA pass,
  batching at scale: `references/methodology.md`.
- Column presets: `resources/grid_templates/` (read via `read_document`;
  schema in that directory's `README.md`).
- Authority hierarchy + citation: `/playbook/general_playbook.md` §0 + §2;
  sanity check §3; sources line format §5.
- When the **aviation** pack is active, also apply
  `/domains/aviation/overlays/tabular-review.md` (portfolio presets and
  document-set intake deltas).

Cell language follows the source document and the user's language;
reasoning and tool calls may use whichever language fits the material.

---
name: draft-agreement
description: Draft a new agreement or PRC litigation document from deal terms — bilingual PRC-first contracts (NDA, services, license, employment and other commercial agreements) built from the clause library, plus PRC 文书生成 (民事起诉状 / 答辩状 skeletons per Civil Procedure Law formalities). Use when the user supplies a term sheet, deal points, or case facts and wants a complete first draft (起草合同 / 合同起草 / 文书生成 / draft an NDA). For reviewing an existing contract use /review-contract instead.
argument-hint: "<term sheet / deal terms / case facts> [--type nda|services|license|employment|起诉状|答辩状] [--side <party>] [--language zh|en|bilingual]"
allowed_connectors: [ecfr_search, courtlistener_search, eurlex_search, "pkulaw_*", flk_npc_search]
rag_collections: [general]
---

# /draft-agreement — Agreement & PRC Filing Drafting

Produce a complete, clause-annotated first draft as a `.docx` artifact,
plus a compact bundle for the orchestrator. Drafting decisions surface as
Findings; the prose lives in the document, not the bundle.

**Not legal advice.** Drafts must be reviewed and adapted by qualified
counsel before signature or filing.

## Output Contract (binding)

Emit exactly one block — nothing before, nothing after:

````
## Draft
- path: <absolute .docx path under outputs/>
- type: <agreement or filing type>  language: <zh / en / bilingual — controlling version stated>
- clauses: <operative clause count>  basis_verified: <count with verified legal basis>

## Findings
- [drafting decision, position taken, or gap needing client input, ONE sentence, inline pinpoint `[Source, art./§]`] (label: treaty | regulation | case | guidance | local-law | best-practice | risk | drafting)

## Out of scope
- ...

## Sources
- [statute / authority backing operative clauses] — [URL or local path] — supports clause(s) X[, online-checked: yes/no, pinpoint: confirmed/unavailable]
````

Hard ceiling **≤ 2000 characters**. Per general playbook §4 the
orchestrator owns the user-facing presentation.

## Clause-basis contract (binding)

- Every operative clause carries a **法律依据** (legal basis): statute /
  regulation + pinpoint (第N条 / art. / §), recorded in the draft's
  `Drafting Notes` annex and rolled up into the bundle `Sources`.
- PRC statutory pinpoints are re-verified before the draft is emitted —
  `pkulaw_fatiao` / `pkulaw_citation_validator` first, `flk_npc_search`
  fallback. Unverifiable → `pinpoint unavailable` inline, and the clause
  is softened, never propped up by an invented article number.
- Clause ids reuse the `/review-contract` clause buckets, so a
  draft → review round-trip shares vocabulary.
- Mandatory-content rules (e.g. statutory required terms for employment
  or filings) are checked as a gate, not a suggestion: a missing
  mandatory item is a `risk` Finding even if the client did not ask.

## Inputs

- **Deal terms** — term sheet, bullet points, or prior correspondence;
  for filings, the case facts, parties, and requested relief (诉讼请求).
- **Type** — contract family or PRC filing type; propose one if absent.
- **Side** the draft favours (discloser / recipient, licensor / licensee,
  employer / employee, 原告 / 被告) and risk posture.
- **Jurisdiction + governing law** (PRC default per general playbook §0)
  and **language** — zh, en, or bilingual; for PRC-law-governed bilingual
  drafts the Chinese version controls unless instructed otherwise.

## Workflow

1. Read the general playbook §0–§3a and the active domain-pack playbook;
   confirm type, side, governing law, and language before drafting.
2. Build the clause plan: start from the matching preset in
   `resources/clause_library/` (or `resources/prc_filings/` skeletons for
   起诉状 / 答辩状), then add / drop clauses per the deal terms. Never
   draft from memory of "typical" contracts when a library entry exists.
3. Draft clause-by-clause: adapt library text to the deal terms, keep the
   clause's `legal_basis` current, and record every deviation from the
   library default as a Finding with its rationale.
4. Verify each 法律依据 per the clause-basis contract above; check
   mandatory-content rules for the type (see `references/methodology.md`).
5. Assemble with `write_docx`: title page, operative clauses in plan
   order, signature / 具状人 block, then a `Drafting Notes` annex listing
   each clause's basis and open points. State the controlling language.
6. Sanity check per general playbook §3; the orchestrator's `/cite-check`
   gate runs on the final answer — never bypass it. Then emit the bundle.

## Pointers

- Full methodology — intake, clause-plan discipline, bilingual /
  controlling-language rules, PRC mandatory-content checks, filing
  formalities, QA: `references/methodology.md`.
- Clause presets: `resources/clause_library/` (schema in its `README.md`);
  filing skeletons: `resources/prc_filings/`.
- Authority hierarchy + citation: `/playbook/general_playbook.md` §0 + §2;
  sources line format §5; document production defaults §7.
- When the **aviation** pack is active, also apply
  `/domains/aviation/overlays/draft-agreement.md`.

Draft language follows the user and the deal; reasoning and tool calls
may use whichever language fits the material.

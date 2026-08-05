---
name: signature-request
description: Prepare a multi-party closing / execution package — identify who must sign which document, in what order, with what supporting evidence (board resolutions, KYC, lien releases, insurance certificates, regulator filings). Use for M&A closings, financing closings, JV launches, license executions, and other multi-document signings.
argument-hint: "<transaction summary and parties>"
allowed_connectors: [ecfr_search, eurlex_search, "pkulaw_*", flk_npc_search]
rag_collections: [general]
---

# /signature-request — Closing / Execution Package

Produce the analytical bundle the orchestrator turns into a closing
checklist + signature matrix.

**Not legal advice.** The bundle drives a draft package; counsel signs off
before execution.

## Output Contract (binding)

Same as `/playbook/general_playbook.md` §4 and §5:

````
## Findings
- [self-contained claim, ONE sentence, ≤60 English words, inline pinpoint `[Source, art./§/p.]`] (label: treaty | regulation | case | guidance | local-law | best-practice | risk | drafting)
- ...

## Out of scope
- ...

## Sources
- [source name] — [URL or local path] — supports findings #X[, online-checked: yes/no, pinpoint: confirmed/unavailable]
````

Hard ceiling **≤ 2000 characters**. No signature-matrix tables or closing
spreadsheets in the bundle — orchestrator authors those.

## Inputs

- **Transaction** type + value + economic terms summary.
- **Parties** with role (buyer / seller / borrower / lender / agent /
  trustee / insurer / regulator) + jurisdiction of incorporation.
- **Target closing date** + any external trigger (regulator approval,
  financing condition, foreign-investment review).
- **Conditions precedent** already met / outstanding.

## Closing sequence (generic)

1. **Pre-closing** — KYC / sanctions screen / UBO confirmation, board
   approvals (resolutions / written consents), authority delegation,
   regulator filings or notifications, lien searches, due-diligence
   confirmation, condition-precedent satisfaction.
2. **Closing** — sequence of executions (often parallel via escrow);
   funds-flow; release / discharge of pre-closing security; counterpart
   delivery via PDF + originals follow.
3. **Filing / registration** — recordation, public-registry filings,
   regulator notifications, tax / customs filings.
4. **Post-closing** — true-up, working-capital reconciliation, post-closing
   covenants calendar, escrow milestones.

## Signature-matrix workflow

For each document, surface a Finding with: document name, mandatory
signatories (party + capacity), supporting evidence required (resolutions,
PoA, KYC, insurance certificate), execution mode (wet-ink / e-sign / both),
sequencing dependency.

## Pointers

- Authority hierarchy + citation: `/playbook/general_playbook.md` §0 + §2.
- Sanity-check + label set: general playbook §3 + §4.
- Universal regulator / disclosure considerations: general playbook §6.
- When the **aviation** pack is active, also apply
  `/domains/aviation/overlays/signature-request.md` for registry filings
  and multi-party aircraft-transaction sequencing.

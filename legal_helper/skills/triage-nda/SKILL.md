---
name: triage-nda
description: Rapidly triage an incoming NDA and classify it GREEN (standard approval), YELLOW (counsel review), or RED (full legal review). Use when a new NDA arrives from sales, BD, procurement, or M&A; when screening for embedded non-solicits, non-competes, or missing carveouts; when checking jurisdiction-specific carveouts (state secrets, regulator disclosure, mandatory reporting); or when deciding whether an NDA can be signed under standard delegation.
argument-hint: "<NDA file or text>"
allowed_connectors: ["pkulaw_*", flk_npc_search, eurlex_search]
rag_collections: [general]
---

# /triage-nda — NDA Triage

Classify an NDA quickly: GREEN (standard sign), YELLOW (light counsel
review), RED (full legal review). Surface the deviations driving the
classification.

**Not legal advice.** This is internal triage; counsel signs off on
anything not GREEN.

## Output Contract (binding)

As a specialist, emit the harness's bundle contract (Findings · Issues ·
Artefacts · Out of scope · Sources), supplied with your task.

## Artefacts

The classification line (GREEN / YELLOW / RED with the triggering clauses) and the carve-out checklist.

## Classification rules

**GREEN — standard sign**
- Mutual; term ≤ 3 years; survival ≤ 5 years.
- Standard commercial scope; no non-compete / non-solicit beyond ordinary
  staff-poaching protection.
- Regulator-disclosure carveout present (or contract permits disclosure
  per law / court order).
- Standard purpose; no broad future-use rights.
- Standard governing law (NY / English / Singapore / PRC for PRC
  counterparties).

**YELLOW — counsel review**
- Unilateral (only disclosing or only receiving party protected).
- Term > 3 years but ≤ 7 years.
- Includes non-solicit (general staff) ≤ 12 months or non-compete ≤ 6
  months; no broad assignment.
- Residuals clause present but narrow (unaided memory of natural persons).
- Unfamiliar governing-law / arbitration seat.
- Industry-specific carveouts requested but generic-feasible.

**RED — full legal review**
- Non-compete > 6 months or geographically broad.
- Indemnity beyond actual damages / liquidated damages.
- Survival > 7 years.
- Bars disclosure to regulators / courts / mandatory reporters.
- Bars disclosure of state secrets / 国家秘密 status — illegal in PRC.
- Restricts disclosure of personal data subject to PIPL cross-border
  rules without a permitted pathway.
- Demands disclosure of trade secrets without segregation / return /
  destruction protocol.
- Demands assignment of receiver-created IP.

## Workflow

1. Parse the NDA — parties, mutual vs unilateral, scope, purpose,
   definitions of CI, exclusions, carveouts, obligations, term, survival,
   remedies, governing law, dispute resolution, residuals.
2. Apply the matrix above.
3. Emit one Finding per material deviation triggering YELLOW or RED.
4. Single Finding declares the overall classification with one-sentence
   rationale.

## Mandatory carveouts (all NDAs)

- Regulator inquiry / court order / mandatory reporting (per general
  playbook §6).
- State secrets (PRC counterparties).
- Disclosure to professional advisers under duty of confidentiality.
- Subpoena / discovery with notice obligation where lawful.

## Pointers

- Citation + sanity-check: `/playbook/general_playbook.md` §2 + §3.
- Universal risk defaults: general playbook §6.
- When the **aviation** pack is active, also apply
  `/domains/aviation/overlays/triage-nda.md` for industry-specific
  carveouts and export-control posture.

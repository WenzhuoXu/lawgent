---
name: review-contract
description: Review a commercial contract against a configurable playbook — flag deviations, generate redlines, and provide business-impact analysis. Covers M&A, services / SOW, license / IP, employment, distribution, supply, JV, settlement, and other commercial agreements. Use for clause-by-clause analysis against standard positions, prioritized redlines, and fallback positions.
argument-hint: "<contract file or text>"
allowed_connectors: [ecfr_search, courtlistener_search, eurlex_search, "pkulaw_*", flk_npc_search]
rag_collections: [general]
---

# /review-contract — Contract Review

Produce the analytical bundle the orchestrator turns into a redline +
business-impact memo. The skill identifies deviations from the playbook,
not user-facing prose.

**Not legal advice.** Drafts must be reviewed by qualified counsel before
counter-signing.

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

Hard ceiling **≤ 2000 characters**. No redline tables or "must-have /
should-have / nice-to-have" tier blocks in the bundle — those are the
orchestrator's job at authoring time.

## Inputs

- **Contract** (file attachment preferred; falls back to pasted text).
- **Which side** the client is on (party A / party B / mutual).
- **Contract type** (sale, services, licence, employment, distribution,
  M&A, NDA, supply, JV, settlement, lease).
- **Jurisdiction** + governing-law clause.
- **Stage** (term-sheet check, first-mark, redline response, closing).
- **Risk appetite** override if it differs from the playbook default.

## Workflow

1. **Read the playbook**: `/playbook/general_playbook.md` for global
   defaults; the active domain pack's playbook for domain positions; any
   user-supplied client-specific playbook.
2. **Read the contract structure** — parties, scope, term, fees,
   warranties, indemnities, limitation of liability, IP, confidentiality,
   data-protection, change of control, termination, dispute resolution,
   governing law.
3. **Clause-by-clause analysis**: surface one Finding per clause that
   deviates from the playbook, with the deviation type (missing /
   defective / one-sided / unenforceable / non-market) and a one-sentence
   fallback position.
4. **Cross-jurisdictional traps** — enforceability in PRC vs US vs EU
   (e.g. liquidated damages, exclusion of consequential loss, choice of
   forum, governing-language).
5. **Risk hooks** — flag any deviation that triggers an escalation per
   `/playbook/general_playbook.md` §6 (regulator-disclosure carveout,
   sanctions, data localisation, state secrets, privilege).
6. **Sanity check** per general playbook §3.

## Clause buckets (generic)

Parties + recitals; definitions; scope / SOW; fees + invoicing; terms +
termination; warranties; indemnities; limitation of liability;
confidentiality + NDA; data protection + DPA; IP ownership + licences;
service levels; security + audit; insurance; change of control;
assignment + novation; force majeure; dispute resolution + governing law;
boilerplate (notices, severability, entire-agreement, counterparts).

## Pointers

- Authority hierarchy + citation: `/playbook/general_playbook.md` §0 + §2.
- Sanity-check + label set: general playbook §3 + §4.
- Universal risk defaults (data, sanctions, privilege, state secrets,
  regulator-disclosure): general playbook §6.
- When the **aviation** pack is active, also apply
  `/domains/aviation/overlays/review-contract.md` for aircraft / charter /
  maintenance / OEM agreements.

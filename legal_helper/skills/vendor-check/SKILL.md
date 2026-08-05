---
name: vendor-check
description: Run third-party due diligence on a vendor / supplier / counterparty. Verify legal standing, sanctions / denied-party screening, ESG posture, certificate status, insurance, data-security posture, prior findings, and required agreements (MSA, DPA, SOW, etc.). Use when onboarding a new supplier, switching providers, or qualifying a SaaS vendor that will touch regulated data.
argument-hint: "<vendor name and proposed scope>"
allowed_connectors: [ecfr_search, eurlex_search, "pkulaw_*", flk_npc_search]
rag_collections: [general]
---

# /vendor-check — Third-Party Diligence

Produce the analytical bundle the orchestrator turns into a vendor-onboarding
memo + scoring sheet.

**Not legal advice.** The bundle drives a recommendation; counsel + the
business owner approve onboarding.

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

Hard ceiling **≤ 2000 characters**. No scoring tables — orchestrator
authors those at presentation time.

## Inputs

- **Vendor / counterparty** (legal name, jurisdiction, parent / UBO).
- **Proposed scope** (services, data flows, geography, duration, value).
- **Risk posture** of the engagement (low / medium / high) per playbook.

## Generic diligence checklist

1. **Legal standing** — entity exists, in good standing, not in bankruptcy
   / liquidation; UBO disclosed.
2. **Sanctions / denied-party screening** — OFAC / EU / UN / UK / MOFCOM /
   BIS / DDTC; PEP and adverse-media check.
3. **Licensing / certification** — any sector-specific cert required for
   the proposed scope (data security, financial conduct, healthcare,
   environmental, safety).
4. **Insurance** — liability / cyber / E&O / product-liability minima per
   business risk; additional-insured / waiver-of-subrogation availability.
5. **Data-protection posture** — DPA available, sub-processors disclosed,
   cross-border transfer pathway (PIPL / GDPR / SCC / CAC SCC), security
   certifications (ISO 27001 / SOC 2 Type II), encryption + access
   controls.
6. **Financial standing** — solvency check, audited financials if material
   contract, indemnity-coverage capacity.
7. **Anti-corruption / ESG** — code of conduct, modern-slavery statement
   where applicable, climate disclosures where applicable.
8. **Prior findings / litigation** — adverse-media + court / regulator
   records (use `flk_npc_search`, `courtlistener_search`).
9. **Required agreements** — MSA, SOW, DPA, security addendum, audit-rights
   addendum, IP terms, exit-and-transition.

## Workflow

1. Capture inputs + scope.
2. Apply the checklist; surface one Finding per item with a status
   (`confirmed` / `gap` / `risk`) and the required next step.
3. Add risk-band signal (low / medium / high) inline per Finding.
4. Flag any **deal-breaker** items separately as a top Finding.

## Pointers

- Authority hierarchy + citation: `/playbook/general_playbook.md` §0 + §2.
- Universal risk defaults (data, sanctions, regulator disclosure):
  general playbook §6.
- When the **aviation** pack is active, also apply
  `/domains/aviation/overlays/vendor-check.md` for industry-specific
  certification, capability, and data-flow diligence.

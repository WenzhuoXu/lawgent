---
name: compliance-check
description: Run a compliance check or standalone legal enquiry on a proposed action, new product/feature, business launch, vendor change, data flow, incident, or regulated activity. Surfaces applicable statutes, regulations, agency guidance, required approvals/filings, reporting duties, and risk areas. Use when launching a new product or service, when modifying a regulated process, when handling regulated data, when a vendor change touches controlled data or sanctions, or when the user asks what legal requirements apply to a scenario.
argument-hint: "<action or initiative to check>"
allowed_connectors: [ecfr_search, federal_register_search, govinfo_search, eurlex_search, "pkulaw_*", flk_npc_search, ethiopia_law_search, ethiopia_law_fetch]
rag_collections: [general]
---

# /compliance-check — Compliance Review

Run a compliance check on a proposed action, initiative, or standalone legal
enquiry. Domain packs add their own regulatory scope on top of the generic
hierarchy.

**Not legal advice.** Compliance assessments must be reviewed by qualified
counsel and the responsible operating officers (compliance officer, DPO,
safety / security director, etc.). Regulatory requirements change
frequently; verify against authoritative sources before reliance.

## Output Contract (binding)

Same as `/playbook/general_playbook.md` §4 and §5 require. Emit exactly:

````
## Findings
- [self-contained claim, ONE sentence, ≤60 English words, inline pinpoint `[Source, art./§/p.]`] (label: treaty | regulation | case | guidance | local-law | best-practice | risk | drafting)
- ...

## Out of scope
- ...

## Sources
- [source name] — [URL or local path] — supports findings #X[, online-checked: yes/no, pinpoint: confirmed/unavailable]
````

Hard ceiling **≤ 2000 characters**. No tables, matrices, walkthroughs, or
sanity-check sections — those are forbidden in specialist bundles.

## Inputs

```
/compliance-check <action or initiative>
```

Ask for, or assume and state explicitly, these facts:

1. **Action / initiative** in detail.
2. **Entity** involved (company, subsidiary, individual).
3. **Operating certificate / licence / registration** that may be touched.
4. **Geographic scope** (countries, regions, regulators).
5. **Timeline / target date**.
6. **Data involved** (personal information, sensitive personal information,
   regulated data, trade secrets).

If a fact is missing, state the assumption inline and proceed; do not block.

## Workflow

1. **Identify the regulatory framework** under each relevant jurisdiction
   following the source hierarchy in
   `/playbook/general_playbook.md#0-jurisdiction--source-hierarchy`.
   Default to PRC first; add US/EU only when relevant.
2. **Research authoritative sources** via connectors when available
   (`flk_npc_search` for PRC, `ecfr_search` / `federal_register_search` /
   `govinfo_search` for US, `eurlex_search` for EU). Fall back to hosted
   web search; quote primary sources with pinpoints.
3. **Required approvals / filings**: surface as one Findings bullet per
   approval, with authority + trigger + window.
4. **Reporting duties**: one bullet per authority (regulator / data
   protection / financial regulator / safety regulator / tax / customs).
5. **Risk areas**: one bullet per concrete risk with a soft severity label
   inline (high / medium / low).
6. **Sanity check** (general playbook §3): every claim has a source; no
   invented article numbers, no invented agency forms.

## Pointers

- General authority hierarchy: `/playbook/general_playbook.md` §0.
- Citation + language rules: general playbook §1 + §2.
- Universal risk defaults (data localisation, state secrets, sanctions,
  privilege): general playbook §6.
- When the **aviation** pack is active, also apply
  `/domains/aviation/overlays/compliance-check.md`.

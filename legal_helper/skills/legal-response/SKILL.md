---
name: legal-response
description: Draft a careful response to a regulator, authority, or counterparty communication — agency inquiry, enforcement letter, formal investigation notice, data-subject access request, or counterparty legal demand. Use for monitor letters, regulator findings, on-the-record requests, DSARs, official complaints, and internal incident escalations. Produces the analysis bundle the orchestrator turns into a draft reply.
argument-hint: "<incoming communication topic or attachment>"
allowed_connectors: [ecfr_search, federal_register_search, govinfo_search, eurlex_search, "pkulaw_*", flk_npc_search, ethiopia_law_search, ethiopia_law_fetch]
rag_collections: [general]
---

# /legal-response — Authority / Counterparty Response

Draft analysis for responses to a regulator, authority, or counterparty
demand. The skill produces the underlying claims, options, and limits; the
orchestrator authors the final letter or filing.

**Not legal advice.** Drafts must be reviewed by qualified counsel before
service / filing.

## Output Contract (binding)

Same as `/playbook/general_playbook.md` §4 and §5. Emit exactly:

````
## Findings
- [self-contained claim, ONE sentence, ≤60 English words, inline pinpoint `[Source, art./§/p.]`] (label: treaty | regulation | case | guidance | local-law | best-practice | risk | drafting)
- ...

## Out of scope
- ...

## Sources
- [source name] — [URL or local path] — supports findings #X[, online-checked: yes/no, pinpoint: confirmed/unavailable]
````

Hard ceiling **≤ 2000 characters**. No tables, walkthroughs, action
matrices, or sanity-check sections.

## Inputs

Provide the inbound communication or its key facts:
- Sender + authority basis (statutory section, treaty article, contract
  clause).
- Request scope (information, document production, attestation, written
  position).
- Response window + service mode (electronic, certified mail, in-person,
  formal filing).
- Jurisdiction + venue (which body, which body's procedural rules).
- Privilege posture (privileged / discoverable / mixed).

## Workflow

1. **Classify the communication**: regulator inquiry / enforcement / on-the-
   record request / DSAR / counterparty demand / discovery / subpoena.
2. **Identify the controlling authority** under each implicated
   jurisdiction. Confirm the sender's statutory or contractual basis.
3. **Map duties and limits**: what must be answered, what may be answered,
   what must not be disclosed (privilege, state secrets, third-party
   confidentiality, contractual confidentiality), and what carveouts apply
   (regulator disclosure, court order, mandatory reporting).
4. **Surface response options** as separate Findings bullets: full response,
   partial response with objections, request for extension, request for
   clarification, motion to quash / objection.
5. **Reporting / escalation map**: who else needs to know (DPO, regulator,
   insurer, board, outside counsel).
6. **Sanity check** per general playbook §3. No invented citations.

## Reporting / authority taxonomy

Use the right authority bucket per implicated jurisdiction:

- PRC: 监管约谈, 行政询问, 监察建议, 检察建议, 行政复议, DSAR under
  PIPL, 仲裁/诉讼 demand.
- US: agency LOI, formal investigation, civil investigative demand, GAO
  audit letter, subpoena, DSAR/CCPA request.
- EU: regulator notice (national competent authority), GDPR Art. 15 DSAR,
  ESMA / EBA inquiry, court order, DPC investigation.

## Pointers

- Citation + language: `/playbook/general_playbook.md` §1 + §2.
- Sanity check: general playbook §3 + §4.
- Universal regulator disclosure carveout: general playbook §6.
- When the **aviation** pack is active, also apply
  `/domains/aviation/overlays/legal-response.md`.

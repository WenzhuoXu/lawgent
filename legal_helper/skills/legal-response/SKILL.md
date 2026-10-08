---
name: legal-response
description: Draft a careful response to a regulator, authority, or counterparty communication — agency inquiry, enforcement letter, formal investigation notice, data-subject access request, or counterparty legal demand. Use for monitor letters, regulator findings, on-the-record requests, DSARs, official complaints, and internal incident escalations. Produces the analysis behind a draft reply.
argument-hint: "<incoming communication topic or attachment>"
allowed_connectors: [ecfr_search, federal_register_search, govinfo_search, eurlex_search, "pkulaw_*", flk_npc_search, ethiopia_law_search, ethiopia_law_fetch]
rag_collections: [general]
---

# /legal-response — Authority / Counterparty Response

Draft analysis for responses to a regulator, authority, or counterparty
demand: the underlying claims, options, and limits behind the final letter
or filing.

**Not legal advice.** Drafts must be reviewed by qualified counsel before
service / filing.

## Output Contract (binding)

As a specialist, emit the harness's bundle contract (Findings · Issues ·
Artefacts · Out of scope · Sources), supplied with your task. As the author of
a final answer, follow the author contract instead and put these artefacts in
the answer where they help the reader.

Artefacts this skill produces: the response outline, or draft paragraphs when the task asks for a draft.

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
7. **A reply or 律师函 delivered as a file** is written with `disclaimer="none"`
   and a `disclaimer_reason` — the letter goes out under counsel's name.

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
- Sanity checks: general playbook §3.
- Universal regulator disclosure carveout: general playbook §6.
- When the **aviation** pack is active, also apply
  `/domains/aviation/overlays/legal-response.md`.

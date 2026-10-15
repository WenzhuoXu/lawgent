---
name: brief
description: Generate a contextual legal briefing — daily scan, standalone legal enquiry / topic research, or incident response. Use to start the day with a scan of legal-relevant items (open regulator correspondence, contract milestones, internal counsel queue), to research a specific legal question across internal and authoritative public sources, or to assemble rapid context on a developing situation (regulator inquiry, contract dispute, incident, enforcement action).
argument-hint: "[daily | topic <query> | incident]"
allowed_connectors: [ecfr_search, federal_register_search, courtlistener_search, eurlex_search, "pkulaw_*", flk_npc_search, ethiopia_law_search, ethiopia_law_fetch]
rag_collections: [general]
---

# /brief — Legal Team Briefing

Generate contextual briefings for legal work. Supports three modes: daily,
topic / standalone legal enquiry, and incident.

**Not legal advice.** Briefings are work product; qualified counsel must
review before reliance.

## Output Contract (binding)

As a specialist, emit the harness's bundle contract (Findings · Issues ·
Artefacts · Out of scope · Sources), supplied with your task.

Artefacts this skill produces: none beyond the bundle.

## Invocation

```
/brief daily              # Morning brief of legal-relevant items
/brief topic [query]      # Research brief on a specific legal question
/brief incident [topic]   # Rapid brief on a developing situation
```

If no mode is specified, ask which.

## Modes

### Daily
Scan connected sources (email, calendar, chat, CLM, CRM/case mgmt) for:
new contract requests, regulator correspondence, compliance questions,
counterparty responses on active negotiations, contract milestones,
upcoming statutory deadlines, internal team escalations.

### Topic / standalone enquiry
1. Restate the question + assumptions (parties, jurisdiction, facts).
2. Identify the governing hierarchy (see `/playbook/general_playbook.md#0-jurisdiction--source-hierarchy`).
3. Research authoritative sources first (statutes, regulations, agency
   guidance, case law). Use connectors when available; fall back to hosted
   web search. Quote primary sources with pinpoints.
4. Emit Findings with one claim per bullet and explicit authority labels.

### Incident
Rapid context on a developing situation. Compile: situation summary,
timeline, immediate legal considerations (reporting obligations,
preservation, privilege, insurance notification), regulatory notifications
(authority / trigger / window / status), relevant agreements, internal
response, key contacts, recommended immediate actions, information gaps.

## Workflow

1. Confirm mode + scope; refuse out-of-scope items in the `Out of scope`
   block rather than glossing them.
2. Apply jurisdiction default from settings (`default_jurisdiction`); flag
   when comparative jurisdictions enter the analysis.
3. Run claim-by-claim sanity check before emitting (see general playbook
   §3). Soften unsupported claims with "may" / "typically" / `pinpoint
   unavailable`.

## Pointers

- General language + citation rules: `/playbook/general_playbook.md` §1, §2.
- Sanity checks: general playbook §3; finding labels: the bundle contract.
- Sources line format: the specialist bundle contract (supplied with the task).
- When the **aviation** domain pack is active, also apply
  `/domains/aviation/overlays/brief.md`.

## Output language

Follow the user's input language. Internal reasoning, sub-agent messaging,
and tool calls may use whichever language fits the material (中文 encouraged
for PRC sources). Quote primary sources in original language with translation.

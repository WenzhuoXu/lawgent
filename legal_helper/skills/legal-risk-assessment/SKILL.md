---
name: legal-risk-assessment
description: Assess and classify legal risks using a severity-by-likelihood framework with escalation criteria. Use when evaluating contract risk, deal exposure, regulatory enforcement risk, litigation exposure, sanctions / export-control risk, data-protection risk, IP risk, employment risk, or whether a matter requires senior counsel or outside legal review.
argument-hint: "<scenario or matter to assess>"
allowed_connectors: [ecfr_search, federal_register_search, courtlistener_search, eurlex_search, "pkulaw_*", flk_npc_search]
rag_collections: [general]
---

# /legal-risk-assessment — Risk Classification

Score and triage legal risk using a 5×5 severity × likelihood matrix.
Domain packs add their own risk categories; the matrix itself is universal.

**Not legal advice.** Risk scores are working drafts for the orchestrator
to synthesize; counsel must validate before any business decision.

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

Hard ceiling **≤ 2000 characters**. No matrices, walkthroughs, sanity-check
sections, or evidence-verification logs in your output — those are the
orchestrator's job.

## Severity × Likelihood Matrix

Severity bands:
1. **Trivial** — cure inside the operating team; no legal exposure.
2. **Minor** — internal counsel cure; cost / disruption only.
3. **Moderate** — contractual, regulatory, or reputational exposure with
   bounded financial loss.
4. **Major** — material litigation / enforcement / sanctions exposure;
   board reporting threshold.
5. **Catastrophic** — going-concern threat, criminal exposure, licence
   revocation, lives-at-stake reporting failure.

Likelihood bands:
1. **Rare** — once in 10 years for this organisation in normal course.
2. **Unlikely** — fewer than once a year.
3. **Possible** — once a year.
4. **Likely** — more than once a year.
5. **Almost certain** — recurring / structural exposure.

Colour bands (residual risk after current controls): **GREEN** (1–6),
**YELLOW** (7–11), **ORANGE** (12–17), **RED** (18–25).

## Generic risk categories

Surface one Findings bullet per identified risk, with severity × likelihood
× residual band inline:

- **Regulatory** — statute / regulation / agency guidance violation.
- **Contractual** — breach, indemnity exposure, hell-or-high-water, change
  of control, MAC.
- **Litigation** — pending or threatened civil action, class exposure,
  forum selection.
- **IP** — infringement, ownership clarity, freedom-to-operate.
- **Employment / labour** — workforce dispute, statutory rights,
  whistleblower exposure.
- **Data / privacy** — PIPL / GDPR / CCPA breach, cross-border transfer.
- **Sanctions / export control** — OFAC, EU, UN, MOFCOM, BIS, DDTC.
- **Anti-corruption** — FCPA, UKBA, 反贪污法.
- **Tax** — characterisation risk, withholding, transfer pricing.
- **Criminal** — criminal liability of entity or officers.
- **Reputational** — public-trust impact, regulator / media attention.
- **Governance** — board duty, disclosure, controlling-shareholder issues.
- **ESG** — environmental, social-licence, climate-reporting.

Add domain-pack-specific categories when a pack is active.

## Workflow

1. State the scenario, parties, and jurisdiction(s).
2. Identify the *applicable* risk categories (drop categories not in scope
   rather than padding).
3. For each in-scope risk: severity score + likelihood score + residual
   band + a one-sentence mitigation handle.
4. Flag **escalation triggers** (any RED band, any 5-severity regardless of
   likelihood, any criminal-exposure or licence-revocation possibility).

## Pointers

- Authority hierarchy + citation: `/playbook/general_playbook.md` §0 + §2.
- Sanity-check + label set: general playbook §3 + §4.
- Universal risk defaults: general playbook §6.
- When the **aviation** pack is active, also apply
  `/domains/aviation/overlays/legal-risk-assessment.md`.

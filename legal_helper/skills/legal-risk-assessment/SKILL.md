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

**Not legal advice.** Risk scores are working drafts; counsel must
validate them before any business decision.

## Output Contract (binding)

As a specialist, emit the harness's bundle contract (Findings · Issues ·
Artefacts · Out of scope · Sources), supplied with your task. As the author of
a final answer, follow the author contract instead and put these artefacts in
the answer where they help the reader.

Artefacts this skill produces: the risk matrix rows (risk · severity 1-5 · likelihood 1-5 · residual band · mitigation · escalation trigger).

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
- Sanity checks: general playbook §3; finding labels: the bundle contract.
- Universal risk defaults: general playbook §6.
- When the **aviation** pack is active, also apply
  `/domains/aviation/overlays/legal-risk-assessment.md`.

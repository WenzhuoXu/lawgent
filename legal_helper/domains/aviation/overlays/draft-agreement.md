# Aviation overlay — /draft-agreement

Layered when the aviation domain pack is active. Adds aviation drafting
presets on top of the generic clause library; the generic clause-basis
contract and mandatory-content gates in `SKILL.md` still apply.

## Aviation draft types

- **Aircraft operating lease / sublease** — start from the `services` +
  `license` grant/termination patterns, then add the aviation clause
  block below. Sublease drafts also need the head-lease consent chain.
- **Wet lease / ACMI / charter** — AOC responsibility split, substituted
  aircraft equivalence, cancellation/ground-stop fee schedule (playbook
  §12).
- **MRO / engineering services** — generic `services` preset plus
  capability-list scope, AOG response SLA, parts title/warranty flow, cap
  2× work-order value (playbook §6).
- **LOI / term sheet** — mark binding vs non-binding clauses expressly
  (confidentiality, exclusivity, governing law binding; commercials not).

## Aviation clause block (lease-family drafts)

Each clause keeps the library schema: bucket id, bilingual text where the
deal needs it, and a `legal_basis` verified per the clause-basis
contract.

- **hell_or_high_water** — absolute, unconditional rent; no set-off /
  abatement (playbook §6).
- **registration_deregistration** — state-of-registry filings; IDERA in
  approved form + deregistration power of attorney; Cape Town / IR
  filings where the registry state has the Article 13 declarations
  (playbook §2). PRC-registered aircraft: CCAR nationality-registration
  formalities and CAAC filing chain per the pack playbook.
- **insurance** — hull + liability minima per playbook §1; AVN52E /
  AVN67B endorsements; severability of interest; waiver of subrogation;
  cancellation-notice period.
- **return_conditions** — hours / cycles to next check, LLP minimums,
  EGT margin, records continuity, EOL compensation (playbook §5).
- **ad_sb_allocation** — pre-existing vs post-delivery cost split with
  the playbook §4 default threshold; mandatory vs alert vs desirable.
- **quiet_enjoyment** — lessor covenant conditioned only on no default.
- **maintenance_reserves** — rates, adjustment mechanics, draw
  conditions, top-up at return.
- **subleasing_assignment** — consent standard, IDERA reissuance + IR
  re-filing + insurance + regulator non-objection on novation (playbook
  §9).
- **export_control** — ITAR / EAR / OFAC representations and
  denied-party screening (playbook §11); pairs with the generic
  `regulator_carveout`.
- **governing_law_dispute** — override the generic PRC default: NY law /
  English law preferred, Irish for leasing structures, Singapore / HK
  for Asia-Pac deals; sovereign-immunity waiver for state-owned
  operators (playbook §8). Keep the PRC-law analysis for CAAC-facing
  regulatory annexes even when the lease is foreign-law governed.

## Filing / regulator documents

`resources/prc_filings/` skeletons are for PRC civil litigation only.
CAAC-facing applications (route approvals, registrations, AOC matters)
are regulatory correspondence — route them through `/legal-response`
with this pack's playbook, not through the filing skeletons.

## Drafting traps

- Cape Town remedies text is ineffective if the registry state lacks the
  relevant declarations — verify before importing IDERA boilerplate.
- Bilingual lease drafts for PRC lessees: the English text normally
  controls when the lease is NY / English law governed — state it; the
  generic Chinese-controls default does NOT apply.
- Tax characterisation (true lease vs disguised loan) language interacts
  with Section 1110 protection (playbook §3); do not soften
  hell-or-high-water or title covenants without flagging it.

# Aviation overlay — /litigation-analysis

Layered when the aviation domain pack is active. The generic element,
chronology, and authority-verification contracts in `SKILL.md` still
apply; this overlay changes which law supplies the elements and — above
all — which limitation regime runs.

## Aviation matter types

- **Passenger / baggage / cargo claims** — international carriage under
  MC99 / Warsaw system (treaty texts in the `aviation_treaties` RAG
  collection, pack playbook §0b); domestic PRC carriage under
  民用航空法 + CAAC consumer rules. Determine which regime applies
  **first** — elements, limits, and limitation all follow from it.
- **Lease / financing default and repossession** — contract elements
  from the generic presets plus Cape Town / IDERA remedies where the
  registry state's declarations support them (pack playbook §2);
  Section 1110 timelines for US debtor counterparties (§3).
- **Insurance recovery** — policy + AVN endorsement construction; hull /
  liability minima context in pack playbook §1.
- **CAAC administrative penalty / license actions** — administrative
  reconsideration and litigation track, not civil (below).

## Limitation deltas (override the generic §188 default)

Verify each item live before relying on it; these regularly displace the
generic 3-year period:

- **MC99 Art. 35** — right to damages **extinguished** two years from
  arrival / scheduled arrival. Widely treated as a condition precedent /
  除斥期间-like bar, not an ordinary limitation open to 中止 / 中断 —
  flag any tolling argument as contested and cite forum case law.
- **民用航空法 第135条** — two-year 诉讼时效 for carriage claims under
  the domestic regime (re-verify the 条号 against the current revision).
- **Administrative track** — 行政复议 within 60 days
  (行政复议法, 2023 revision); 行政诉讼 within six months
  (行政诉讼法 第46条). Missing the reconsideration window can foreclose
  the litigation route for reconsideration-first matters.
- Cargo claims: check the air waybill / conditions of carriage for
  **notice-of-claim periods** (MC99 Art. 31 — 14 days damage / 21 days
  delay) that run before any limitation question arises.

## Element-table deltas (carrier-liability claims, MC99)

- **Applicability**: international carriage between States Parties
  (Art. 1) — ratification status of both ends decides Warsaw vs MC99;
  state each party's status per the general playbook §0 discipline.
- Passenger death / injury: accident on board or embarking/disembarking
  (Art. 17); two-tier liability with the strict tier up to the current
  revised SDR limit (Art. 21 — limits revised effective 2024,
  re-verify the figure); carrier negligence defense only above the
  strict tier.
- Delay: Art. 19 with the all-necessary-measures defense; baggage /
  cargo limits Art. 22 (revised alongside Art. 21).
- Contributory negligence defense: Art. 20.
- Forum: Art. 33 jurisdictions (carrier domicile, principal place of
  business, place of contract, destination; fifth jurisdiction for
  passenger claims) — run the forum screen before the merits.

## Chronology and evidence deltas

- Chronology rows for carriage claims must pin: ticket / AWB issuance,
  scheduled vs actual arrival, written complaint dates (Art. 31
  notice), and the two-year bar date.
- Technical records (maintenance releases, AD/SB status, flight and
  crew records) are element evidence in lease-return and insurance
  disputes — pinpoint to the record entry, not the binder.
- Accident-investigation reports: check admissibility and purpose
  limits in the forum before treating findings as liability evidence
  (投诉/调查 documents are often non-binding on civil liability).

## Authority deltas

- PRC aviation case law via `pkulaw_case_search` / `get_case_list`
  (案由: 航空运输人身损害责任纠纷, 航空运输合同纠纷, 融资租赁合同纠纷);
  US comparative via `courtlistener_search` (MC99 interpretation,
  Section 1110, Cape Town enforcement).
- Treaty interpretation questions cite the treaty article first, then
  forum case law on it — treaty > national statute per the pack
  playbook §0d research order.
- Escalate per pack playbook §14 when the matter involves an accident,
  a regulator enforcement action, or repossession against an operating
  carrier.

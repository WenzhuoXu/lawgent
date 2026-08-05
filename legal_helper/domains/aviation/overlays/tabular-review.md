# Aviation overlay — /tabular-review

Layered when the aviation domain pack is active. Adds portfolio presets
and document-set intake deltas for fleets of aircraft agreements.

## Intake deltas

- **Row key = MSN / registration mark**, not filename. A lease plus its
  side letters, novations, and lease supplements is ONE row; pinpoints
  name the instrument (`Lease cl. 12.3`, `Side Letter 2 ¶4`,
  `Novation Deed cl. 5`).
- Group by aircraft even when one master lease covers several — one row
  per airframe, with the master's clause pinpoints repeated per row.
- Engines leased separately from airframes get their own rows (row key =
  ESN).

## Lease-portfolio column preset

Use these columns (pair each with `X — 依据` per the generic contract);
thresholds come from the aviation pack playbook sections cited:

- **cross_default** (flag) — does default under any other lease /
  facility with the lessor group trigger default here? Note the basket.
- **registration_deregistration** (extract) — state of registry;
  deregistration mechanics: IDERA in approved form + DPoA, who holds
  them, and whether an IR filing + Article 13 declaration check is
  recorded (playbook §2, Cape Town).
- **return_conditions** (extract) — hours / cycles to next major check,
  LLP minimum remaining cycles, EGT margin, records-continuity and EOL
  compensation mechanics (playbook §5).
- **insurance_minima** (extract) — hull agreed value and combined
  single limit vs playbook §1 minimums; AVN52E / AVN67B endorsements;
  cancellation-notice days.
- **hell_or_high_water** (flag) — is rent absolute and unconditional;
  any set-off or abatement carve-in (playbook §6).
- **quiet_enjoyment** (classify: `absolute / conditional / absent`) —
  lessee protection on lessor financing or transfer.
- **maintenance_reserves** (extract) — reserve rates by check type,
  adjustment formula, and whether reserves are lessor property.
- **subleasing** (classify: `consent_required / permitted_listed /
  prohibited`) — sub-lease / wet-lease permissions and conditions
  (playbook §10).
- **ad_sb_allocation** (extract) — AD / SB cost split, pre-existing vs
  post-delivery, threshold amount (playbook §4 default $1M).
- **governing_law_forum** (extract) — governing law (NY / English /
  Irish per playbook §8), forum, and sovereign-immunity waiver for
  state-owned operators.

## Aviation consistency-pass additions

- Normalize hours / cycles as integers, EGT margin in °C, reserve rates
  as `CCY/FH` or `CCY/FC` — never mix per-hour and per-cycle values in
  one column.
- `NOT FOUND` on **registration_deregistration** or **insurance_minima**
  is always a portfolio-level Finding (label: risk), not just a cell.
- Cross-check the same MSN across columns: a return-conditions delta vs
  delivery conditions, or reserves inconsistent with the check intervals
  cited, gets re-verified before the grid ships.

## Comparative-law columns (run on shortlisted rows only)

- Cape Town effectiveness for the state of registry (declarations,
  Article 13) — cite the treaty status table in the bundle `Sources`.
- CAAC / CCAR registration prerequisites for PRC-registered airframes —
  verify via the pack's PKULaw / CAAC connectors, pinpoint the 条款.

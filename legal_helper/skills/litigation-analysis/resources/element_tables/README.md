# Element-table presets

Cause-of-action decompositions for `/litigation-analysis`. Each preset
is a starting point for the element table — adapt to the matter, never
paste blind.

## Schema

```yaml
name: <must equal the file stem>
display_name: <bilingual label>
cause_of_action: <案由, as courts caption it>
legal_basis:            # the claim's statutory anchor(s)
  - source: 《...》      # instrument, 书名号 form for PRC law
    pinpoint: 第N条
elements:               # what the claimant must establish
  - id: snake_case_id
    element_zh: ...     # at least one of element_zh / element_en
    element_en: ...
    burden: claimant | respondent
    evidence_hints: [typical proof, ...]
    authority:          # may be empty (practice), never invented
      - source: 《...》
        pinpoint: 第N条
defenses:               # optional; same entry shape as elements
  - id: ...
    burden: respondent
    stage: arises | extinguished | exercisable | quantum
    ...
```

`stage` places a defence in the claim lifecycle
(`references/issue_element_method.md` §2) so the table reads in order:

- `arises` — the right never came into existence (invalidity, statutory
  exception, exemption such as 不可抗力);
- `extinguished` — it existed and has ended (履行, 抵销, 免除, guarantee
  period lapse);
- `exercisable` — it exists but cannot be enforced now (诉讼时效,
  同时履行 / 先履行抗辩, 先诉抗辩);
- `quantum` — it survives but in a smaller amount (违约金调整, 过失相抵,
  减价, 减损).

A denial of one of the claimant's elements (否认) is not a defence and does
not get a `defenses` entry; it stays on the element row.

## Usage rules

- **Re-verify every pinpoint** via `pkulaw_fatiao` /
  `pkulaw_citation_validator` before it enters the element table or the
  bundle — presets go stale when laws are revised or renumbered. Pick the
  version in force on the event date (general playbook §0).
- An empty `authority` list means the element rests on practice /
  case-law synthesis; say so in the table. Never fill the gap with an
  invented article number.
- `burden` follows the element, not the claim — defenses carry
  respondent burdens by default, but reversals exist; check
  `references/methodology.md` §2.
- Presets cover frequent 案由 only; for anything else, decompose from
  the 请求权基础 directly and note `no preset — decomposed from statute`
  in the bundle.

## Provenance

- `sale_price`, `lease_rent`, `guarantee`, `unjust_enrichment` and
  `property_service_fee` were written from the 民法典 text published at
  court.gov.cn (zixun/xiangqing/233181.html) and the cited SPC
  interpretations (买卖合同解释 and 诉讼时效规定 as amended by 法释〔2020〕17号;
  担保制度解释 法释〔2020〕28号), every pinpoint checked on 2026-10-07. The
  same check corrected `loan_dispute`: "no delivery" is a denial on the
  delivery element (民间借贷规定 第十五条第二款), and 第十六条 is the
  transfer-voucher rule.
- `BLK-WHT/LEGO` (Apache-2.0; https://github.com/BLK-WHT/LEGO) doctrine
  cards were consulted as an issue checklist only. A spot-check of 15 cards
  (~170 statements) against the 民法典 text found most statements accurate
  but several material errors — an exception carried over from the repealed
  担保法, a default mutual-recourse rule contrary to 担保制度解释 第十三条,
  delivery treated as transferring registrable property, an invented limit
  on 第九百八十八条, an unsourced "statutory termination outranks agreed
  termination" — and no article pinpoints. No card text was copied into
  these presets.
- The `stage` lifecycle follows `lilialla/request-right-skill-reference`
  (CC BY 4.0), as adapted in `references/issue_element_method.md`.

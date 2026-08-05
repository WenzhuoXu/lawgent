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
    ...
```

## Usage rules

- **Re-verify every pinpoint** via `pkulaw_fatiao` /
  `pkulaw_citation_validator` before it enters the element table or the
  bundle — presets go stale when laws are revised or renumbered.
- An empty `authority` list means the element rests on practice /
  case-law synthesis; say so in the table. Never fill the gap with an
  invented article number.
- `burden` follows the element, not the claim — defenses carry
  respondent burdens by default, but reversals exist; check
  `references/methodology.md` §2.
- Presets cover frequent 案由 only; for anything else, decompose from
  the 请求权基础 directly and note `no preset — decomposed from statute`
  in the bundle.

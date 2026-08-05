# Clause library

Bilingual PRC-first clause presets for `/draft-agreement`. Each `.yaml`
file defines one agreement family. Load with `read_document` (plain
text), build the clause plan, then adapt clause text to the deal terms —
library text is a starting position, not boilerplate to paste untouched.

## Schema

```yaml
name: <preset id, matches filename>
display_name: "<English> / <中文>"
applies_to: [<deal types this preset fits>]
side_default: "<which side the library positions favour>"
clauses:
  - id: <snake_case id, from the /review-contract clause buckets>
    title_zh: "<clause heading, 中文>"
    title_en: "<clause heading, English>"
    text_zh: |
      <model clause text, 中文>
    text_en: |
      <model clause text, English>
    legal_basis:
      - source: "<statute / regulation name (original language)>"
        pinpoint: "<第N条 / art. N / § N — re-verify via PKULaw each run>"
    notes: "<side flips, fallbacks, when to drop — optional>"
```

Rules (see `references/methodology.md` §2–§4 for the full discipline):

- Clause `id`s reuse the `/review-contract` clause-bucket vocabulary so
  draft → review round-trips share terms; new ids only when no bucket
  fits.
- `legal_basis` pinpoints are drafting anchors, not proof: re-verify via
  `pkulaw_fatiao` / `pkulaw_citation_validator` before the draft ships.
  Clauses that are pure market practice carry an empty `legal_basis` and
  get the `best-practice` label in Findings, never an invented citation.
- Placeholders in clause text use `【 】` (zh) / `[ ]` (en); every one
  left unresolved must surface as a Finding.
- Domain packs ship their own clause presets in their overlay file, not
  here.

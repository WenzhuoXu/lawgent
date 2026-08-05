# Grid templates

Column presets for `/tabular-review`. Each `.yaml` file defines one
preset. Load with `read_document` (they are plain text), then confirm the
column list with the user before extraction.

## Schema

```yaml
name: <preset id, matches filename>
display_name: "<English> / <中文>"
applies_to: [<document types this preset fits>]
columns:
  - id: <snake_case id>
    header_en: "<grid header, English>"
    header_zh: "<grid header, 中文>"
    type: extract | classify | flag
    question: "<the exact review question answered into the cell>"
    enum: [<closed value list — classify columns only>]
```

Rules (see `references/methodology.md` §2 for the full discipline):

- Pick `header_en` or `header_zh` per the user's language; never both in
  one grid.
- Every column in a preset is substantive, so the grid writes it as the
  pair `X` / `X — 依据` regardless of what the preset says.
- Presets are starting points: drop columns the user does not need and
  append user-specific questions after the preset columns.
- Domain packs ship their own presets in their overlay file, not here.
